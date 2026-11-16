"""Request validation with the same rules, field errors and messages the Laravel API had.

Rules are written as Laravel writes them: strings such as "required", "max:255" or "in:a,b", and rule
objects for the ones that need the database (Exists, Unique) or a callable that receives
`(attribute, value, fail)`. Errors come back as a 422 shaped like Laravel's:
`{"message": "...", "errors": {"field": ["..."]}}`.

`validate()` returns the validated fields with integers, numbers and booleans converted to Python types.
"""

import re
from collections.abc import Callable, Iterable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from starlette.datastructures import UploadFile

from app import clock

MISSING: Any = object()


class ValidationError(Exception):
    def __init__(self, errors: dict[str, list[str]]):
        super().__init__(next(iter(errors.values()))[0] if errors else "The given data was invalid.")
        self.errors = errors

    @property
    def message(self) -> str:
        messages = [message for field in self.errors.values() for message in field]
        if not messages:
            return "The given data was invalid."
        extra = len(messages) - 1
        if extra:
            return f"{messages[0]} (and {extra} more {'error' if extra == 1 else 'errors'})"
        return messages[0]


def fail(field: str, message: str) -> None:
    raise ValidationError({field: [message]})


MESSAGES = {
    "required": "The :attribute field is required.",
    "present": "The :attribute field must be present.",
    "prohibited": "The :attribute field is prohibited.",
    "string": "The :attribute field must be a string.",
    "integer": "The :attribute field must be an integer.",
    "numeric": "The :attribute field must be a number.",
    "boolean": "The :attribute field must be true or false.",
    "array": "The :attribute field must be an array.",
    "email": "The :attribute field must be a valid email address.",
    "date": "The :attribute field must be a valid date.",
    "date_format": "The :attribute field must match the format :format.",
    "after_or_equal": "The :attribute field must be a date after or equal to :date.",
    "in": "The selected :attribute is invalid.",
    "not_in": "The selected :attribute is invalid.",
    "exists": "The selected :attribute is invalid.",
    "unique": "The :attribute has already been taken.",
    "regex": "The :attribute field format is invalid.",
    "distinct": "The :attribute field has a duplicate value.",
    "different": "The :attribute field and :other must be different.",
    "image": "The :attribute field must be an image.",
    "mimes": "The :attribute field must be a file of type: :values.",
    "url": "The :attribute field must be a valid URL.",
    "max.numeric": "The :attribute field must not be greater than :max.",
    "max.string": "The :attribute field must not be greater than :max characters.",
    "max.array": "The :attribute field must not have more than :max items.",
    "max.file": "The :attribute field must not be greater than :max kilobytes.",
    "min.numeric": "The :attribute field must be at least :min.",
    "min.string": "The :attribute field must be at least :min characters.",
    "min.array": "The :attribute field must have at least :min items.",
    "min.file": "The :attribute field must be at least :min kilobytes.",
    "between.numeric": "The :attribute field must be between :min and :max.",
    "between.string": "The :attribute field must be between :min and :max characters.",
    "between.array": "The :attribute field must have between :min and :max items.",
    "between.file": "The :attribute field must be between :min and :max kilobytes.",
    "gt.numeric": "The :attribute field must be greater than :value.",
    "gt.string": "The :attribute field must be greater than :value characters.",
    "gt.array": "The :attribute field must have more than :value items.",
    "gt.file": "The :attribute field must be greater than :value kilobytes.",
}

# Rules that run even when the field is missing or empty.
IMPLICIT = {"required", "present", "prohibited", "required_if"}
SIZED = {"max", "min", "between", "gt"}
INT_PATTERN = re.compile(r"^\s*[+-]?\d+\s*$")
NUMBER_PATTERN = re.compile(r"^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
IMAGE_SIGNATURES = {
    "jpg": (b"\xff\xd8\xff",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "gif": (b"GIF87a", b"GIF89a"),
    "bmp": (b"BM",),
}


class Rule:
    """A rule object. `name` keys its message, as in "field.name" custom messages."""

    name = "rule"
    implicit = False

    def check(self, validator: "Validator", attribute: str, value: Any) -> str | None:
        raise NotImplementedError


class In(Rule):
    name = "in"

    def __init__(self, values: Iterable[Any]):
        self.values = [str(value) for value in values]

    def check(self, validator, attribute, value):
        if isinstance(value, (list, dict)) or scalar_string(value) not in self.values:
            return validator.message(attribute, self.name)
        return None


class NotIn(In):
    name = "not_in"

    def check(self, validator, attribute, value):
        if not isinstance(value, (list, dict)) and scalar_string(value) in self.values:
            return validator.message(attribute, self.name)
        return None


class Exists(Rule):
    """Passes when `lookup(value)` finds a row."""

    name = "exists"

    def __init__(self, lookup: Callable[[Any], bool]):
        self.lookup = lookup

    def check(self, validator, attribute, value):
        if isinstance(value, (list, dict)) or not self.lookup(value):
            return validator.message(attribute, self.name)
        return None


class Unique(Rule):
    """Passes when `taken(value)` finds no other row with the value."""

    name = "unique"

    def __init__(self, taken: Callable[[Any], bool]):
        self.taken = taken

    def check(self, validator, attribute, value):
        if self.taken(value):
            return validator.message(attribute, self.name)
        return None


class Regex(Rule):
    name = "regex"

    def __init__(self, pattern: str):
        self.pattern = re.compile(pattern)

    def check(self, validator, attribute, value):
        if not isinstance(value, (str, int, float)) or not self.pattern.search(str(value)):
            return validator.message(attribute, self.name)
        return None


class RequiredIf(Rule):
    name = "required"
    implicit = True

    def __init__(self, condition: bool):
        self.condition = condition

    def check(self, validator, attribute, value):
        if self.condition and not filled(value):
            return validator.message(attribute, self.name)
        return None


def scalar_string(value: Any) -> str:
    if value is True:
        return "1"
    if value is False:
        return "0"
    if value is None:
        return ""
    return str(value)


def filled(value: Any) -> bool:
    """Laravel's "required": not missing, null, blank or empty."""
    if value is MISSING or value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, dict)):
        return len(value) > 0
    if isinstance(value, UploadFile):
        return bool(value.filename)
    return True


def is_integer(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value.is_integer()
    return isinstance(value, str) and bool(INT_PATTERN.match(value))


def is_numeric(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float, Decimal)):
        return True
    return isinstance(value, str) and bool(NUMBER_PATTERN.match(value))


def is_boolean(value: Any) -> bool:
    return value in (True, False, 0, 1, "0", "1") and not isinstance(value, float)


def to_bool(value: Any) -> bool:
    """Laravel's $request->boolean(): true for 1, "1", "true", "on" and "yes"."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)


def parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text in ("today", "now"):
        return clock.today()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%d %B %Y", "%B %d, %Y", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def image_type(upload: UploadFile) -> str | None:
    """The image type from the file's first bytes."""
    head = upload.file.read(16)
    upload.file.seek(0)
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    for kind, signatures in IMAGE_SIGNATURES.items():
        if any(head.startswith(signature) for signature in signatures):
            return kind
    return None


def upload_kilobytes(upload: UploadFile) -> float:
    if upload.size is not None:
        return upload.size / 1024
    position = upload.file.tell()
    upload.file.seek(0, 2)
    size = upload.file.tell()
    upload.file.seek(position)
    return size / 1024


def display(attribute: str) -> str:
    return attribute.replace("_", " ")


def wildcard(attribute: str) -> str:
    return ".".join("*" if part.isdigit() else part for part in attribute.split("."))


def get_path(data: Any, attribute: str) -> Any:
    current = data
    for part in attribute.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return MISSING
    return current


def set_path(data: Any, attribute: str, value: Any) -> None:
    parts = attribute.split(".")
    current = data
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    last = parts[-1]
    if isinstance(current, list):
        current[int(last)] = value
    else:
        current[last] = value


def expand(data: Any, pattern: str) -> list[str]:
    """The concrete attributes a pattern such as "items.*.quantity" names in the data."""
    if "*" not in pattern:
        return [pattern]
    head, _, tail = pattern.partition(".*")
    container = get_path(data, head)
    if isinstance(container, list):
        keys = [str(index) for index in range(len(container))]
    elif isinstance(container, dict):
        keys = [str(key) for key in container]
    else:
        return []
    attributes: list[str] = []
    for key in keys:
        attributes.extend(expand(data, f"{head}.{key}{tail}"))
    return attributes


class Validator:
    def __init__(
        self,
        data: dict[str, Any],
        rules: dict[str, list[Any]],
        messages: dict[str, str] | None = None,
    ):
        self.data = data
        self.rules = rules
        self.custom = messages or {}
        self.errors: dict[str, list[str]] = {}
        self._after: list[Callable[["Validator"], None]] = []

    def after(self, callback: Callable[["Validator"], None]) -> "Validator":
        self._after.append(callback)
        return self

    def add_error(self, attribute: str, message: str) -> None:
        self.errors.setdefault(attribute, []).append(message)

    def message(self, attribute: str, rule: str, size_kind: str | None = None, **replace: Any) -> str:
        for key in (f"{attribute}.{rule}", f"{wildcard(attribute)}.{rule}"):
            if key in self.custom:
                template = self.custom[key]
                break
        else:
            template = MESSAGES.get(f"{rule}.{size_kind}" if size_kind else rule, MESSAGES.get(rule, "The :attribute field is invalid."))
        text = template.replace(":attribute", display(attribute))
        for name, value in replace.items():
            text = text.replace(f":{name}", str(value))
        return text

    def validate(self) -> dict[str, Any]:
        for pattern, rules in self.rules.items():
            for attribute in expand(self.data, pattern):
                self._validate_attribute(attribute, rules)
        for callback in self._after:
            callback(self)
        if self.errors:
            raise ValidationError(self.errors)
        return self.validated()

    def _validate_attribute(self, attribute: str, rules: list[Any]) -> None:
        value = get_path(self.data, attribute)
        names = {rule.split(":")[0] for rule in rules if isinstance(rule, str)}
        if "sometimes" in names and value is MISSING:
            return
        for rule in rules:
            if isinstance(rule, str) and rule in ("sometimes", "nullable", "bail"):
                continue
            implicit = (isinstance(rule, str) and rule.split(":")[0] in IMPLICIT) or (isinstance(rule, Rule) and rule.implicit)
            empty = value is MISSING or (isinstance(value, str) and value.strip() == "")
            if empty and not implicit:
                continue
            if value is None and "nullable" in names and not implicit:
                continue
            error = self._check(rule, attribute, value, rules)
            if error:
                self.add_error(attribute, error)
                if implicit:
                    return

    def _size_kind(self, value: Any, rules: list[Any]) -> str:
        names = {rule.split(":")[0] for rule in rules if isinstance(rule, str)}
        if isinstance(value, UploadFile):
            return "file"
        if isinstance(value, list):
            return "array"
        if names & {"numeric", "integer"} and is_numeric(value):
            return "numeric"
        return "string"

    def _size(self, value: Any, kind: str) -> float:
        if kind == "file":
            return upload_kilobytes(value)
        if kind == "array":
            return len(value)
        if kind == "numeric":
            return float(value)
        return len(value) if isinstance(value, str) else len(str(value))

    def _check(self, rule: Any, attribute: str, value: Any, rules: list[Any]) -> str | None:
        if isinstance(rule, Rule):
            return rule.check(self, attribute, value)
        if callable(rule):
            failures: list[str] = []
            rule(attribute, value, failures.append)
            return failures[0] if failures else None

        name, _, parameter = rule.partition(":")
        params = parameter.split(",") if parameter else []
        message = lambda **replace: self.message(attribute, name, **replace)  # noqa: E731

        match name:
            case "required":
                return None if filled(value) else message()
            case "present":
                return None if value is not MISSING else message()
            case "prohibited":
                return message() if filled(value) else None
            case "string":
                return None if isinstance(value, str) else message()
            case "integer":
                return None if is_integer(value) else message()
            case "numeric":
                return None if is_numeric(value) else message()
            case "boolean":
                return None if is_boolean(value) else message()
            case "array":
                return None if isinstance(value, (list, dict)) else message()
            case "email":
                return None if isinstance(value, str) and EMAIL_PATTERN.match(value) else message()
            case "date":
                return None if parse_date(value) else message()
            case "date_format":
                valid = isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) and parse_date(value)
                return None if valid else message(format=parameter)
            case "after_or_equal":
                other = get_path(self.data, parameter)
                limit = parse_date(other) if other is not MISSING else parse_date(parameter)
                mine = parse_date(value)
                if limit is None or mine is None or mine < limit:
                    shown = display(parameter) if other is not MISSING else parameter
                    return message(date=shown)
                return None
            case "in":
                return In(params).check(self, attribute, value)
            case "not_in":
                return NotIn(params).check(self, attribute, value)
            case "regex":
                return Regex(parameter).check(self, attribute, value)
            case "distinct":
                parent = attribute.rsplit(".", 1)[0]
                siblings = get_path(self.data, parent)
                if isinstance(siblings, list) and [scalar_string(item) for item in siblings].count(scalar_string(value)) > 1:
                    return message()
                return None
            case "different":
                other = get_path(self.data, parameter)
                return message(other=display(parameter)) if other is not MISSING and scalar_string(other) == scalar_string(value) else None
            case "image":
                if not isinstance(value, UploadFile) or image_type(value) not in ("jpg", "png", "gif", "bmp", "webp"):
                    return message()
                return None
            case "mimes":
                kind = image_type(value) if isinstance(value, UploadFile) else None
                allowed = set(params) | ({"jpg"} if "jpeg" in params else set())
                return None if kind in allowed else message(values=", ".join(params))
            case "url":
                schemes = params or ["http", "https"]
                valid = isinstance(value, str) and re.match(rf"^({'|'.join(schemes)})://[^\s/$.?#][^\s]*$", value, re.IGNORECASE)
                return None if valid else message()
            case _ if name in SIZED:
                kind = self._size_kind(value, rules)
                if kind == "numeric" and not is_numeric(value):
                    return None
                size = self._size(value, kind)
                limits = [float(param) for param in params]
                failed = {
                    "max": lambda: size > limits[0],
                    "min": lambda: size < limits[0],
                    "between": lambda: size < limits[0] or size > limits[1],
                    "gt": lambda: size <= limits[0],
                }[name]()
                if not failed:
                    return None
                shown = [param for param in params]
                return self.message(
                    attribute, name, kind, max=shown[-1], min=shown[0], value=shown[0]
                )
        raise ValueError(f"Unknown validation rule: {rule}")

    def validated(self) -> dict[str, Any]:
        """The fields that have rules, with numbers and booleans converted."""
        result: dict[str, Any] = {}
        for pattern in self.rules:
            top = pattern.split(".")[0]
            if top in self.data and top not in result:
                result[top] = copy_value(self.data[top])
        for pattern, rules in self.rules.items():
            names = {rule.split(":")[0] for rule in rules if isinstance(rule, str)}
            for attribute in expand(result, pattern):
                value = get_path(result, attribute)
                if value is MISSING or value is None:
                    continue
                if "boolean" in names:
                    set_path(result, attribute, bool(int(value)) if isinstance(value, str) else bool(value))
                elif "integer" in names and is_integer(value):
                    set_path(result, attribute, int(float(value)) if isinstance(value, (str, float)) else value)
                elif "numeric" in names and is_numeric(value):
                    set_path(result, attribute, to_decimal(value))
        return result


def copy_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: copy_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [copy_value(item) for item in value]
    return value


def to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return Decimal(0)


def validate(
    data: dict[str, Any],
    rules: dict[str, list[Any]],
    messages: dict[str, str] | None = None,
    after: Callable[[Validator], None] | None = None,
) -> dict[str, Any]:
    validator = Validator(data, rules, messages)
    if after:
        validator.after(after)
    return validator.validate()

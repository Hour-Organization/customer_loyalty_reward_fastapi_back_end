"""Request input, JSON responses, pagination and error handling shared by the routers."""

import functools
import inspect
import logging
import math
import re
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.config import get_settings
from app.validation import ValidationError, to_bool, validate

log = logging.getLogger("app")

DEFAULT_PER_PAGE = 30
MAX_PER_PAGE = 100
# Laravel leaves passwords untrimmed.
UNTRIMMED = {"password", "password_confirmation", "current_password"}


# ---------------------------------------------------------------- Input


def _clean(value: Any, key: str = "") -> Any:
    """Trim strings and turn empty ones into null, as Laravel's TrimStrings and ConvertEmptyStringsToNull do."""
    if isinstance(value, dict):
        return {k: _clean(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v, key) for v in value]
    if isinstance(value, str):
        if key not in UNTRIMMED:
            value = value.strip()
        return None if value == "" else value
    return value


def _listify(value: Any) -> Any:
    """Turn {"0": a, "1": b} into [a, b], as PHP arrays with numeric keys are lists."""
    if isinstance(value, dict):
        value = {key: _listify(item) for key, item in value.items()}
        if value and all(re.fullmatch(r"\d+", str(key)) for key in value):
            return [value[key] for key in sorted(value, key=int)]
    return value


def _php_form(items: list[tuple[str, Any]]) -> dict[str, Any]:
    """Read form fields named the PHP way: `images[]`, `variants[0][size]`."""
    data: dict[str, Any] = {}
    for name, value in items:
        match = re.match(r"^([^\[]+)((?:\[[^\]]*\])*)$", name)
        if not match:
            data[name] = value
            continue
        head, rest = match.groups()
        keys = [head, *re.findall(r"\[([^\]]*)\]", rest)]
        current: Any = data
        for index, key in enumerate(keys):
            last = index == len(keys) - 1
            if key == "":
                if not isinstance(current, list):
                    raise ValueError
                if last:
                    current.append(value)
                else:
                    current.append({})
                    current = current[-1]
                continue
            if last:
                current[key] = value
            else:
                nxt = keys[index + 1]
                if key not in current or not isinstance(current[key], (dict, list)):
                    current[key] = [] if nxt == "" else {}
                current = current[key]
    return _listify(data)


async def request_input(request: Request) -> dict[str, Any]:
    """The query string and body together, like Laravel's $request->all()."""
    data: dict[str, Any] = _php_form(list(request.query_params.multi_items()))
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.body()
        if body:
            try:
                parsed = await request.json()
            except ValueError:
                raise HTTPException(400, "The request body is not valid JSON.")
            if isinstance(parsed, dict):
                data.update(parsed)
    elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        fields = [(name, value) for name, value in form.multi_items() if not (isinstance(value, UploadFile) and not value.filename)]
        data.update(_php_form(fields))
    return _clean(data)


Input = Annotated[dict[str, Any], Depends(request_input)]


def method_of(request: Request, data: dict[str, Any]) -> str:
    """The request method, honouring the `_method` field browsers use to send PATCH as a multipart POST."""
    if request.method == "POST":
        spoofed = str(data.get("_method") or "").upper()
        if spoofed in ("PUT", "PATCH", "DELETE"):
            return spoofed
        raise HTTPException(405, f"The POST method is not supported for route {request.url.path.lstrip('/')}. Supported methods: PUT, PATCH.")
    return request.method


def presence(method: str) -> str:
    """PATCH updates only the fields sent; POST and PUT need every required field."""
    return "sometimes" if method == "PATCH" else "required"


def uploads(data: dict[str, Any], key: str) -> list[UploadFile]:
    value = data.get(key)
    if isinstance(value, UploadFile):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, UploadFile)]
    return []


def flag(data: dict[str, Any], key: str) -> bool:
    return to_bool(data.get(key))


# ---------------------------------------------------------------- Responses


def respond(payload: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status)


def ok(data: Any, message: str, status: int = 200) -> JSONResponse:
    return respond({"success": True, "message": message, "data": data}, status)


def done(message: str) -> JSONResponse:
    return respond({"success": True, "message": message})


def refuse(message: str, status: int = 422) -> JSONResponse:
    return respond({"success": False, "message": message}, status)


def not_found(model: str = "record") -> HTTPException:
    return HTTPException(404, f"No query results for model [{model}].")


def find_or_404(db: Session, model: type, id: int, *, include_deleted: bool = False):
    statement = select(model).where(model.id == id)
    if include_deleted:
        statement = statement.execution_options(include_deleted=True)
    found = db.scalars(statement).first()
    if found is None:
        raise HTTPException(404, f"No query results for model [{model.__name__}] {id}")
    return found


def attempt(failure_message: str) -> Callable:
    """Turn an unexpected error into a JSON 500 with a friendly message, as the Laravel controllers did.

    Validation, authentication and HTTP errors pass through. The request's session is rolled back.
    """

    def decorate(handler: Callable) -> Callable:
        def failed(exception: Exception) -> JSONResponse:
            log.exception("%s", failure_message)
            payload: dict[str, Any] = {"success": False, "message": failure_message}
            if get_settings().app_debug:
                payload["error"] = str(exception)
            return respond(payload, 500)

        if inspect.iscoroutinefunction(handler):

            @functools.wraps(handler)
            async def async_wrapper(*args, **kwargs):
                try:
                    return await handler(*args, **kwargs)
                except (ValidationError, HTTPException):
                    raise
                except Exception as exception:  # noqa: BLE001
                    return failed(exception)

            return async_wrapper

        @functools.wraps(handler)
        def wrapper(*args, **kwargs):
            try:
                return handler(*args, **kwargs)
            except (ValidationError, HTTPException):
                raise
            except Exception as exception:  # noqa: BLE001
                return failed(exception)

        return wrapper

    return decorate


# ---------------------------------------------------------------- Pagination


def per_page(data: dict[str, Any]) -> tuple[int, int]:
    """The page and page size asked for: 30 rows by default, 100 at most."""
    checked = validate(
        data,
        {
            "page": ["nullable", "integer", "min:1"],
            "per_page": ["nullable", "integer", "min:1", f"max:{MAX_PER_PAGE}"],
        },
    )
    return checked.get("page") or 1, checked.get("per_page") or DEFAULT_PER_PAGE


def paginate(db: Session, statement: Select, data: dict[str, Any]) -> tuple[list[Any], dict[str, int]]:
    page, size = per_page(data)
    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
    rows = list(db.scalars(statement.limit(size).offset((page - 1) * size)).unique())
    meta = {"current_page": page, "per_page": size, "total": total, "last_page": max(math.ceil(total / size), 1)}
    return rows, meta


def paginated(rows: list[Any], meta: dict[str, int], message: str, extra: dict[str, Any] | None = None) -> JSONResponse:
    return respond({"success": True, "message": message, "data": rows, "meta": meta, **(extra or {})})


def page_of(
    db: Session,
    statement: Select,
    data: dict[str, Any],
    message: str,
    shape: Callable[[Any], Any],
) -> JSONResponse:
    rows, meta = paginate(db, statement, data)
    return paginated([shape(row) for row in rows], meta, message)


# ---------------------------------------------------------------- Dates


def from_day(column: Any, value: Any) -> Any:
    """`column` on or after the start of the day, like whereDate(column, '>=', day)."""
    from datetime import datetime

    from app.validation import parse_date

    return column >= datetime.combine(parse_date(value), datetime.min.time())


def until_day(column: Any, value: Any) -> Any:
    """`column` on or before the end of the day, like whereDate(column, '<=', day)."""
    from datetime import datetime, timedelta

    from app.validation import parse_date

    return column < datetime.combine(parse_date(value) + timedelta(days=1), datetime.min.time())


def update_route(router: Any, path: str, methods: tuple[str, ...] = ("PUT", "PATCH", "POST")) -> Callable:
    """Register an update for each method, with its own operation id. POST is the multipart `_method` alias."""

    def register(handler: Callable) -> Callable:
        for method in methods:
            router.add_api_route(
                path,
                handler,
                methods=[method],
                operation_id=f"{handler.__name__}_{method.lower()}",
                include_in_schema=method != "POST",
            )
        return handler

    return register

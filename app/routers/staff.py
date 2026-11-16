"""Staff accounts and role permissions (Super Admin only)."""

import re
import unicodedata
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import clock
from app.auth import Db, SuperAdmin, hash_password, revoke_tokens
from app.http import Input, attempt, done, find_or_404, method_of, ok, page_of, presence, refuse
from app.models import Role, User
from app.services.records import log_staff_change
from app.shapes import counts
from app.validation import Exists, In, Unique, validate

router = APIRouter(prefix="/backend", tags=["staff accounts"])


def plural(word: str, count: int) -> str:
    return word if count == 1 else f"{word}s"


def snake(label: str) -> str:
    """Laravel's Str::snake(Str::ascii($label)): "Marketing Manager" becomes "marketing_manager"."""
    ascii_label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    words = re.sub(r"\s+", "", " ".join(word[:1].upper() + word[1:] for word in ascii_label.split(" ")))
    return re.sub(r"(.)(?=[A-Z])", r"\1_", words).lower()


# ---------------------------------------------------------------- Roles


def role_query():
    return select(Role).options(selectinload(Role.permissions))


def role_api(db: Session, role: Role) -> dict[str, Any]:
    return role.to_api(counts(db, User.role_id, [role.id]).get(role.id, 0))


def role_fields(db: Session, method: str, role: Role | None, data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("label"), str):
        data["label"] = re.sub(r"\s+", " ", data["label"].strip())

    def has_section(attribute, value, fail):
        if not [page for page in (value if isinstance(value, list) else [value]) if page != Role.PROFILE_PAGE]:
            fail("Select at least one section for this role.")

    def unique_label(attribute, value, fail):
        statement = select(Role.id).where(func.lower(Role.label) == str(value).lower(), Role.deleted_at.is_(None))
        if role is not None:
            statement = statement.where(Role.id != role.id)
        if db.scalar(statement):
            fail("A role with that name already exists.")

    return validate(
        data,
        {
            "label": [presence(method), "string", "min:2", "max:50", unique_label],
            "pages": ["sometimes", "array", has_section],
            "pages.*": ["string", "distinct", In([page for page in Role.PAGES if page not in Role.RESERVED_PAGES])],
            "stock_authority": ["sometimes", "string", In(["clerk", "manager"])],
        },
        {"pages.*.in": "Exchange Rate, User Management, and Role Permissions are reserved for Super Admin."},
    )


def unique_code(db: Session, label: str) -> str:
    base = snake(label) or "role"
    code, suffix = base, 2
    # Deleted roles keep their code (unique in the database), so they count as taken.
    while db.scalar(select(Role.id).where(Role.code == code).execution_options(include_deleted=True)):
        code = f"{base}_{suffix}"
        suffix += 1
    return code


SUPER_ADMIN_IS_FIXED = "Super Admin always has full access and cannot be changed."


@router.get("/roles")
@attempt("Could not load roles.")
def list_roles(db: Db, staff: SuperAdmin):
    roles = list(db.scalars(role_query().order_by(Role.id)))
    users = counts(db, User.role_id, [role.id for role in roles])
    return ok([role.to_api(users.get(role.id, 0)) for role in roles], "Roles retrieved successfully")


@router.post("/roles", status_code=201)
@attempt("Could not create the role.")
def create_role(db: Db, staff: SuperAdmin, data: Input):
    fields = role_fields(db, "POST", None, data)
    role = Role(code=unique_code(db, fields["label"]), label=fields["label"], stock_authority=fields.get("stock_authority") or "clerk")
    role.sync_pages(fields.get("pages") or ["dashboard"])
    db.add(role)
    db.flush()
    log_staff_change(db, staff, "ROLE", "CREATED")
    db.commit()
    return ok(role_api(db, role), "Role created successfully", 201)


@router.get("/roles/{role_id:int}")
@attempt("Could not load the role.")
def show_role(role_id: int, db: Db, staff: SuperAdmin):
    return ok(role_api(db, find_or_404(db, Role, role_id)), "Role retrieved successfully")


@router.api_route("/roles/{role_id:int}", methods=["PUT", "PATCH", "POST"])
@attempt("Could not update the role.")
def update_role(role_id: int, request: Request, db: Db, staff: SuperAdmin, data: Input):
    role = find_or_404(db, Role, role_id)
    if role.is_super_admin():
        return refuse(SUPER_ADMIN_IS_FIXED)
    fields = role_fields(db, method_of(request, data), role, data)
    if "label" in fields:
        role.label = fields["label"]
    if "stock_authority" in fields:
        role.stock_authority = fields["stock_authority"]
    if "pages" in fields:
        role.sync_pages(fields["pages"] or [])
    db.flush()
    log_staff_change(db, staff, "ROLE", "UPDATED", role.id)
    db.commit()
    return ok(role_api(db, role), "Role updated successfully")


@router.delete("/roles/{role_id:int}")
@attempt("Could not delete the role.")
def delete_role(role_id: int, db: Db, staff: SuperAdmin):
    role = find_or_404(db, Role, role_id)
    if role.is_super_admin():
        return refuse(SUPER_ADMIN_IS_FIXED)
    users = db.scalar(select(func.count()).where(User.role_id == role.id))
    if users:
        return refuse(f"Move the {users} staff {plural('user', users)} with the {role.label} role to another role first.")
    role.deleted_at = clock.now()
    log_staff_change(db, staff, "ROLE", "DELETED", role.id)
    db.commit()
    return done("Role deleted successfully")


# ---------------------------------------------------------------- Staff users


def user_query():
    return select(User).options(selectinload(User.role).selectinload(Role.permissions))


def user_fields(db: Session, method: str, user: User | None, data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("email"), str):
        data["email"] = data["email"].strip().lower()
    if method == "PATCH" and "password" in data and data["password"] is None:
        del data["password"]

    def email_taken(value) -> bool:
        statement = select(User.id).where(User.email == value)
        if user is not None:
            statement = statement.where(User.id != user.id)
        return bool(db.scalar(statement))

    return validate(
        data,
        {
            "name": [presence(method), "string", "max:255"],
            "email": [presence(method), "string", "email", "max:255", Unique(email_taken)],
            "password": [presence(method), "string", "min:8"],
            "role_id": [
                presence(method), "integer",
                Exists(lambda value: bool(db.scalar(select(Role.id).where(Role.id == int(value), Role.deleted_at.is_(None))))),
            ],  # fmt: skip
            "is_active": ["sometimes", "boolean"],
        },
        {"email.unique": "A staff user already has that email address."},
    )


def protected_change_error(db: Session, actor: User, user: User, was_active: bool, was_role_id: int | None) -> str | None:
    """Staff cannot lock themselves out, and an active Super Admin must remain."""
    super_admin_id = db.scalar(select(Role.id).where(Role.code == Role.SUPER_ADMIN))
    # Without a Super Admin role, a user with no role (None) must not count as holding it.
    was_super_admin = super_admin_id is not None and was_active and was_role_id == super_admin_id
    stays_super_admin = super_admin_id is not None and user.is_active and user.role_id == super_admin_id
    if user.id == actor.id and not user.is_active:
        return "You cannot disable your own account."
    if user.id == actor.id and was_super_admin and not stays_super_admin:
        return "You cannot remove your own Super Admin role."
    others = db.scalar(
        select(User.id).where(User.id != user.id, User.role_id == super_admin_id, User.is_active.is_(True))
    )
    if was_super_admin and not stays_super_admin and not others:
        return "At least one active Super Admin is required."
    return None


def user_response(db: Session, user_id: int) -> dict[str, Any]:
    db.expire_all()
    return db.scalars(user_query().where(User.id == user_id)).one().to_api()


@router.get("/users")
@attempt("Could not load staff users.")
def list_users(db: Db, staff: SuperAdmin, data: Input):
    statement = user_query().order_by(User.name, User.id)
    return page_of(db, statement, data, "Users retrieved successfully", lambda user: user.to_api())


@router.post("/users", status_code=201)
@attempt("Could not create the staff user.")
def create_user(db: Db, staff: SuperAdmin, data: Input):
    fields = user_fields(db, "POST", None, data)
    user = User(
        name=fields["name"],
        email=fields["email"],
        password=hash_password(fields["password"]),
        role_id=fields["role_id"],
        is_active=fields.get("is_active", True),
    )
    db.add(user)
    db.flush()
    log_staff_change(db, staff, "USER", "CREATED", user.name)
    db.commit()
    return ok(user_response(db, user.id), "User created successfully", 201)


@router.get("/users/{user_id:int}")
@attempt("Could not load the staff user.")
def show_user(user_id: int, db: Db, staff: SuperAdmin):
    find_or_404(db, User, user_id)
    return ok(user_response(db, user_id), "User retrieved successfully")


@router.api_route("/users/{user_id:int}", methods=["PUT", "PATCH", "POST"])
@attempt("Could not update the staff user.")
def update_user(user_id: int, request: Request, db: Db, staff: SuperAdmin, data: Input):
    user = find_or_404(db, User, user_id)
    fields = user_fields(db, method_of(request, data), user, data)
    was_active, was_role_id = user.is_active, user.role_id
    for name, value in fields.items():
        setattr(user, name, hash_password(value) if name == "password" else value)
    error = protected_change_error(db, staff, user, was_active, was_role_id)
    if error:
        db.rollback()
        return refuse(error)
    if was_active and not user.is_active:
        revoke_tokens(db, user)
    db.flush()
    log_staff_change(db, staff, "USER", "UPDATED", user.name)
    db.commit()
    return ok(user_response(db, user.id), "User updated successfully")


routers = [router]

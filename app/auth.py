"""Passwords, API tokens and the two guards: staff (`users`) and customers.

Tokens work like Laravel Sanctum's: the client gets "<id>|<40 random characters>" once, and the table keeps
only a SHA-256 of the random part. A token belongs to one kind of account, so a customer token is refused
on staff routes and the other way round. Tokens idle for longer than the configured timeout stop working.
"""

import hashlib
import hmac
import secrets
import string
from datetime import timedelta
from typing import Annotated

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from app import clock
from app.config import get_settings
from app.database import get_db
from app.models import Customer, PersonalAccessToken, Role, User

STAFF = "App\\Models\\User"
CUSTOMER = "App\\Models\\Customer"

Db = Annotated[Session, Depends(get_db)]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(get_settings().bcrypt_rounds)).decode()


def check_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def random_string(length: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_token(db: Session, owner: User | Customer, name: str) -> str:
    secret = random_string(40)
    token = PersonalAccessToken(
        tokenable_type=STAFF if isinstance(owner, User) else CUSTOMER,
        tokenable_id=owner.id,
        name=name[:255],
        token=hashlib.sha256(secret.encode()).hexdigest(),
        abilities='["*"]',
    )
    db.add(token)
    db.flush()
    return f"{token.id}|{secret}"


def revoke_tokens(db: Session, owner: User | Customer) -> None:
    db.execute(
        delete(PersonalAccessToken).where(
            PersonalAccessToken.tokenable_type == (STAFF if isinstance(owner, User) else CUSTOMER),
            PersonalAccessToken.tokenable_id == owner.id,
        )
    )


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, credentials = header.partition(" ")
    return credentials.strip() if scheme.lower() == "bearer" and credentials.strip() else None


def _find_token(db: Session, plain: str) -> PersonalAccessToken | None:
    if "|" in plain:
        token_id, secret = plain.split("|", 1)
        if not token_id.isdigit():
            return None
        token = db.get(PersonalAccessToken, int(token_id))
        digest = hashlib.sha256(secret.encode()).hexdigest()
        return token if token and hmac.compare_digest(token.token, digest) else None
    digest = hashlib.sha256(plain.encode()).hexdigest()
    return db.scalars(select(PersonalAccessToken).where(PersonalAccessToken.token == digest)).first()


def _authenticate(request: Request, db: Session, kind: str) -> tuple[PersonalAccessToken, User | Customer]:
    plain = _bearer(request)
    token = _find_token(db, plain) if plain else None
    if token is None or token.tokenable_type != kind:
        raise HTTPException(401, "Unauthenticated.")
    current = clock.now()
    if token.expires_at and token.expires_at <= current:
        raise HTTPException(401, "Unauthenticated.")
    idle = get_settings().token_idle_timeout
    last_activity = token.last_used_at or token.created_at
    if idle and last_activity and last_activity <= current - timedelta(minutes=idle):
        raise HTTPException(401, "Unauthenticated.")
    if kind == STAFF:
        owner = db.scalars(
            select(User).where(User.id == token.tokenable_id).options(selectinload(User.role).selectinload(Role.permissions))
        ).first()
    else:
        owner = db.get(Customer, token.tokenable_id)
    if owner is None:
        raise HTTPException(401, "Unauthenticated.")
    token.last_used_at = current
    db.commit()
    request.state.token = token
    return token, owner


def current_staff(request: Request, db: Db) -> User:
    return _authenticate(request, db, STAFF)[1]


def current_customer(request: Request, db: Db) -> Customer:
    return _authenticate(request, db, CUSTOMER)[1]


Staff = Annotated[User, Depends(current_staff)]
Shopper = Annotated[Customer, Depends(current_customer)]


def current_token(request: Request) -> PersonalAccessToken | None:
    return getattr(request.state, "token", None)


# ---------------------------------------------------------------- Gates


def forbid(message: str = "This action is unauthorized.") -> HTTPException:
    return HTTPException(403, message)


def pages(*names: str):
    """Staff whose role opens at least one of the pages."""

    def check(staff: Staff) -> User:
        if not any(staff.can_access_page(name) for name in names):
            raise forbid()
        return staff

    return Depends(check)


def super_admin(staff: Staff) -> User:
    """Staff accounts, roles and system settings are for Super Admin only."""
    if not staff.is_super_admin():
        raise forbid()
    return staff


def stock_access(staff: Staff) -> User:
    if not staff.stock_authority():
        raise forbid("Your role has no inventory access.")
    return staff


def stock_authority(level: str):
    def check(staff: Annotated[User, Depends(stock_access)]) -> User:
        if not staff.has_stock_authority(level):
            raise forbid(f"This action needs stock {level} authority.")
        return staff

    return Depends(check)


def require_stock_authority(staff: User, level: str) -> None:
    if not staff.has_stock_authority(level):
        raise forbid(f"This action needs stock {level} authority.")


SuperAdmin = Annotated[User, Depends(super_admin)]
StockStaff = Annotated[User, Depends(stock_access)]

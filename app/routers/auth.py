"""Signing in and out, registration, and each principal's own profile."""

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app import storage
from app.auth import (
    Db,
    Shopper,
    Staff,
    check_password,
    create_token,
    current_token,
    hash_password,
    random_string,
)
from app.http import Input, attempt, ok, respond
from app.models import Customer, Referral, Tier, User
from app.serial import file_url
from app.validation import Exists, Unique, ValidationError, validate

staff_router = APIRouter(prefix="/backend", tags=["staff auth"])
customer_router = APIRouter(tags=["customer auth"])

LOGIN_RULES = {
    "email": ["required", "email"],
    "password": ["required", "string"],
    "device_name": ["nullable", "string", "max:255"],
}


def _device(request: Request, data: dict) -> str:
    return (data.get("device_name") or request.headers.get("user-agent") or "api")[:255]


# ---------------------------------------------------------------- Staff


@staff_router.post("/login")
@attempt("Could not sign in. Please try again.")
def staff_login(request: Request, db: Db, data: Input):
    credentials = validate(data, LOGIN_RULES)
    user = db.scalars(select(User).where(User.email == credentials["email"])).first()
    if user is None or not check_password(credentials["password"], user.password):
        raise ValidationError({"email": ["The provided credentials are incorrect."]})
    if not user.is_active:
        raise ValidationError({"email": ["This staff account has been disabled."]})
    token = create_token(db, user, _device(request, credentials))
    db.commit()
    return respond(
        {
            "message": "Login successful",
            "token": token,
            "token_type": "Bearer",
            "user": user.to_array("id", "name", "email", "is_active"),
        }
    )


@staff_router.post("/logout")
@attempt("Could not sign out. Please try again.")
def staff_logout(request: Request, db: Db, staff: Staff):
    token = current_token(request)
    if token is not None:
        db.delete(token)
        db.commit()
    return respond({"message": "Logged out"})


@staff_router.get("/profile")
def staff_profile(staff: Staff):
    """The signed-in staff user, with the role and pages the frontend uses for menu access."""
    return ok(staff.to_api(), "Profile retrieved successfully")


@staff_router.patch("/profile")
@attempt("Could not update your profile.")
def update_staff_profile(db: Db, staff: Staff, data: Input):
    taken = lambda email: bool(  # noqa: E731
        db.scalar(select(User.id).where(User.email == email, User.id != staff.id))
    )
    fields = validate(
        data,
        {
            "name": ["required", "string", "max:255"],
            "email": ["required", "email", "max:255", Unique(taken)],
        },
    )
    staff.name = fields["name"]
    staff.email = fields["email"]
    db.commit()
    return ok(staff.to_api(), "Profile updated successfully")


# ---------------------------------------------------------------- Customers


def _unique_referral_code(db: Db) -> str:
    while True:
        code = random_string(8).upper()
        if not db.scalar(select(Customer.id).where(Customer.referral_code == code)):
            return code


@customer_router.post("/register", status_code=201)
@attempt("Could not create your account. Please try again.")
def register(request: Request, db: Db, data: Input):
    if isinstance(data.get("email"), str):
        data["email"] = data["email"].strip().lower()
    if isinstance(data.get("referral_code"), str):
        data["referral_code"] = data["referral_code"].upper()
    fields = validate(
        data,
        {
            "name": ["required", "string", "max:255"],
            "name_kh": ["nullable", "string", "max:255"],
            "email": [
                "required", "string", "email", "max:255",
                Unique(lambda email: bool(db.scalar(select(Customer.id).where(Customer.email == email)))),
            ],  # fmt: skip
            "password": ["required", "string", "min:8"],
            "phone": ["nullable", "string", "max:32"],
            "avatar": ["nullable", "image", "mimes:jpg,jpeg,png,webp", "max:2048"],
            "referral_code": [
                "nullable",
                "string",
                Exists(
                    lambda code: bool(
                        db.scalar(select(Customer.id).where(Customer.referral_code == code, Customer.is_active.is_(True)))
                    )
                ),
            ],
            "province": ["nullable", "string", "max:255"],
            "device_name": ["nullable", "string", "max:255"],
        },
        {
            "email.unique": "An account already exists with that email address.",
            "avatar.mimes": "The avatar must be a JPG, PNG or WebP image.",
            "avatar.max": "The avatar must be 2 MB or smaller.",
            "referral_code.exists": "That referral code is not valid.",
        },
    )
    entry_tier = db.scalars(select(Tier).order_by(Tier.min_points)).first()
    if entry_tier is None:
        raise HTTPException(503, "Registration is unavailable until loyalty tiers are set up.")

    avatar = storage.store(fields["avatar"], "customers/avatars") if fields.get("avatar") else None
    try:
        customer = Customer(
            tier_id=entry_tier.id,
            name=fields["name"],
            name_kh=fields.get("name_kh"),
            email=fields["email"],
            phone=fields.get("phone"),
            password=hash_password(fields["password"]),
            province=fields.get("province"),
            avatar=avatar,
            referral_code=_unique_referral_code(db),
        )
        db.add(customer)
        db.flush()
        code = fields.get("referral_code")
        referrer_id = db.scalar(select(Customer.id).where(Customer.referral_code == code)) if code else None
        if referrer_id:
            db.add(Referral(referrer_id=referrer_id, referred_id=customer.id))
        token = create_token(db, customer, _device(request, fields))
        db.commit()
    except Exception:
        # Nothing references the uploaded avatar once the customer is rolled back.
        db.rollback()
        storage.delete(avatar)
        raise

    db.refresh(customer)
    return respond(
        {
            "message": "Registration successful",
            "token": token,
            "token_type": "Bearer",
            "customer": {
                **customer.to_array("id", "name", "name_kh", "email", "phone", "province", "referral_code", "points_balance"),
                "avatar": file_url(avatar),
                "tier": entry_tier.to_array("id", "name"),
            },
        },
        201,
    )


@customer_router.post("/login")
@attempt("Could not sign in. Please try again.")
def customer_login(request: Request, db: Db, data: Input):
    credentials = validate(data, LOGIN_RULES)
    customer = db.scalars(select(Customer).where(Customer.email == credentials["email"])).first()
    if customer is None or not check_password(credentials["password"], customer.password):
        raise ValidationError({"email": ["The provided credentials are incorrect."]})
    if not customer.is_active:
        raise ValidationError({"email": ["This account has been deactivated."]})
    token = create_token(db, customer, _device(request, credentials))
    db.commit()
    return respond(
        {
            "message": "Login successful",
            "token": token,
            "token_type": "Bearer",
            "customer": customer.to_array("id", "name", "email", "phone"),
        }
    )


@customer_router.post("/logout")
@attempt("Could not sign out. Please try again.")
def customer_logout(request: Request, db: Db, customer: Shopper):
    token = current_token(request)
    if token is not None:
        db.delete(token)
        db.commit()
    return respond({"message": "Logged out"})


@customer_router.get("/profile")
def customer_profile(customer: Shopper):
    return ok(
        customer.to_array(
            "id", "name", "name_kh", "email", "phone", "province", "referral_code", "points_balance", "qualifying_points"
        ),
        "Profile retrieved successfully",
    )


routers = [staff_router, customer_router]

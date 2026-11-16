"""The points ledger. Rows are only ever added, so there are no update or delete routes."""

from typing import Annotated

from fastapi import APIRouter
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import selectinload

from app.auth import Db, Shopper, pages
from app.http import Input, attempt, from_day, ok, page_of, until_day
from app.models import Customer, LoyaltyTransaction, User
from app.services.loyalty import record_points
from app.services.records import log_activity
from app.validation import Exists, In, validate

router = APIRouter(prefix="/backend/points", tags=["points"])
customer_router = APIRouter(tags=["customer account"])

PointsStaff = Annotated[User, pages("points")]


def matching_customers(statement: Select, search: str) -> Select:
    """Customers whose name or email contains the search, or whose id it is."""
    term = f"%{search.lower()}%"
    conditions = [func.lower(Customer.name).like(term), func.lower(Customer.email).like(term)]
    if search.isdigit():
        conditions.append(Customer.id == int(search))
    return statement.where(or_(*conditions))


@router.get("/transactions")
@attempt("Could not load the points history.")
def transactions(db: Db, staff: PointsStaff, data: Input):
    filters = validate(
        data,
        {
            "search": ["nullable", "string", "max:255"],
            "type": ["nullable", In(LoyaltyTransaction.TYPES)],
            "direction": ["nullable", In(["earn", "deduct"])],
            "date_from": ["nullable", "date"],
            "date_to": ["nullable", "date", "after_or_equal:date_from"],
        },
    )
    statement = (
        select(LoyaltyTransaction)
        .options(selectinload(LoyaltyTransaction.customer), selectinload(LoyaltyTransaction.creator))
        .order_by(LoyaltyTransaction.created_at.desc(), LoyaltyTransaction.id.desc())
    )
    if filters.get("search"):
        statement = statement.where(
            LoyaltyTransaction.customer_id.in_(matching_customers(select(Customer.id), filters["search"]))
        )
    if filters.get("type"):
        statement = statement.where(LoyaltyTransaction.type == filters["type"])
    if filters.get("direction"):
        statement = statement.where(
            LoyaltyTransaction.points > 0 if filters["direction"] == "earn" else LoyaltyTransaction.points < 0
        )
    if filters.get("date_from"):
        statement = statement.where(from_day(LoyaltyTransaction.created_at, filters["date_from"]))
    if filters.get("date_to"):
        statement = statement.where(until_day(LoyaltyTransaction.created_at, filters["date_to"]))
    return page_of(db, statement, data, "Points history retrieved successfully", lambda row: row.with_people())


@router.get("/customers")
@attempt("Could not load customers.")
def customers(db: Db, staff: PointsStaff, data: Input):
    """Customers for the adjustment form's picker, with their balance and tier."""
    filters = validate(data, {"search": ["nullable", "string", "max:255"]})
    statement = select(Customer).options(selectinload(Customer.tier)).order_by(Customer.name, Customer.id)
    if filters.get("search"):
        statement = matching_customers(statement, filters["search"])

    def shape(customer: Customer):
        return {
            **customer.to_array("id", "tier_id", "name", "email", "points_balance"),
            "tier": customer.tier.to_array("id", "name") if customer.tier else None,
        }

    return page_of(db, statement, data, "Customers retrieved successfully", shape)


@router.post("/adjustments", status_code=201)
@attempt("Could not adjust the points.")
def adjust(db: Db, staff: PointsStaff, data: Input):
    fields = validate(
        data,
        {
            "customer_id": ["required", "integer", Exists(lambda value: bool(db.get(Customer, int(value))))],
            "points": ["required", "integer", "not_in:0", "between:-1000000,1000000"],
            "reason": ["required", "string", "max:255"],
        },
        {
            "customer_id.required": "Choose a customer.",
            "customer_id.exists": "That customer no longer exists.",
            "points.required": "Enter the points to add or subtract.",
            "points.integer": "Points must be a whole number.",
            "points.not_in": "Points cannot be zero.",
            "reason.required": "Enter a reason for the adjustment.",
        },
    )
    customer = db.get(Customer, fields["customer_id"])
    points, reason = fields["points"], fields["reason"]
    transaction = record_points(
        db, customer, LoyaltyTransaction.TYPE_ADJUST, points, f"Manual adjustment: {reason}", created_by=staff.id
    )
    log_activity(db, "POINTS_ADJUSTED", f"{points:+d} points for {customer.name}: {reason}", customer, staff)
    db.commit()
    db.refresh(transaction)
    return ok(transaction.with_people(), "Points adjusted successfully", 201)


@customer_router.get("/points")
@attempt("Could not load your points history.")
def history(db: Db, customer: Shopper, data: Input):
    """The signed-in customer's own points history, newest first."""
    statement = (
        select(LoyaltyTransaction)
        .where(LoyaltyTransaction.customer_id == customer.id)
        .order_by(LoyaltyTransaction.created_at.desc(), LoyaltyTransaction.id.desc())
    )
    columns = ("id", "customer_id", "order_id", "type", "points", "balance_before", "balance_after", "reason", "created_at")
    return page_of(db, statement, data, "Points history retrieved successfully", lambda row: row.to_array(*columns))


routers = [router, customer_router]

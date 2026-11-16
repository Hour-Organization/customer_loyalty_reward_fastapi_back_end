"""Customers, orders and referrals (which the dashboards also read), the activity log, reports and settings."""

from typing import Annotated

from fastapi import APIRouter
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.auth import Db, SuperAdmin, pages, revoke_tokens
from app.http import Input, attempt, find_or_404, ok, page_of
from app.models import ActivityLog, Customer, LoyaltyTransaction, Order, Redemption, Referral, Setting, Tier, User
from app.services import loyalty, orders
from app.services.records import log_activity, put_setting
from app.shapes import customer_api
from app.validation import Exists, In, validate

router = APIRouter(prefix="/backend", tags=["operations"])

CustomerReaders = Annotated[
    User, pages("customers", "users", "dashboard", "analytics", "logs", "referrals", "fulfillment")
]
CustomersStaff = Annotated[User, pages("customers")]
OrderReaders = Annotated[User, pages("orders", "customers", "dashboard", "analytics")]
OrdersStaff = Annotated[User, pages("orders")]
ReferralReaders = Annotated[User, pages("referrals", "analytics")]
LogReaders = Annotated[User, pages("logs", "dashboard")]
ReportReaders = Annotated[User, pages("dashboard", "analytics")]


def customer_query():
    return select(Customer).options(selectinload(Customer.tier), selectinload(Customer.badges))


# ---------------------------------------------------------------- Customers


@router.get("/customers")
@attempt("Could not load customers.")
def list_customers(db: Db, staff: CustomerReaders, data: Input):
    filters = validate(data, {"search": ["nullable", "string", "max:255"]})
    statement = customer_query().order_by(Customer.id.desc())
    if filters.get("search"):
        term = f"%{filters['search'].lower()}%"
        statement = statement.where(or_(func.lower(Customer.name).like(term), func.lower(Customer.email).like(term)))
    return page_of(db, statement, data, "Customers retrieved successfully", lambda customer: customer_api(db, customer))


@router.patch("/customers/{customer_id:int}/tier")
@attempt("Could not change the tier.")
def update_tier(customer_id: int, db: Db, staff: CustomersStaff, data: Input):
    """Lock the customer to a tier, or with no tier let their points decide it again."""
    customer = find_or_404(db, Customer, customer_id)
    fields = validate(
        data,
        {
            "tier": ["nullable", "string", Exists(lambda name: bool(db.scalar(select(Tier.id).where(Tier.name == name))))],
            "reason": ["required", "string", "max:255"],
        },
    )
    tier = db.scalars(select(Tier).where(Tier.name == fields["tier"])).one() if fields.get("tier") else None
    loyalty.override_tier(db, customer, tier, fields["reason"], staff)
    db.commit()
    db.refresh(customer)
    return ok(customer_api(db, customer), "Tier updated successfully")


@router.patch("/customers/{customer_id:int}/status")
@attempt("Could not change the customer.")
def update_customer_status(customer_id: int, db: Db, staff: SuperAdmin, data: Input):
    customer = find_or_404(db, Customer, customer_id)
    customer.is_active = validate(data, {"is_active": ["required", "boolean"]})["is_active"]
    if not customer.is_active:
        revoke_tokens(db, customer)
    log_activity(db, "CUSTOMER_STATUS", f"{'Activated' if customer.is_active else 'Deactivated'} {customer.name}", customer, staff)
    db.commit()
    return ok(customer_api(db, customer), "Customer activated" if customer.is_active else "Customer deactivated")


# ---------------------------------------------------------------- Orders


@router.get("/orders")
@attempt("Could not load orders.")
def list_orders(db: Db, staff: OrderReaders, data: Input):
    filters = validate(
        data, {"status": ["nullable", In(Order.TRANSITIONS)], "customer_id": ["nullable", "integer"]}
    )
    statement = select(Order).options(*orders.with_relations()).order_by(Order.id.desc())
    if filters.get("status"):
        statement = statement.where(Order.status == filters["status"])
    if filters.get("customer_id"):
        statement = statement.where(Order.customer_id == filters["customer_id"])
    return page_of(db, statement, data, "Orders retrieved successfully", lambda order: orders.to_api(db, order))


@router.patch("/orders/{order_id:int}/status")
@attempt("Could not update the order.")
def update_order_status(order_id: int, db: Db, staff: OrdersStaff, data: Input):
    order = find_or_404(db, Order, order_id)
    status = validate(data, {"status": ["required", In(Order.TRANSITIONS)]})["status"]
    order = orders.transition(db, order, status, staff)
    db.commit()
    return ok(orders.to_api(db, order), "Order updated successfully")


# ---------------------------------------------------------------- Referrals, activity log and reports


@router.get("/referrals")
@attempt("Could not load referrals.")
def list_referrals(db: Db, staff: ReferralReaders, data: Input):
    statement = (
        select(Referral)
        .options(selectinload(Referral.referrer), selectinload(Referral.referred))
        .order_by(Referral.id.desc())
    )
    return page_of(db, statement, data, "Referrals retrieved successfully", lambda referral: referral.to_api())


@router.get("/activity-logs")
@attempt("Could not load the activity log.")
def activity_logs(db: Db, staff: LogReaders, data: Input):
    filters = validate(data, {"event": ["nullable", "string", "max:255"], "search": ["nullable", "string", "max:255"]})
    statement = (
        select(ActivityLog)
        .options(selectinload(ActivityLog.customer), selectinload(ActivityLog.user))
        .order_by(ActivityLog.id.desc())
    )
    if filters.get("event"):
        statement = statement.where(ActivityLog.event == filters["event"])
    if filters.get("search"):
        statement = statement.where(func.lower(ActivityLog.description).like(f"%{filters['search'].lower()}%"))
    return page_of(db, statement, data, "Activity log retrieved successfully", lambda entry: entry.to_api())


@router.get("/reports/summary")
@attempt("Could not load the summary.")
def summary(db: Db, staff: ReportReaders):
    """Totals the dashboards show, for staff who cannot read the points ledger themselves."""
    issued = db.scalar(select(func.coalesce(func.sum(LoyaltyTransaction.points), 0)).where(LoyaltyTransaction.points > 0))
    spent = db.scalar(
        select(func.coalesce(func.sum(LoyaltyTransaction.points), 0)).where(
            LoyaltyTransaction.type == LoyaltyTransaction.TYPE_SPEND
        )
    )
    return ok(
        {
            "points_issued": int(issued),
            "points_redeemed": -int(spent),
            "rewards_redeemed": db.scalar(select(func.count(Redemption.id))),
        },
        "Summary retrieved successfully",
    )


@router.put("/settings/exchange-rate")
@attempt("Could not save the exchange rate.")
def update_exchange_rate(db: Db, staff: SuperAdmin, data: Input):
    rate = validate(data, {"rate": ["required", "integer", "min:1", "max:1000000000"]})["rate"]
    put_setting(db, Setting.EXCHANGE_RATE, rate)
    log_activity(db, "EXCHANGE_RATE_UPDATED", f"Changed the exchange rate to {rate} KHR", staff=staff)
    db.commit()
    return ok({"exchange_rate": rate}, "Exchange rate saved successfully")


routers = [router]

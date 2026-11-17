"""The signed-in customer: their account, profile, checkout and its payment, redemptions and restock requests."""

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.auth import Db, Shopper
from app.http import Input, attempt, done, not_found, ok
from app.models import Order, Payment, Redemption, Referral, RestockRequest, Reward, TierHistory
from app.services import orders, payments, redemptions
from app.services.payments import FakeKhqr
from app.services.records import log_activity
from app.shapes import customer_api
from app.validation import In, Regex, ValidationError, validate

router = APIRouter(tags=["customer account"])


@router.get("/account")
@attempt("Could not load your account.")
def account(db: Db, customer: Shopper):
    """Everything about the signed-in customer. Overdue reservations are expired first."""
    redemptions.expire_overdue(db)
    db.commit()
    db.refresh(customer)
    mine = customer.id
    return ok(
        {
            "profile": customer_api(db, customer),
            "orders": [
                orders.to_api(db, order)
                for order in db.scalars(
                    select(Order).options(*orders.with_relations()).where(Order.customer_id == mine).order_by(Order.id.desc())
                )
            ],
            "redemptions": [
                redemption.to_api()
                for redemption in db.scalars(
                    select(Redemption)
                    .options(selectinload(Redemption.reward), selectinload(Redemption.variant))
                    .where(Redemption.customer_id == mine)
                    .order_by(Redemption.id.desc())
                )
            ],
            "restock_requests": [
                request.to_api()
                for request in db.scalars(
                    select(RestockRequest).where(RestockRequest.customer_id == mine).order_by(RestockRequest.id.desc())
                )
            ],
            "referrals": [
                referral.to_api()
                for referral in db.scalars(
                    select(Referral)
                    .options(selectinload(Referral.referrer), selectinload(Referral.referred))
                    .where(Referral.referrer_id == mine)
                    .order_by(Referral.id.desc())
                )
            ],
            "tier_history": [
                change.to_api()
                for change in db.scalars(
                    select(TierHistory)
                    .options(selectinload(TierHistory.from_tier), selectinload(TierHistory.to_tier))
                    .where(TierHistory.customer_id == mine)
                    .order_by(TierHistory.id.desc())
                )
            ],
        },
        "Account retrieved successfully",
    )


@router.patch("/profile")
@attempt("Could not update your profile.")
def update_profile(db: Db, customer: Shopper, data: Input):
    """Customers can change their own details, but not their points."""
    fields = validate(
        data,
        {
            "name": ["sometimes", "required", "string", "max:255"],
            "name_kh": ["sometimes", "nullable", "string", "max:255"],
            "phone": ["sometimes", "nullable", "string", "max:32"],
            "province": ["sometimes", "nullable", "string", "max:255"],
        },
    )
    for name, value in fields.items():
        setattr(customer, name, value)
    db.commit()
    return ok(customer_api(db, customer), "Profile updated successfully")


@router.post("/checkout", status_code=201)
@attempt("Could not place your order. Please try again.")
def checkout(db: Db, customer: Shopper, data: Input):
    fields = validate(
        data,
        {
            "items": ["required", "array", "min:1"],
            "items.*.product_id": ["required", "integer"],
            "items.*.quantity": ["required", "integer", "min:1", "max:1000"],
            "address": ["required", "string", "max:255"],
            "phone": ["required", "string", Regex(r"^\+?\d[\d\s-]{7,14}$")],
            "payment_method": ["nullable", In(Payment.METHODS)],
        },
        {
            "items.required": "Your cart is empty.",
            "items.min": "Your cart is empty.",
            "address.required": "Enter a delivery address and phone number.",
            "phone.required": "Enter a delivery address and phone number.",
            "phone.regex": "Enter a valid phone number.",
            "payment_method.in": "Choose cash on delivery or KHQR.",
        },
    )
    method = fields.get("payment_method") or Payment.CASH_ON_DELIVERY
    order = orders.place(db, customer, fields["items"], fields["address"], fields["phone"], method)
    db.commit()
    return ok(orders.to_api(db, order), "Order placed successfully", 201)


def _own_order(db: Db, customer: Shopper, order_id: int) -> Order:
    order = db.scalars(
        select(Order).options(*orders.with_relations()).where(Order.id == order_id, Order.customer_id == customer.id)
    ).first()
    if order is None:
        raise not_found("Order")
    return order


@router.get("/orders/{order_id:int}/payment")
@attempt("Could not check the payment.")
def payment_status(order_id: int, db: Db, customer: Shopper):
    """The order with its payment brought up to date: the page showing the KHQR polls this."""
    order = _own_order(db, customer, order_id)
    payments.refresh(db, order)
    db.commit()
    db.refresh(order)
    return ok(orders.to_api(db, order), "Payment retrieved successfully")


@router.post("/orders/{order_id:int}/payment/simulate")
@attempt("Could not simulate the payment.")
def simulate_payment(order_id: int, db: Db, customer: Shopper):
    """Development only: pay the order's KHQR as a banking app would. Missing unless the fake provider is on."""
    if payments.configured() != FakeKhqr.name:
        raise HTTPException(404)
    order = _own_order(db, customer, order_id)
    if order.payment and order.payment.provider == FakeKhqr.name and order.payment.provider_ref:
        FakeKhqr.pay(order.payment.provider_ref)
    payments.refresh(db, order)
    db.commit()
    db.refresh(order)
    return ok(orders.to_api(db, order), "Payment simulated successfully")


@router.post("/redemptions", status_code=201)
@attempt("Could not redeem the reward.")
def redeem(db: Db, customer: Shopper, data: Input):
    fields = validate(data, {"reward_id": ["required", "integer"], "variant_id": ["nullable", "integer"]})
    redemption = redemptions.redeem(db, customer, fields["reward_id"], fields.get("variant_id"))
    db.commit()
    db.refresh(redemption)
    return ok(redemption.to_api(), "Reward redeemed successfully", 201)


@router.post("/redemptions/{redemption_id:int}/cancel")
@attempt("Could not cancel the redemption.")
def cancel_mine(redemption_id: int, db: Db, customer: Shopper):
    own = db.scalars(
        select(Redemption).where(Redemption.id == redemption_id, Redemption.customer_id == customer.id)
    ).first()
    if own is None:
        raise HTTPException(404, f"No query results for model [Redemption] {redemption_id}")
    redemption = redemptions.cancel(db, own, "Cancelled by customer")
    db.commit()
    return ok(redemption.to_api(), "Redemption cancelled")


@router.post("/restock-requests", status_code=201)
@attempt("Could not save your request.")
def request_restock(db: Db, customer: Shopper, data: Input):
    """Ask to hear when a sold-out reward is back in stock."""
    fields = validate(data, {"reward_id": ["required", "integer"], "variant_id": ["nullable", "integer"]})
    reward = db.scalars(select(Reward).options(selectinload(Reward.variants)).where(Reward.id == fields["reward_id"])).first()
    if reward is None:
        raise HTTPException(404, f"No query results for model [Reward] {fields['reward_id']}")
    variant_id = fields.get("variant_id")
    if variant_id not in reward.variant_ids():
        raise ValidationError({"variant_id": ["Choose an option for this reward."]})
    waiting = select(RestockRequest.id).where(
        RestockRequest.customer_id == customer.id,
        RestockRequest.reward_id == reward.id,
        RestockRequest.variant_id.is_(None) if variant_id is None else RestockRequest.variant_id == variant_id,
        RestockRequest.notified_at.is_(None),
    )
    if db.scalar(waiting):
        raise ValidationError({"reward_id": ["You will already be notified when this is back in stock."]})
    request = RestockRequest(customer_id=customer.id, reward_id=reward.id, variant_id=variant_id)
    db.add(request)
    db.flush()
    log_activity(db, "RESTOCK_REQUESTED", f"Asked to be notified when {reward.name} is back in stock", customer)
    db.commit()
    return ok(request.to_api(), "We will let you know when it is back in stock", 201)


@router.delete("/restock-requests/{request_id:int}")
@attempt("Could not remove your request.")
def dismiss_restock(request_id: int, db: Db, customer: Shopper):
    request = db.scalars(
        select(RestockRequest).where(RestockRequest.id == request_id, RestockRequest.customer_id == customer.id)
    ).first()
    if request is None:
        raise HTTPException(404, f"No query results for model [RestockRequest] {request_id}")
    db.delete(request)
    db.commit()
    return done("Request removed")


routers = [router]

"""Spending points on rewards: reserving a unit, backorders, fulfilment, returns, cancellations and expiry."""

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import clock
from app.models import (
    Customer,
    LoyaltyTransaction,
    Redemption,
    RestockRequest,
    Reward,
    RewardStockMovement,
    Setting,
    StockLocation,
    User,
)
from app.services import stock_ledger
from app.services.loyalty import record_points
from app.services.records import log_activity, setting_int
from app.validation import fail as _fail


def fail(message: str):
    _fail("redemption", message)


def redeem(db: Session, customer: Customer, reward_id: int, variant_id: int | None) -> Redemption:
    """Spend the points and reserve a unit at the best-stocked location, or backorder it when allowed."""
    reward = db.scalars(
        select(Reward)
        .where(Reward.id == reward_id)
        .options(selectinload(Reward.stock_item), selectinload(Reward.variants), selectinload(Reward.min_tier))
        .with_for_update()
    ).first()
    db.refresh(customer, with_for_update=True)
    if reward is None or not reward.is_active or (reward.expires_at and reward.expires_at < clock.today()):
        fail("Reward unavailable.")
    if variant_id not in reward.variant_ids():
        fail("Choose an option for this reward.")
    already_open = db.scalars(
        select(Redemption.id).where(
            Redemption.customer_id == customer.id,
            Redemption.reward_id == reward.id,
            Redemption.status.in_(Redemption.OPEN),
        )
    ).first()
    if already_open:
        fail("You already redeemed this reward.")
    if customer.points_balance < reward.points_cost:
        fail("Insufficient points.")
    if reward.min_tier and (customer.tier.min_points if customer.tier else 0) < reward.min_tier.min_points:
        fail(f"Requires {reward.min_tier.name} tier.")
    location_id = stock_ledger.best_location(db, reward.id, variant_id)
    if not location_id and not reward.stock_settings().allow_backorder:
        fail("Reward is out of stock.")

    redemption = Redemption(
        customer_id=customer.id,
        reward_id=reward.id,
        variant_id=variant_id,
        location_id=location_id,
        points=reward.points_cost,
        status=Redemption.RESERVED if location_id else Redemption.BACKORDERED,
        fulfillment=Redemption.NOT_FULFILLED,
        reserved_until=_reservation_end(db) if location_id else None,
    )
    db.add(redemption)
    db.flush()
    name = redemption.reward_name()
    record_points(db, customer, LoyaltyTransaction.TYPE_SPEND, -reward.points_cost, f"Redeemed: {name}")
    if location_id:
        stock_ledger.hold(db, redemption, "RESERVE", "Online redemption")
    suffix = "" if location_id else " (backordered)"
    log_activity(db, "REWARD_REDEEMED", f"Redeemed {name} for {reward.points_cost} points{suffix}", customer)
    return redemption


def cancel(db: Session, redemption: Redemption, reason: str, staff: User | None = None) -> Redemption:
    """Cancel an open redemption, releasing its unit and refunding the points."""
    redemption = _lock(db, redemption.id)
    if redemption.status not in Redemption.OPEN:
        fail("Only reserved or backordered redemptions can be cancelled.")
    _close(db, redemption, Redemption.CANCELLED, reason, staff)
    settle(db)
    if staff:
        log_activity(db, "REDEMPTION_CANCELLED", f"Cancelled {redemption.reward_name()}: {reason}", redemption.customer, staff)
    db.refresh(redemption)
    return redemption


def fulfill(db: Session, redemption: Redemption, method: str, serial: str | None, staff: User) -> Redemption:
    """Hand the reserved unit to the customer, by shipping it or at pickup."""
    redemption = _lock(db, redemption.id)
    if redemption.status != Redemption.RESERVED or not redemption.location_id:
        fail("Only reserved redemptions can be fulfilled.")
    shipped = method == Redemption.SHIPPED
    part = stock_ledger.fulfill(db, staff, redemption, "Shipped to customer" if shipped else "Picked up by customer", serial)
    redemption.status = Redemption.FULFILLED
    redemption.fulfillment = method
    redemption.fulfilled_at = clock.now()
    redemption.batch = part.get("batch")
    redemption.serial = (part.get("serials") or [None])[0]
    db.flush()
    customer = redemption.customer
    log_activity(
        db,
        "REDEMPTION_FULFILLED",
        f"{'Shipped' if shipped else 'Handed over'} {redemption.reward_name()} to {customer.name if customer else ''}",
        customer,
        staff,
    )
    return redemption


def return_to_stock(db: Session, actor: User, data: dict[str, Any]) -> list[RewardStockMovement]:
    """Put a fulfilled redemption's unit back in stock, closing it, and refund its points when asked."""
    redemption = _lock(db, data["redemption_id"])
    refund = bool(data.get("refund_points", False))
    if redemption.status != Redemption.FULFILLED:
        fail("Only fulfilled redemptions can be returned.")
    if refund and not actor.has_stock_authority("manager"):
        fail("Refunding points needs stock manager authority.")
    movements = stock_ledger.return_stock(
        db,
        actor,
        {
            **data,
            "reward_id": redemption.reward_id,
            "variant_id": redemption.variant_id,
            "quantity": 1,
            "batch": redemption.batch if redemption.batch is not None else data.get("batch"),
            "serials": [redemption.serial] if redemption.serial else (data.get("serials") or []),
            "reference": redemption.reference(),
        },
    )
    if refund:
        _refund(db, redemption, "Returned to stock")
    redemption.status = Redemption.RETURNED
    redemption.closed_at = clock.now()
    redemption.close_reason = data["reason"]
    db.flush()
    log_activity(db, "STOCK_RETURNED", f"Returned {redemption.reward_name()}: {data['reason']}", redemption.customer, actor)
    return movements


def extend(db: Session, redemption: Redemption, hours: int, staff: User) -> Redemption:
    redemption = _lock(db, redemption.id)
    if redemption.status != Redemption.RESERVED:
        fail("Only reserved redemptions can be extended.")
    current = clock.now()
    start = redemption.reserved_until if redemption.reserved_until and redemption.reserved_until > current else current
    redemption.reserved_until = start + timedelta(hours=hours)
    db.flush()
    log_activity(
        db, "RESERVATION_EXTENDED", f"Extended {redemption.reward_name()} reservation by {hours}h", redemption.customer, staff
    )
    return redemption


def expire_overdue(db: Session) -> int:
    """Expire reservations whose time ran out, refunding their points."""
    overdue = list(
        db.scalars(
            select(Redemption)
            .where(Redemption.status == Redemption.RESERVED, Redemption.reserved_until < clock.now())
            .order_by(Redemption.id)
            .with_for_update()
        )
    )
    for redemption in overdue:
        _close(db, redemption, Redemption.EXPIRED, "Reservation timed out")
    if overdue:
        settle(db)
    return len(overdue)


def settle(db: Session) -> None:
    """Reserve units for backorders, oldest first, and flag restock requests whose reward is back."""
    backorders = db.scalars(
        select(Redemption).where(Redemption.status == Redemption.BACKORDERED).order_by(Redemption.id)
    )
    for redemption in list(backorders):
        location_id = stock_ledger.best_location(db, redemption.reward_id, redemption.variant_id)
        if not location_id:
            continue
        redemption.status = Redemption.RESERVED
        redemption.location_id = location_id
        redemption.reserved_until = _reservation_end(db)
        db.flush()
        stock_ledger.hold(db, redemption, "RESERVE", "Backorder allocated")
        location = db.scalar(select(StockLocation.name).where(StockLocation.id == location_id))
        log_activity(
            db,
            "BACKORDER_RESERVED",
            f"{redemption.reward_name()} is back in stock and reserved at {location}",
            redemption.customer,
        )

    waiting = db.scalars(select(RestockRequest).where(RestockRequest.notified_at.is_(None)).order_by(RestockRequest.id))
    for request in list(waiting):
        if request.reward and request.reward.is_active and stock_ledger.best_location(db, request.reward_id, request.variant_id):
            request.notified_at = clock.now()
            db.flush()
            log_activity(db, "RESTOCK_NOTIFIED", f"{request.reward.name} is back in stock", request.customer)


def _reservation_end(db: Session):
    return clock.now() + timedelta(hours=setting_int(db, Setting.RESERVATION_HOURS))


def _close(db: Session, redemption: Redemption, status: str, reason: str, staff: User | None = None) -> None:
    if redemption.status == Redemption.RESERVED and redemption.location_id:
        stock_ledger.hold(db, redemption, "RELEASE", reason, staff)
    _refund(db, redemption, reason)
    redemption.status = status
    redemption.closed_at = clock.now()
    redemption.close_reason = reason
    db.flush()


def _refund(db: Session, redemption: Redemption, reason: str) -> None:
    name = redemption.reward_name()
    record_points(db, redemption.customer, LoyaltyTransaction.TYPE_REFUND, redemption.points, f"Refund for {name}: {reason}")
    log_activity(db, "POINTS_REFUNDED", f"Refunded {redemption.points} points for {name} ({reason})", redemption.customer)


def _lock(db: Session, redemption_id: int) -> Redemption:
    redemption = db.scalars(select(Redemption).where(Redemption.id == redemption_id).with_for_update()).first()
    if redemption is None:
        from fastapi import HTTPException

        raise HTTPException(404, f"No query results for model [Redemption] {redemption_id}")
    db.refresh(redemption)
    return redemption

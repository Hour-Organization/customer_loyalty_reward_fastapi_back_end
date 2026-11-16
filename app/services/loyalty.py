"""The loyalty program: the points ledger, points for orders, tiers, badges and referral bonuses."""

import math
from decimal import Decimal

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app import clock
from app.models import (
    Badge,
    Campaign,
    Customer,
    CustomerBadge,
    LoyaltyTransaction,
    Order,
    Referral,
    Tier,
    TierHistory,
    User,
)
from app.services.records import log_activity
from app.validation import fail

REFERRER_BONUS = 100
REFERRED_BONUS = 50


def live_campaigns() -> Select:
    """Active campaigns running today."""
    today = clock.today()
    return select(Campaign).where(
        Campaign.is_active.is_(True),
        Campaign.start_date <= today,
        or_(Campaign.end_date.is_(None), Campaign.end_date >= today),
    )


def record_points(
    db: Session,
    customer: Customer,
    kind: str,
    points: int,
    reason: str | None = None,
    created_by: int | None = None,
    order_id: int | None = None,
) -> LoyaltyTransaction:
    """Add a row to the points ledger and move the balance. Earned points also count towards the tier."""
    db.flush()
    db.refresh(customer, with_for_update=True)
    before = customer.points_balance
    after = before + points
    if after < 0:
        fail("points", f"{customer.name} only has {before:,} points, so this would make the balance negative.")
    customer.points_balance = after
    if kind == LoyaltyTransaction.TYPE_EARN:
        customer.qualifying_points += points
    transaction = LoyaltyTransaction(
        customer_id=customer.id,
        created_by=created_by,
        order_id=order_id,
        type=kind,
        points=points,
        balance_before=before,
        balance_after=after,
        reason=reason,
    )
    db.add(transaction)
    db.flush()
    return transaction


def points_for(db: Session, order: Order, customer: Customer) -> int:
    """Base points times the product bonus and quantity, scaled by the tier and the best live campaign."""
    base = sum(item.base_points * float(item.bonus_multiplier) * item.quantity for item in order.items)
    tier = float(customer.tier.multiplier) if customer.tier else 1.0
    best = db.scalar(select(func.max(Campaign.multiplier)).select_from(live_campaigns().subquery()))
    campaign = max(1.0, float(best or 0))
    # Rounding first keeps floating point noise from losing a point.
    return math.floor(round(base * tier * campaign, 6))


def order_completed(db: Session, order: Order) -> None:
    """Pay the points for a completed order, then update the tier, the referral and the badges."""
    customer = db.get(Customer, order.customer_id)
    reference = order.reference()
    is_first_order = not db.scalar(
        select(func.count()).where(Order.customer_id == customer.id, Order.status == Order.COMPLETED, Order.id != order.id)
    )
    earned = points_for(db, order, customer)
    order.points_earned = earned
    customer.total_spent = (customer.total_spent or Decimal(0)) + order.total_amount
    if earned > 0:
        record_points(db, customer, LoyaltyTransaction.TYPE_EARN, earned, f"Completed order {reference}", order_id=order.id)
        log_activity(db, "POINTS_EARNED", f"Earned {earned} points from {reference}", customer)
    refresh_tier(db, customer, f"Completed order {reference}")
    if is_first_order:
        _convert_referral(db, customer)
    award_badges(db, customer)


def refresh_tier(db: Session, customer: Customer, reason: str) -> None:
    """Move the customer to the tier their qualifying points reach, unless staff locked it."""
    if customer.tier_locked:
        return
    tier = db.scalars(
        select(Tier).where(Tier.min_points <= customer.qualifying_points).order_by(Tier.min_points.desc())
    ).first()
    if tier:
        _move_tier(db, customer, tier, reason)


def override_tier(db: Session, customer: Customer, tier: Tier | None, reason: str, staff: User) -> None:
    """Lock the customer to a tier, or with no tier let their points decide it again."""
    customer.tier_locked = tier is not None
    if tier:
        _move_tier(db, customer, tier, reason, staff)
    else:
        refresh_tier(db, customer, reason)


def award_badges(db: Session, customer: Customer) -> None:
    """Award every badge whose threshold the customer has now reached."""
    joined = customer.created_at or clock.now()
    stats = {
        "orders": db.scalar(
            select(func.count()).where(Order.customer_id == customer.id, Order.status == Order.COMPLETED)
        ),
        "spend": float(customer.total_spent or 0),
        "referrals": db.scalar(
            select(func.count()).where(Referral.referrer_id == customer.id, Referral.is_completed.is_(True))
        ),
        "qualifyingPoints": int(customer.qualifying_points),
        "membershipDays": max((clock.now() - joined).days, 0),
    }
    earned = select(CustomerBadge.badge_id).where(CustomerBadge.customer_id == customer.id)
    badges = db.scalars(select(Badge).where(Badge.id.not_in(earned), Badge.metric.is_not(None)).order_by(Badge.id))
    for badge in list(badges):
        if badge.threshold is not None and stats.get(badge.metric, 0) >= badge.threshold:
            db.add(CustomerBadge(customer_id=customer.id, badge_id=badge.id))
            db.flush()
            log_activity(db, "BADGE_AWARDED", f"Earned badge: {badge.name}", customer)
    db.expire(customer, ["badges"])


def _convert_referral(db: Session, customer: Customer) -> None:
    """Pay both referral bonuses on the referred customer's first completed order."""
    referral = db.scalars(
        select(Referral).where(Referral.referred_id == customer.id, Referral.is_completed.is_(False))
    ).first()
    if referral is None or referral.referrer_id == customer.id:
        return
    referral.is_completed = True
    for recipient, bonus in ((referral.referrer, REFERRER_BONUS), (customer, REFERRED_BONUS)):
        record_points(db, recipient, LoyaltyTransaction.TYPE_REFERRAL, bonus, f"Referral conversion for {customer.name}")
        log_activity(db, "REFERRAL_CONVERTED", f"Earned {bonus} points from {customer.name}'s first order", recipient)
    award_badges(db, referral.referrer)


def _move_tier(db: Session, customer: Customer, tier: Tier, reason: str, staff: User | None = None) -> None:
    if customer.tier_id == tier.id:
        return
    previous = customer.tier
    db.add(TierHistory(customer_id=customer.id, from_tier_id=customer.tier_id, to_tier_id=tier.id, reason=reason))
    customer.tier_id = tier.id
    customer.tier = tier
    db.flush()
    log_activity(db, "TIER_CHANGED", f"{previous.name if previous else ''} → {tier.name}: {reason}", customer, staff)

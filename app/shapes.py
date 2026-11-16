"""Response shapes that need more than one row: customers with their totals, counts per row, rewards with stock."""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.models import Customer, Order, Reward
from app.serial import day, file_url, num
from app.services import stock_ledger


def counts(db: Session, column: InstrumentedAttribute, ids: Iterable[int], *where: Any) -> dict[int, int]:
    """How many rows point at each id through `column`, like Eloquent's withCount()."""
    ids = list(ids)
    if not ids:
        return {}
    model = column.class_
    statement = select(column, func.count()).where(column.in_(ids), *where).group_by(column)
    if hasattr(model, "deleted_at"):
        statement = statement.where(model.deleted_at.is_(None))
    return dict(db.execute(statement).all())


def customer_api(db: Session, customer: Customer) -> dict[str, Any]:
    """The customer with their tier, completed orders, last order date and badge codes."""
    completed = db.scalar(select(func.count()).where(Order.customer_id == customer.id, Order.status == Order.COMPLETED))
    last_order = db.scalar(select(func.max(Order.ordered_at)).where(Order.customer_id == customer.id))
    return {
        **customer.to_array(
            "id", "name", "name_kh", "email", "phone", "province", "referral_code",
            "points_balance", "qualifying_points", "is_active", "tier_locked",
        ),  # fmt: skip
        "avatar": file_url(customer.avatar),
        "total_spent": num(customer.total_spent),
        "tier": customer.tier.name if customer.tier else None,
        "joined_at": day(customer.created_at),
        "last_order_at": str(last_order)[:10] if last_order else None,
        "orders_count": int(completed or 0),
        "badges": [badge.code for badge in customer.badges],
    }


def reward_api(reward: Reward, available: dict[int, int]) -> dict[str, Any]:
    """The reward with its minimum tier and the units the ledger has available."""
    return {
        **reward.to_array(),
        "min_tier": reward.min_tier.to_array("id", "name") if reward.min_tier else None,
        "stock": available.get(reward.id, 0),
    }


def reward_with_stock(db: Session, reward: Reward) -> dict[str, Any]:
    return reward_api(reward, stock_ledger.available_by_reward(db, reward.id))

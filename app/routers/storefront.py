"""What guests can browse: the catalog, rewards, tiers, badges, campaigns and the exchange rate."""

from typing import Any

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.auth import Db
from app.http import attempt, ok
from app.models import Badge, Campaign, Category, Product, Reward, Setting, Tier
from app.serial import day
from app.services import stock_ledger
from app.services.loyalty import live_campaigns
from app.services.records import setting_int

router = APIRouter(tags=["storefront"])


def storefront_reward(reward: Reward, availability: dict[str, int]) -> dict[str, Any]:
    settings = reward.stock_settings()
    return {
        **reward.to_array(
            "id", "min_tier_id", "name", "name_kh", "description", "category", "points_cost", "image_url", "is_active"
        ),
        "expires_at": day(reward.expires_at),
        "min_tier": reward.min_tier.to_array("id", "name") if reward.min_tier else None,
        "stock": sum(units for name, units in availability.items() if name.startswith(f"{reward.id}|")),
        "stock_item": {
            "sku": settings.sku,
            "tracking": settings.tracking,
            "out_of_stock": settings.out_of_stock,
            "allow_backorder": bool(settings.allow_backorder),
        },
        "variants": [variant.to_api() for variant in reward.variants],
        # Units per option; the standard item of a reward without options is under "".
        "available": {
            ("" if variant_id is None else str(variant_id)): availability.get(
                f"{reward.id}|{'' if variant_id is None else variant_id}", 0
            )
            for variant_id in reward.variant_ids()
        },
    }


@router.get("/storefront")
@attempt("Could not load the store.")
def storefront(db: Db):
    availability = stock_ledger.availability(db)
    products = db.scalars(
        select(Product)
        .join(Product.category)
        .where(Category.is_active.is_(True))
        .options(selectinload(Product.category), selectinload(Product.images), selectinload(Product.inventories))
        .order_by(Product.id.desc())
    )
    rewards = db.scalars(
        select(Reward)
        .where(Reward.is_active.is_(True))
        .options(selectinload(Reward.min_tier), selectinload(Reward.stock_item), selectinload(Reward.variants))
        .order_by(Reward.id)
    )
    campaigns = db.scalars(live_campaigns().order_by(Campaign.sort_order, Campaign.id))
    return ok(
        {
            "categories": [
                category.to_array("id", "parent_id", "name", "name_kh", "description", "is_active")
                for category in db.scalars(select(Category).where(Category.is_active.is_(True)).order_by(Category.name))
            ],
            "products": [
                {
                    **product.to_api(hide=("cost_price", "reorder_level")),
                    "category": product.category.to_array("id", "name", "is_active"),
                }
                for product in products.unique()
            ],
            "rewards": [storefront_reward(reward, availability) for reward in rewards],
            "tiers": [
                tier.to_array("id", "name", "min_points", "max_points", "multiplier", "color")
                for tier in db.scalars(select(Tier).order_by(Tier.min_points))
            ],
            "badges": [
                badge.to_array("id", "code", "name", "name_kh", "description", "icon", "color", "metric", "threshold")
                for badge in db.scalars(select(Badge).order_by(Badge.id))
            ],
            "campaigns": [campaign.to_array() for campaign in campaigns],
            "exchange_rate": setting_int(db, Setting.EXCHANGE_RATE),
        },
        "Storefront retrieved successfully",
    )


routers = [router]

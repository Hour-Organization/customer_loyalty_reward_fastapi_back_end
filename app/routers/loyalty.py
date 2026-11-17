"""Loyalty setup: membership tiers, badges, campaigns (with home-page banners) and rewards."""

from typing import Annotated, Any

from fastapi import APIRouter, Request
from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app import storage
from app.auth import Db, pages
from app.http import (
    Input,
    attempt,
    done,
    find_or_404,
    method_of,
    ok,
    page_of,
    presence,
    refuse,
    update_route,
    uploads,
)
from app.models import (
    Badge,
    Campaign,
    Customer,
    CustomerBadge,
    Redemption,
    Reward,
    RewardStockMovement,
    Tier,
    TierHistory,
    User,
)
from app.services import stock_ledger
from app.services.loyalty import live_campaigns
from app.services.records import log_staff_change
from app.shapes import counts, reward_api, reward_with_stock
from app.validation import (
    Exists,
    In,
    Regex,
    Unique,
    Validator,
    parse_date,
    to_bool,
    validate,
)

router = APIRouter(prefix="/backend", tags=["loyalty setup"])
public_router = APIRouter(tags=["storefront"])

TiersStaff = Annotated[User, pages("tiers")]
BadgesStaff = Annotated[User, pages("badges")]
CampaignsStaff = Annotated[User, pages("campaigns")]
RewardsStaff = Annotated[User, pages("rewards")]

HEX_COLOR = r"^#[0-9A-Fa-f]{6}$"


def plural(word: str, count: int) -> str:
    return word if count == 1 else f"{word}s"


def as_dates(fields: dict[str, Any], *names: str) -> dict[str, Any]:
    for name in names:
        if fields.get(name) is not None:
            fields[name] = parse_date(fields[name])
    return fields


# ---------------------------------------------------------------- Tiers


def tier_api(db: Session, tier: Tier, customers_count: int | None = None) -> dict[str, Any]:
    if customers_count is None:
        customers_count = counts(db, Customer.tier_id, [tier.id]).get(tier.id, 0)
    return {**tier.to_array(), "customers_count": customers_count}


def tier_validator(db: Session, method: str, tier: Tier | None, data: dict[str, Any]) -> Validator:
    def unique_name(attribute, value, fail):
        statement = select(Tier.id).where(func.lower(Tier.name) == str(value).lower())
        if tier is not None:
            statement = statement.where(Tier.id != tier.id)
        if db.scalar(statement):
            fail(f"A tier named {value} already exists.")

    def ranges(validator: Validator) -> None:
        if validator.errors:
            return
        minimum = int(data["min_points"]) if "min_points" in data else (tier.min_points if tier else 0)
        maximum = data["max_points"] if "max_points" in data else (tier.max_points if tier else None)
        maximum = None if maximum is None else int(maximum)
        if maximum is not None and maximum <= minimum:
            validator.add_error("max_points", "The maximum points must be greater than the minimum points.")
            return
        others = select(Tier)
        if tier is not None:
            others = others.where(Tier.id != tier.id)
        if maximum is None and db.scalars(others.where(Tier.max_points.is_(None))).first():
            validator.add_error(
                "max_points", "Only one tier can have no upper limit. Give the other open-ended tier a maximum first."
            )
            return
        overlapping = others.where(or_(Tier.max_points.is_(None), Tier.max_points >= minimum))
        if maximum is not None:
            overlapping = overlapping.where(Tier.min_points <= maximum)
        overlap = db.scalars(overlapping.order_by(Tier.min_points)).first()
        if overlap:
            upper = "+" if overlap.max_points is None else f"–{overlap.max_points:,}"
            validator.add_error(
                "min_points", f"This point range overlaps the {overlap.name} tier ({overlap.min_points:,}{upper})."
            )

    rules = {
        "name": [presence(method), "string", "max:50", unique_name],
        "min_points": [presence(method), "integer", "min:0", "max:2000000000"],
        "max_points": ["nullable", "integer", "max:2000000000"],
        "multiplier": [presence(method), "numeric", "between:1,10"],
        "color": ["nullable", "string", Regex(HEX_COLOR)],
    }
    return Validator(data, rules, {"color.regex": "The color must be a hex color such as #C0C0C0."}).after(ranges)


@router.get("/tiers")
@attempt("Could not load tiers.")
def list_tiers(db: Db, staff: TiersStaff, data: Input):
    return page_of(
        db, select(Tier).order_by(Tier.min_points, Tier.id), data, "Tiers retrieved successfully", lambda tier: tier_api(db, tier)
    )


@router.post("/tiers", status_code=201)
@attempt("Could not create the tier.")
def create_tier(db: Db, staff: TiersStaff, data: Input):
    tier = Tier(**tier_validator(db, "POST", None, data).validate())
    db.add(tier)
    db.flush()
    log_staff_change(db, staff, "TIER", "CREATED", tier.name)
    db.commit()
    return ok(tier_api(db, tier), "Tier created successfully", 201)


@router.get("/tiers/{tier_id:int}")
@attempt("Could not load the tier.")
def show_tier(tier_id: int, db: Db, staff: TiersStaff):
    return ok(tier_api(db, find_or_404(db, Tier, tier_id)), "Tier retrieved successfully")


@update_route(router, "/tiers/{tier_id:int}")
@attempt("Could not update the tier.")
def update_tier(tier_id: int, request: Request, db: Db, staff: TiersStaff, data: Input):
    tier = find_or_404(db, Tier, tier_id)
    for name, value in tier_validator(db, method_of(request, data), tier, data).validate().items():
        setattr(tier, name, value)
    db.flush()
    log_staff_change(db, staff, "TIER", "UPDATED", tier.name)
    db.commit()
    return ok(tier_api(db, tier), "Tier updated successfully")


@router.delete("/tiers/{tier_id:int}")
@attempt("Could not delete the tier.")
def delete_tier(tier_id: int, db: Db, staff: TiersStaff):
    tier = find_or_404(db, Tier, tier_id)
    if db.scalar(select(func.count(Tier.id))) <= 1:
        return refuse("Keep at least one tier: new customers join the lowest tier.")
    customers = db.scalar(select(func.count()).where(Customer.tier_id == tier.id))
    if customers:
        return refuse(f"Move the {customers} {plural('customer', customers)} in {tier.name} to another tier first.")
    rewards = db.scalar(select(func.count()).where(Reward.min_tier_id == tier.id))
    if rewards:
        return refuse(f"Change the minimum tier of the {rewards} {plural('reward', rewards)} set to {tier.name} first.")
    in_history = db.scalar(
        select(exists().where(or_(TierHistory.from_tier_id == tier.id, TierHistory.to_tier_id == tier.id)))
    )
    if in_history:
        return refuse(f"{tier.name} appears in customers' tier history, so it cannot be deleted.")
    name = tier.name
    db.delete(tier)
    log_staff_change(db, staff, "TIER", "DELETED", name)
    db.commit()
    return done("Tier deleted successfully")


# ---------------------------------------------------------------- Badges


def badge_api(db: Session, badge: Badge) -> dict[str, Any]:
    return {**badge.to_array(), "customers_count": counts(db, CustomerBadge.badge_id, [badge.id]).get(badge.id, 0)}


def badge_fields(db: Session, method: str, badge: Badge | None, data: dict[str, Any]) -> dict[str, Any]:
    if badge is not None:
        code = ["prohibited"]
    else:
        code = [
            "required", "string", "max:50", Regex(r"^[A-Z0-9_]+$"),
            Unique(lambda value: bool(db.scalar(select(Badge.id).where(Badge.code == value)))),
        ]  # fmt: skip
    return validate(
        data,
        {
            "code": code,
            "name": [presence(method), "string", "max:100"],
            "name_kh": ["nullable", "string", "max:255"],
            "description": ["nullable", "string", "max:500"],
            "icon": ["nullable", In(Badge.ICONS)],
            "color": ["nullable", "string", Regex(HEX_COLOR)],
            "metric": [presence(method), In(Badge.METRICS)],
            "threshold": [presence(method), "integer", "min:1", "max:2000000000"],
        },
        {
            "code.regex": "The code may only use capital letters, numbers and underscores, such as FIRST_PURCHASE.",
            "code.unique": "A badge with this code already exists.",
            "code.prohibited": "A badge code cannot be changed once created.",
            "color.regex": "The color must be a hex color such as #F59E0B.",
            "metric.in": "Choose a condition: orders, spend, referrals, qualifying points or membership days.",
        },
    )


@router.get("/badges")
@attempt("Could not load badges.")
def list_badges(db: Db, staff: BadgesStaff, data: Input):
    return page_of(db, select(Badge).order_by(Badge.id), data, "Badges retrieved successfully", lambda badge: badge_api(db, badge))


@router.post("/badges", status_code=201)
@attempt("Could not create the badge.")
def create_badge(db: Db, staff: BadgesStaff, data: Input):
    badge = Badge(**badge_fields(db, "POST", None, data))
    db.add(badge)
    db.flush()
    log_staff_change(db, staff, "BADGE", "CREATED", badge.name)
    db.commit()
    return ok(badge_api(db, badge), "Badge created successfully", 201)


@router.get("/badges/{badge_id:int}")
@attempt("Could not load the badge.")
def show_badge(badge_id: int, db: Db, staff: BadgesStaff):
    return ok(badge_api(db, find_or_404(db, Badge, badge_id)), "Badge retrieved successfully")


@update_route(router, "/badges/{badge_id:int}")
@attempt("Could not update the badge.")
def update_badge(badge_id: int, request: Request, db: Db, staff: BadgesStaff, data: Input):
    badge = find_or_404(db, Badge, badge_id)
    for name, value in badge_fields(db, method_of(request, data), badge, data).items():
        if name != "code":
            setattr(badge, name, value)
    db.flush()
    log_staff_change(db, staff, "BADGE", "UPDATED", badge.name)
    db.commit()
    return ok(badge_api(db, badge), "Badge updated successfully")


@router.delete("/badges/{badge_id:int}")
@attempt("Could not delete the badge.")
def delete_badge(badge_id: int, db: Db, staff: BadgesStaff):
    badge = find_or_404(db, Badge, badge_id)
    name = badge.name
    # Customers who earned it lose it too (customer_badges cascades).
    db.execute(delete(CustomerBadge).where(CustomerBadge.badge_id == badge.id))
    db.delete(badge)
    log_staff_change(db, staff, "BADGE", "DELETED", name)
    db.commit()
    return done("Badge deleted successfully")


# ---------------------------------------------------------------- Campaigns

IMAGE_MESSAGES = {
    "image.url": "The image URL must start with http:// or https://.",
    "image_file.image": "The file must be an image.",
    "image_file.mimes": "Images must be JPG, PNG or WebP.",
    "image_file.max": "The image must be 4 MB or smaller.",
}


def campaign_fields(method: str, campaign: Campaign | None, data: dict[str, Any]) -> dict[str, Any]:
    # A PATCH without a start date checks the end date against the stored one.
    start = data.get("start_date", campaign.start_date.isoformat() if campaign and campaign.start_date else None)
    fields = validate(
        data,
        {
            "name": [presence(method), "string", "max:100"],
            "name_kh": ["nullable", "string", "max:255"],
            "description": ["nullable", "string", "max:1000"],
            "multiplier": [presence(method), "numeric", "between:1,10"],
            "start_date": [presence(method), "date"],
            "end_date": ["nullable", "date", *([f"after_or_equal:{start}"] if start else [])],
            "is_active": ["sometimes", "boolean"],
            "show_on_home": ["sometimes", "boolean"],
            "sort_order": ["sometimes", "integer", "min:0", "max:9999"],
            "banner_title": ["nullable", "string", "max:120"],
            "banner_subtitle": ["nullable", "string", "max:255"],
            "button_label": ["nullable", "string", "max:40"],
            "link_url": ["nullable", "string", "max:2048", Regex(r"^(/|https?://)")],
            "image": ["nullable", "url:http,https", "max:2048"],
            "image_file": ["nullable", "image", "mimes:jpg,jpeg,png,webp", "max:4096"],
        },
        {
            "end_date.after_or_equal": "The end date cannot be before the start date.",
            "link_url.regex": "The link must be a page path starting with / (such as /products) or an http(s) address.",
            **IMAGE_MESSAGES,
        },
    )
    return as_dates(fields, "start_date", "end_date")


def store_image(model: Campaign | Reward, fields: dict[str, Any], folder: str) -> None:
    files = uploads(fields, "image_file")
    if files:
        model.image = storage.store(files[0], f"{folder}/{model.id}")


@router.get("/campaigns")
@attempt("Could not load campaigns.")
def list_campaigns(db: Db, staff: CampaignsStaff, data: Input):
    statement = select(Campaign).order_by(Campaign.start_date.desc(), Campaign.id)
    return page_of(db, statement, data, "Campaigns retrieved successfully", lambda campaign: campaign.to_array())


@public_router.get("/campaigns/banners")
@attempt("Could not load banners.")
def banners(db: Db):
    """Home-page banners: live campaigns staff chose to show, in their display order. Public."""
    statement = live_campaigns().where(Campaign.show_on_home.is_(True)).order_by(Campaign.sort_order, Campaign.id)
    return ok([campaign.to_banner() for campaign in db.scalars(statement)], "Banners retrieved successfully")


@router.post("/campaigns", status_code=201)
@attempt("Could not create the campaign.")
def create_campaign(db: Db, staff: CampaignsStaff, data: Input):
    fields = campaign_fields("POST", None, data)
    campaign = Campaign(**{name: value for name, value in fields.items() if name != "image_file"})
    db.add(campaign)
    db.flush()
    store_image(campaign, fields, "campaigns")
    log_staff_change(db, staff, "CAMPAIGN", "CREATED", campaign.name)
    db.commit()
    db.refresh(campaign)
    return ok(campaign.to_array(), "Campaign created successfully", 201)


@router.get("/campaigns/{campaign_id:int}")
@attempt("Could not load the campaign.")
def show_campaign(campaign_id: int, db: Db, staff: CampaignsStaff):
    return ok(find_or_404(db, Campaign, campaign_id).to_array(), "Campaign retrieved successfully")


@update_route(router, "/campaigns/{campaign_id:int}")
@attempt("Could not update the campaign.")
def update_campaign(campaign_id: int, request: Request, db: Db, staff: CampaignsStaff, data: Input):
    campaign = find_or_404(db, Campaign, campaign_id)
    fields = campaign_fields(method_of(request, data), campaign, data)
    previous, had_upload = campaign.image, campaign.has_uploaded_image()
    for name, value in fields.items():
        if name != "image_file":
            setattr(campaign, name, value)
    store_image(campaign, fields, "campaigns")
    log_staff_change(db, staff, "CAMPAIGN", "UPDATED", campaign.name)
    db.commit()
    # Drop the old uploaded file once the campaign no longer uses it.
    if had_upload and previous != campaign.image:
        storage.delete(previous)
    db.refresh(campaign)
    return ok(campaign.to_array(), "Campaign updated successfully")


@router.patch("/campaigns/{campaign_id:int}/status")
@attempt("Could not change the campaign status.")
def campaign_status(campaign_id: int, db: Db, staff: CampaignsStaff, data: Input):
    campaign = find_or_404(db, Campaign, campaign_id)
    campaign.is_active = validate(data, {"is_active": ["required", "boolean"]})["is_active"]
    log_staff_change(db, staff, "CAMPAIGN", "UPDATE_STATUS", campaign.name)
    db.commit()
    return ok(campaign.to_array(), "Campaign activated" if campaign.is_active else "Campaign deactivated")


@router.delete("/campaigns/{campaign_id:int}")
@attempt("Could not delete the campaign.")
def delete_campaign(campaign_id: int, db: Db, staff: CampaignsStaff):
    campaign = find_or_404(db, Campaign, campaign_id)
    name, image, uploaded = campaign.name, campaign.image, campaign.has_uploaded_image()
    db.delete(campaign)
    log_staff_change(db, staff, "CAMPAIGN", "DELETED", name)
    db.commit()
    if uploaded:
        storage.delete(image)
    return done("Campaign deleted successfully")


# ---------------------------------------------------------------- Rewards


def reward_query():
    return select(Reward).options(selectinload(Reward.min_tier))


def reward_fields(db: Session, method: str, reward: Reward | None, data: dict[str, Any]) -> dict[str, Any]:
    # An already expired reward can still be edited without moving its date.
    keeps_expiry = reward is not None and data.get("expires_at") == (reward.expires_at.isoformat() if reward.expires_at else None)
    fields = validate(
        data,
        {
            "min_tier_id": [
                presence(method), "integer", Exists(lambda value: bool(db.get(Tier, int(value)))),
            ],  # fmt: skip
            "name": [presence(method), "string", "max:255"],
            "name_kh": ["nullable", "string", "max:255"],
            "description": ["nullable", "string", "max:2000"],
            "category": ["nullable", "string", "max:50"],
            "points_cost": [presence(method), "integer", "min:1", "max:2000000000"],
            "stock": ["prohibited"],
            "image": ["nullable", "url:http,https", "max:2048"],
            "image_file": ["nullable", "image", "mimes:jpg,jpeg,png,webp", "max:4096"],
            "is_active": ["sometimes", "boolean"],
            "expires_at": ["nullable", "date", *([] if keeps_expiry else ["after_or_equal:today"])],
        },
        {
            "min_tier_id.exists": "Choose a minimum tier that exists.",
            "stock.prohibited": "Stock comes from the ledger. Record a stock movement instead.",
            "expires_at.after_or_equal": "The expiry date cannot be in the past.",
            **IMAGE_MESSAGES,
        },
    )
    fields.pop("stock", None)
    return as_dates(fields, "expires_at")


@router.get("/rewards")
@attempt("Could not load rewards.")
def list_rewards(db: Db, staff: RewardsStaff, data: Input):
    filters = validate(data, {"is_active": ["nullable", "boolean"]})
    statement = reward_query().order_by(Reward.id)
    if filters.get("is_active") is not None:
        statement = statement.where(Reward.is_active.is_(to_bool(data["is_active"])))
    available = stock_ledger.available_by_reward(db)
    return page_of(db, statement, data, "Rewards retrieved successfully", lambda reward: reward_api(reward, available))


@router.get("/rewards/tier-options")
@attempt("Could not load tiers.")
def reward_tier_options(db: Db, staff: RewardsStaff):
    tiers = db.scalars(select(Tier).order_by(Tier.min_points))
    return ok([tier.to_array("id", "name") for tier in tiers], "Tiers retrieved successfully")


@router.post("/rewards", status_code=201)
@attempt("Could not create the reward.")
def create_reward(db: Db, staff: RewardsStaff, data: Input):
    fields = reward_fields(db, "POST", None, data)
    reward = Reward(**{name: value for name, value in fields.items() if name != "image_file"})
    db.add(reward)
    db.flush()
    store_image(reward, fields, "rewards")
    log_staff_change(db, staff, "REWARD", "CREATED", reward.name)
    db.commit()
    db.refresh(reward)
    return ok(reward_with_stock(db, reward), "Reward created successfully", 201)


@router.get("/rewards/{reward_id:int}")
@attempt("Could not load the reward.")
def show_reward(reward_id: int, db: Db, staff: RewardsStaff):
    return ok(reward_with_stock(db, find_or_404(db, Reward, reward_id)), "Reward retrieved successfully")


@update_route(router, "/rewards/{reward_id:int}")
@attempt("Could not update the reward.")
def update_reward(reward_id: int, request: Request, db: Db, staff: RewardsStaff, data: Input):
    reward = find_or_404(db, Reward, reward_id)
    fields = reward_fields(db, method_of(request, data), reward, data)
    previous, had_upload = reward.image, reward.has_uploaded_image()
    for name, value in fields.items():
        if name != "image_file":
            setattr(reward, name, value)
    store_image(reward, fields, "rewards")
    log_staff_change(db, staff, "REWARD", "UPDATED", reward.name)
    db.commit()
    # Drop the old uploaded file once the reward no longer uses it.
    if had_upload and previous != reward.image:
        storage.delete(previous)
    db.refresh(reward)
    return ok(reward_with_stock(db, reward), "Reward updated successfully")


@router.patch("/rewards/{reward_id:int}/status")
@attempt("Could not change the reward status.")
def reward_status(reward_id: int, db: Db, staff: RewardsStaff, data: Input):
    reward = find_or_404(db, Reward, reward_id)
    reward.is_active = validate(data, {"is_active": ["required", "boolean"]})["is_active"]
    log_staff_change(db, staff, "REWARD", "UPDATE_STATUS", reward.name)
    db.commit()
    return ok(reward_with_stock(db, reward), "Reward activated" if reward.is_active else "Reward deactivated")


@router.delete("/rewards/{reward_id:int}")
@attempt("Could not delete the reward.")
def delete_reward(reward_id: int, db: Db, staff: RewardsStaff):
    reward = find_or_404(db, Reward, reward_id)
    has_history = db.scalar(select(exists().where(Redemption.reward_id == reward.id))) or db.scalar(
        select(exists().where(RewardStockMovement.reward_id == reward.id))
    )
    if has_history:
        return refuse(f"{reward.name} has redemptions or stock history, so it cannot be deleted. Deactivate it instead.")
    name, image, uploaded = reward.name, reward.image, reward.has_uploaded_image()
    db.delete(reward)
    log_staff_change(db, staff, "REWARD", "DELETED", name)
    db.commit()
    if uploaded:
        storage.delete(image)
    return done("Reward deleted successfully")


routers = [router, public_router]

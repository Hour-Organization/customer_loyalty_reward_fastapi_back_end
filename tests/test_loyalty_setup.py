from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select

from app import clock
from app.config import get_settings
from app.models import CustomerBadge, Reward, TierHistory
from tests.factories import image


def headers(make, pages=None):
    return make.headers(make.staff(pages))


def stored(url: str) -> Path:
    return Path(get_settings().storage_dir) / url.split("/storage/", 1)[1]


# ---------------------------------------------------------------- Tiers


def test_tiers_are_listed_from_the_lowest_with_customer_counts(client, make):
    gold = make.tier(name="Gold", min_points=5000)
    make.tier(name="Bronze", min_points=0, max_points=999)
    make.customer(tier=gold)
    rows = client.get("/api/backend/tiers", headers=headers(make)).json()["data"]
    assert [(row["name"], row["customers_count"]) for row in rows] == [("Bronze", 0), ("Gold", 1)]
    assert rows[0]["multiplier"] == "1.00"


def test_tiers_are_created_and_updated_with_only_the_fields_sent(client, make):
    h = headers(make)
    created = client.post("/api/backend/tiers", headers=h, json={"name": "Silver", "min_points": 1000, "max_points": 4999, "multiplier": 1.25})
    assert created.status_code == 201
    tier = created.json()["data"]
    assert tier["multiplier"] == "1.25" and tier["customers_count"] == 0
    updated = client.patch(f"/api/backend/tiers/{tier['id']}", headers=h, json={"color": "#C0C0C0"})
    assert updated.json()["data"]["color"] == "#C0C0C0" and updated.json()["data"]["min_points"] == 1000


def test_tier_fields_are_validated(client, make):
    h = headers(make)
    errors = client.post("/api/backend/tiers", headers=h, json={"multiplier": 11, "color": "red"}).json()["errors"]
    assert errors["name"] == ["The name field is required."]
    assert errors["multiplier"] == ["The multiplier field must be between 1 and 10."]
    assert errors["color"] == ["The color must be a hex color such as #C0C0C0."]
    same = client.post("/api/backend/tiers", headers=h, json={"name": "A", "min_points": 100, "max_points": 100, "multiplier": 1})
    assert same.json()["errors"]["max_points"] == ["The maximum points must be greater than the minimum points."]


def test_tier_ranges_cannot_overlap_and_only_one_can_be_open_ended(client, make):
    make.tier(name="Silver", min_points=1000, max_points=4999)
    make.tier(name="Platinum", min_points=10000)
    h = headers(make)
    overlap = client.post("/api/backend/tiers", headers=h, json={"name": "Gold", "min_points": 4000, "max_points": 9999, "multiplier": 1.5})
    assert overlap.json()["errors"]["min_points"] == ["This point range overlaps the Silver tier (1,000–4,999)."]
    open_ended = client.post("/api/backend/tiers", headers=h, json={"name": "Elite", "min_points": 20000, "multiplier": 3})
    assert open_ended.json()["errors"]["max_points"][0].startswith("Only one tier can have no upper limit.")


def test_tiers_in_use_or_the_last_tier_cannot_be_deleted(client, make, db):
    h = headers(make)
    only = make.tier(name="Only")
    assert client.delete(f"/api/backend/tiers/{only.id}", headers=h).json()["message"] == (
        "Keep at least one tier: new customers join the lowest tier."
    )
    used = make.tier(name="Used", min_points=100)
    make.customer(tier=used)
    assert client.delete(f"/api/backend/tiers/{used.id}", headers=h).json()["message"] == (
        "Move the 1 customer in Used to another tier first."
    )
    rewarded = make.tier(name="Rewarded", min_points=200)
    make.reward(tier=rewarded)
    assert "Change the minimum tier of the 1 reward" in client.delete(f"/api/backend/tiers/{rewarded.id}", headers=h).json()["message"]
    historic = make.tier(name="Historic", min_points=300)
    customer = make.customer(tier=only)
    db.add(TierHistory(customer_id=customer.id, from_tier_id=historic.id, to_tier_id=only.id))
    db.commit()
    assert "tier history" in client.delete(f"/api/backend/tiers/{historic.id}", headers=h).json()["message"]
    unused = make.tier(name="Unused", min_points=400)
    assert client.delete(f"/api/backend/tiers/{unused.id}", headers=h).status_code == 200


def test_tiers_need_the_tiers_page(client, make):
    assert client.get("/api/backend/tiers", headers=headers(make, ["badges"])).status_code == 403


# ---------------------------------------------------------------- Badges


def test_badges_are_created_listed_and_their_code_cannot_change(client, make):
    h = headers(make)
    created = client.post(
        "/api/backend/badges", headers=h, json={"code": "FIRST_BUY", "name": "First", "metric": "orders", "threshold": 1, "icon": "cart"}
    )
    assert created.status_code == 201 and created.json()["data"]["customers_count"] == 0
    badge_id = created.json()["data"]["id"]
    changed = client.patch(f"/api/backend/badges/{badge_id}", headers=h, json={"code": "OTHER"})
    assert changed.json()["errors"]["code"] == ["A badge code cannot be changed once created."]
    renamed = client.patch(f"/api/backend/badges/{badge_id}", headers=h, json={"name": "First Purchase"})
    assert renamed.json()["data"]["name"] == "First Purchase" and renamed.json()["data"]["code"] == "FIRST_BUY"
    assert client.get("/api/backend/badges", headers=h).json()["meta"]["total"] == 1


def test_badge_fields_are_validated(client, make):
    make.badge(code="TAKEN")
    errors = client.post(
        "/api/backend/badges", headers=headers(make), json={"code": "TAKEN", "metric": "likes", "threshold": 0, "color": "#zzz"}
    ).json()["errors"]
    assert errors["code"] == ["A badge with this code already exists."]
    assert errors["metric"] == ["Choose a condition: orders, spend, referrals, qualifying points or membership days."]
    assert errors["threshold"] == ["The threshold field must be at least 1."]
    lower = client.post("/api/backend/badges", headers=headers(make), json={"code": "lower"}).json()["errors"]["code"]
    assert lower == ["The code may only use capital letters, numbers and underscores, such as FIRST_PURCHASE."]


def test_deleting_a_badge_takes_it_from_customers_who_earned_it(client, make, db):
    badge = make.badge()
    customer = make.customer()
    db.add(CustomerBadge(customer_id=customer.id, badge_id=badge.id))
    db.commit()
    assert client.delete(f"/api/backend/badges/{badge.id}", headers=headers(make)).status_code == 200
    assert db.scalar(select(func.count(CustomerBadge.id))) == 0


# ---------------------------------------------------------------- Campaigns


def test_campaign_is_created_with_an_uploaded_banner(client, make):
    response = client.post(
        "/api/backend/campaigns",
        headers=headers(make),
        data={"name": "Double Points", "multiplier": "2", "start_date": "2026-11-01", "end_date": "", "show_on_home": "1", "link_url": "/products"},
        files={"image_file": image()},
    )
    assert response.status_code == 201, response.json()
    data = response.json()["data"]
    assert data["multiplier"] == "2.00" and data["start_date"] == "2026-11-01" and data["end_date"] is None
    assert data["show_on_home"] is True
    assert stored(data["image_url"]).is_file()


def test_a_new_banner_replaces_the_old_file(client, make):
    h = headers(make)
    created = client.post(
        "/api/backend/campaigns", headers=h, data={"name": "A", "multiplier": "1", "start_date": "2026-11-01"}, files={"image_file": image()}
    ).json()["data"]
    old = stored(created["image_url"])
    updated = client.post(
        f"/api/backend/campaigns/{created['id']}", headers=h, data={"_method": "PATCH", "name": "B"}, files={"image_file": image("new.png")}
    ).json()["data"]
    assert updated["name"] == "B"
    assert not old.exists() and stored(updated["image_url"]).is_file()
    assert client.delete(f"/api/backend/campaigns/{created['id']}", headers=h).status_code == 200
    assert not stored(updated["image_url"]).exists()


def test_campaign_can_be_deactivated_and_reactivated(client, make):
    campaign = make.campaign()
    h = headers(make)
    off = client.patch(f"/api/backend/campaigns/{campaign.id}/status", headers=h, json={"is_active": False})
    assert off.json()["message"] == "Campaign deactivated" and off.json()["data"]["is_active"] is False
    on = client.patch(f"/api/backend/campaigns/{campaign.id}/status", headers=h, json={"is_active": True})
    assert on.json()["message"] == "Campaign activated"


def test_campaign_fields_are_validated(client, make):
    errors = client.post(
        "/api/backend/campaigns",
        headers=headers(make),
        json={"name": "A", "multiplier": 20, "start_date": "2026-11-10", "end_date": "2026-11-01", "link_url": "products", "image": "ftp://x"},
    ).json()["errors"]
    assert errors["multiplier"] == ["The multiplier field must be between 1 and 10."]
    assert errors["end_date"] == ["The end date cannot be before the start date."]
    assert errors["link_url"] == ["The link must be a page path starting with / (such as /products) or an http(s) address."]
    assert errors["image"] == ["The image URL must start with http:// or https://."]


def test_public_banners_list_only_live_home_page_campaigns_in_order(client, make):
    today = clock.today()
    make.campaign(name="Second", show_on_home=True, sort_order=2, image="https://example.com/2.jpg")
    make.campaign(name="First", show_on_home=True, sort_order=1)
    make.campaign(name="Hidden", show_on_home=False)
    make.campaign(name="Off", show_on_home=True, is_active=False)
    make.campaign(name="Future", show_on_home=True, start_date=today + timedelta(days=3))
    data = client.get("/api/campaigns/banners").json()["data"]
    assert [banner["name"] for banner in data] == ["First", "Second"]
    assert data[1]["image_url"] == "https://example.com/2.jpg"
    assert "is_active" not in data[0]


def test_campaigns_need_the_campaigns_page(client, make):
    assert client.get("/api/backend/campaigns", headers=headers(make, ["rewards"])).status_code == 403


# ---------------------------------------------------------------- Rewards


def test_rewards_are_listed_with_their_minimum_tier_and_ledger_stock(client, make):
    gold = make.tier(name="Gold")
    reward = make.reward(tier=gold, name="Voucher")
    staff = make.staff()
    active, closed = make.location(), make.location()
    make.receive(staff, reward, 5, active)
    make.receive(staff, reward, 3, active)
    make.receive(staff, reward, 4, closed)
    closed.is_active = False
    make.save(closed)
    rows = client.get("/api/backend/rewards", headers=make.headers(staff)).json()["data"]
    assert rows[0]["min_tier"] == {"id": gold.id, "name": "Gold"}
    assert rows[0]["stock"] == 8


def test_tier_options_list_tiers_from_the_lowest(client, make):
    make.tier(name="Gold", min_points=5000)
    make.tier(name="Bronze", min_points=0)
    data = client.get("/api/backend/rewards/tier-options", headers=headers(make)).json()["data"]
    assert [tier["name"] for tier in data] == ["Bronze", "Gold"]
    assert set(data[0]) == {"id", "name"}


def test_reward_is_created_with_an_image_and_a_new_image_replaces_it(client, make):
    tier = make.tier()
    h = headers(make)
    created = client.post(
        "/api/backend/rewards",
        headers=h,
        data={"min_tier_id": str(tier.id), "name": "Mug", "points_cost": "300", "is_active": "1", "expires_at": ""},
        files={"image_file": image()},
    )
    assert created.status_code == 201, created.json()
    data = created.json()["data"]
    assert data["stock"] == 0 and data["expires_at"] is None
    old = stored(data["image_url"])
    updated = client.post(
        f"/api/backend/rewards/{data['id']}", headers=h, data={"_method": "PATCH", "image": "https://example.com/mug.jpg"}
    ).json()["data"]
    assert updated["image_url"] == "https://example.com/mug.jpg" and not old.exists()


def test_reward_fields_are_validated_and_stock_comes_from_the_ledger(client, make):
    errors = client.post(
        "/api/backend/rewards", headers=headers(make), json={"min_tier_id": 999, "points_cost": 0, "stock": 5, "expires_at": "2000-01-01"}
    ).json()["errors"]
    assert errors["min_tier_id"] == ["Choose a minimum tier that exists."]
    assert errors["points_cost"] == ["The points cost field must be at least 1."]
    assert errors["stock"] == ["Stock comes from the ledger. Record a stock movement instead."]
    assert errors["expires_at"] == ["The expiry date cannot be in the past."]


def test_an_expired_reward_can_be_edited_without_moving_its_date(client, make):
    reward = make.reward(expires_at=clock.today() - timedelta(days=3))
    response = client.patch(
        f"/api/backend/rewards/{reward.id}", headers=headers(make), json={"name": "Renamed", "expires_at": reward.expires_at.isoformat()}
    )
    assert response.status_code == 200 and response.json()["data"]["name"] == "Renamed"


def test_reward_can_be_deactivated(client, make):
    reward = make.reward()
    response = client.patch(f"/api/backend/rewards/{reward.id}/status", headers=headers(make), json={"is_active": False})
    assert response.json()["message"] == "Reward deactivated" and response.json()["data"]["is_active"] is False


def test_only_unused_rewards_can_be_deleted(client, make, db):
    staff = make.staff()
    h = make.headers(staff)
    unused = make.reward()
    assert client.delete(f"/api/backend/rewards/{unused.id}", headers=h).status_code == 200
    used = make.reward(name="Used")
    make.receive(staff, used, 1, make.location())
    response = client.delete(f"/api/backend/rewards/{used.id}", headers=h)
    assert response.status_code == 422
    assert response.json()["message"] == "Used has redemptions or stock history, so it cannot be deleted. Deactivate it instead."
    assert db.scalar(select(func.count(Reward.id))) == 1


def test_rewards_need_the_rewards_page(client, make):
    assert client.get("/api/backend/rewards", headers=headers(make, ["tiers"])).status_code == 403

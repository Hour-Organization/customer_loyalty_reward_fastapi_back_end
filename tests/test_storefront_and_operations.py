from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app import clock
from app.models import ActivityLog, Order, PersonalAccessToken, Referral, Setting
from app.seeders import seed

# ---------------------------------------------------------------- Storefront


def test_guests_see_the_live_catalog_with_stock_but_no_costs(client, make):
    open_category = make.category(name="Coffee")
    make.product(category=open_category, name="Kampot", stock=4, cost_price=Decimal(3))
    make.product(category=make.category(name="Hidden", is_active=False), name="Secret")
    data = client.get("/api/storefront").json()["data"]
    assert [category["name"] for category in data["categories"]] == ["Coffee"]
    product = data["products"][0]
    assert product["name"] == "Kampot" and product["stock"] == 4
    assert "cost_price" not in product and "reorder_level" not in product
    assert product["category"] == {"id": open_category.id, "name": "Coffee", "is_active": True}
    assert data["exchange_rate"] == 4000


def test_rewards_show_what_is_available_for_each_option(client, make):
    staff = make.staff()
    location = make.location()
    plain = make.reward(name="Voucher")
    make.receive(staff, plain, 3, location)
    kit = make.reward(name="Kit")
    small, large = make.variant(kit, size="S"), make.variant(kit, size="L")
    make.receive(staff, kit, 2, location, variant_id=small.id)
    make.reward(name="Off", is_active=False)
    rewards = {reward["name"]: reward for reward in client.get("/api/storefront").json()["data"]["rewards"]}
    assert set(rewards) == {"Voucher", "Kit"}
    assert rewards["Voucher"]["available"] == {"": 3} and rewards["Voucher"]["stock"] == 3
    assert rewards["Kit"]["available"] == {str(small.id): 2, str(large.id): 0}
    assert rewards["Kit"]["stock_item"] == {"sku": f"RW-{kit.id}", "tracking": "none", "out_of_stock": "disable", "allow_backorder": False}


def test_only_running_campaigns_are_listed_with_tiers_badges_and_the_exchange_rate(client, make, db):
    make.campaign(name="Now")
    make.campaign(name="Ended", start_date=clock.today() - timedelta(days=10), end_date=clock.today() - timedelta(days=1))
    make.tier(name="Bronze", multiplier=Decimal("1.25"))
    make.badge(code="FIRST")
    db.add(Setting(key=Setting.EXCHANGE_RATE, value="4100"))
    db.commit()
    data = client.get("/api/storefront").json()["data"]
    assert [campaign["name"] for campaign in data["campaigns"]] == ["Now"]
    assert data["tiers"][0]["multiplier"] == "1.25" and "customers_count" not in data["tiers"][0]
    assert data["badges"][0]["code"] == "FIRST"
    assert data["exchange_rate"] == 4100


# ---------------------------------------------------------------- Account


def test_the_account_holds_only_the_customers_own_records(client, make, db):
    customer, other = make.customer(), make.customer()
    friend = make.customer()
    db.add(Referral(referrer_id=customer.id, referred_id=friend.id))
    db.commit()
    product = make.product(stock=5)
    for who in (customer, other):
        client.post("/api/checkout", headers=make.headers(who), json={"items": [{"product_id": product.id, "quantity": 1}], "address": "A", "phone": "012345678"})
    data = client.get("/api/account", headers=make.headers(customer)).json()["data"]
    assert data["profile"]["id"] == customer.id and data["profile"]["tier"] == customer.tier.name
    assert [order["customer_id"] for order in data["orders"]] == [customer.id]
    assert data["referrals"][0]["referred_name"] == friend.name
    assert data["redemptions"] == data["restock_requests"] == data["tier_history"] == []


def test_customers_can_update_their_details_but_not_their_points(client, make, db):
    customer = make.customer(points=10)
    response = client.patch("/api/profile", headers=make.headers(customer), json={"name": "Renamed", "province": "Kep", "points_balance": 9999})
    assert response.json()["data"]["name"] == "Renamed" and response.json()["data"]["province"] == "Kep"
    db.expire_all()
    assert customer.points_balance == 10
    blank = client.patch("/api/profile", headers=make.headers(customer), json={"name": ""})
    assert blank.json()["errors"]["name"] == ["The name field is required."]


def test_the_account_needs_a_customer_token(client, make):
    assert client.get("/api/account").status_code == 401
    assert client.get("/api/account", headers=make.headers(make.staff())).status_code == 401


# ---------------------------------------------------------------- Customer management


def test_customers_are_listed_with_their_totals_and_no_password(client, make):
    customer = make.customer(name="Sreymom", total_spent=Decimal("12.5"))
    row = client.get("/api/backend/customers?search=srey", headers=make.headers(make.staff(["dashboard"]))).json()["data"][0]
    assert row["id"] == customer.id and row["total_spent"] == 12.5 and row["orders_count"] == 0
    assert "password" not in row and row["badges"] == [] and row["joined_at"] == clock.today().isoformat()


def test_a_tier_can_be_locked_and_unlocked_with_a_reason(client, make, db):
    bronze = make.tier(name="Bronze", min_points=0)
    make.tier(name="Gold", min_points=5000)
    customer = make.customer(tier=bronze)
    headers = make.headers(make.staff(["customers"]))
    url = f"/api/backend/customers/{customer.id}/tier"
    assert client.patch(url, headers=headers, json={"tier": "Gold"}).json()["errors"]["reason"] == ["The reason field is required."]
    locked = client.patch(url, headers=headers, json={"tier": "Gold", "reason": "VIP"}).json()["data"]
    assert locked["tier"] == "Gold" and locked["tier_locked"] is True
    unlocked = client.patch(url, headers=headers, json={"tier": None, "reason": "Back to points"}).json()["data"]
    assert unlocked["tier"] == "Bronze" and unlocked["tier_locked"] is False


def test_deactivating_a_customer_signs_them_out_and_needs_super_admin(client, make, db):
    customer = make.customer()
    customer_headers = make.headers(customer)
    url = f"/api/backend/customers/{customer.id}/status"
    assert client.patch(url, headers=make.headers(make.staff(["customers"])), json={"is_active": False}).status_code == 403
    response = client.patch(url, headers=make.headers(make.staff()), json={"is_active": False})
    assert response.json()["message"] == "Customer deactivated"
    assert client.get("/api/profile", headers=customer_headers).status_code == 401
    assert db.scalar(select(func.count(PersonalAccessToken.id)).where(PersonalAccessToken.tokenable_id == customer.id, PersonalAccessToken.tokenable_type.like("%Customer"))) == 0


def test_customers_need_a_page_that_uses_them(client, make):
    assert client.get("/api/backend/customers", headers=make.headers(make.staff(["products"]))).status_code == 403


def test_referrals_are_listed_with_both_customers(client, make, db):
    referrer, friend = make.customer(name="Dara"), make.customer(name="Sok")
    db.add(Referral(referrer_id=referrer.id, referred_id=friend.id))
    db.commit()
    row = client.get("/api/backend/referrals", headers=make.headers(make.staff(["referrals"]))).json()["data"][0]
    assert (row["referrer_name"], row["referred_name"], row["completed"]) == ("Dara", "Sok", False)


# ---------------------------------------------------------------- Activity log, settings and reports


def test_staff_changes_are_logged_with_who_made_them_but_reads_and_rejections_are_not(client, make, db):
    staff = make.staff(name="Rotha")
    headers = make.headers(staff)
    client.get("/api/backend/tiers", headers=headers)
    client.post("/api/backend/tiers", headers=headers, json={})
    tier = client.post("/api/backend/tiers", headers=headers, json={"name": "Gold", "min_points": 0, "multiplier": 1}).json()["data"]
    client.delete(f"/api/backend/tiers/{tier['id']}", headers=headers)
    rows = client.get("/api/backend/activity-logs", headers=headers).json()["data"]
    assert [(row["event"], row["description"], row["actor_name"]) for row in rows] == [("TIER_CREATED", "Tier created: Gold", "Rotha")]


def test_the_log_is_read_newest_first_and_filtered(client, make, db):
    for event, description in (("A", "first entry"), ("B", "Second Entry"), ("A", "third")):
        db.add(ActivityLog(event=event, description=description))
    db.commit()
    headers = make.headers(make.staff(["logs"]))
    rows = client.get("/api/backend/activity-logs", headers=headers).json()["data"]
    assert [row["description"] for row in rows] == ["third", "Second Entry", "first entry"]
    assert rows[0]["actor_name"] == "System"
    assert len(client.get("/api/backend/activity-logs?event=A", headers=headers).json()["data"]) == 2
    assert len(client.get("/api/backend/activity-logs?search=second", headers=headers).json()["data"]) == 1
    assert client.get("/api/backend/activity-logs", headers=make.headers(make.staff(["orders"]))).status_code == 403


def test_the_exchange_rate_is_for_super_admins_and_validated(client, make):
    url = "/api/backend/settings/exchange-rate"
    assert client.put(url, headers=make.headers(make.staff(["dashboard"])), json={"rate": 4100}).status_code == 403
    admin = make.headers(make.staff())
    assert client.put(url, headers=admin, json={"rate": 0}).json()["errors"]["rate"] == ["The rate field must be at least 1."]
    assert client.put(url, headers=admin, json={"rate": 4100}).json()["data"] == {"exchange_rate": 4100}
    assert client.get("/api/storefront").json()["data"]["exchange_rate"] == 4100


def test_only_a_stock_admin_changes_the_reservation_time(client, make):
    url = "/api/backend/stock-settings"
    manager = make.headers(make.staff(["stock"], "manager"))
    assert client.put(url, headers=manager, json={"reservation_hours": 48}).json()["message"] == "This action needs stock admin authority."
    assert client.put(url, headers=make.headers(make.staff()), json={"reservation_hours": 48}).json()["data"] == {"reservation_hours": 48}


def test_dashboards_get_totals_without_reading_the_ledger(client, make):
    customer = make.customer()
    admin = make.headers(make.staff())
    for points in (300, -100):
        client.post("/api/backend/points/adjustments", headers=admin, json={"customer_id": customer.id, "points": points, "reason": "x"})
    data = client.get("/api/backend/reports/summary", headers=make.headers(make.staff(["dashboard"]))).json()["data"]
    assert data == {"points_issued": 300, "points_redeemed": 0, "rewards_redeemed": 0}


# ---------------------------------------------------------------- Seeder


def test_the_seeder_can_run_twice_and_the_demo_accounts_log_in(client, db):
    seed(db)
    seed(db)
    assert db.scalar(select(func.count(Order.id))) == 6
    assert client.post("/api/login", json={"email": "customer@example.com", "password": "password"}).status_code == 200
    login = client.post("/api/backend/login", json={"email": "test@example.com", "password": "password"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert client.get("/api/backend/profile", headers=headers).json()["data"]["role"]["code"] == "super_admin"
    stock = client.get("/api/backend/stock", headers=headers).json()["data"]
    assert len(stock["rewards"]) == 8 and len(stock["locations"]) == 3 and len(stock["movements"]) == 17


def test_the_seeded_ledger_chains_to_each_customers_balance(db):
    from app.models import Customer, LoyaltyTransaction

    seed(db)
    for customer in db.scalars(select(Customer)):
        rows = list(db.scalars(select(LoyaltyTransaction).where(LoyaltyTransaction.customer_id == customer.id).order_by(LoyaltyTransaction.id)))
        balance = 0
        for row in rows:
            assert row.balance_before == balance
            balance = row.balance_after
        assert balance == customer.points_balance


# ---------------------------------------------------------------- Settings


def test_the_expiry_interval_takes_seconds_or_a_unit():
    from app.config import Settings

    every = {value: Settings(expire_redemptions_every=value).expire_redemptions_every for value in ("300", "90s", "5m", "1h", "0")}
    assert every == {"300": 300, "90s": 90, "5m": 300, "1h": 3600, "0": 0}


def test_setup_seeds_an_empty_database_once(db, monkeypatch):
    from app import cli
    from app.models import Tier

    monkeypatch.setattr(cli, "migrate", lambda: None)
    cli.setup()
    db.query(Tier).filter(Tier.name == "Bronze").update({"multiplier": Decimal("1.1")})
    db.commit()
    cli.setup()
    db.expire_all()
    assert db.scalar(select(func.count(Tier.id))) == 4
    assert db.scalar(select(Tier.multiplier).where(Tier.name == "Bronze")) == Decimal("1.1")


def test_seeded_products_have_descriptions_and_keep_edited_ones(db):
    from app.models import Product

    seed(db)
    assert all(db.scalars(select(Product.description)))
    coffee = db.scalar(select(Product).where(Product.name == "Kampot Pepper Coffee"))
    coffee.description = "Written by staff"
    db.commit()
    seed(db)
    db.expire_all()
    assert db.scalar(select(Product.description).where(Product.name == "Kampot Pepper Coffee")) == "Written by staff"

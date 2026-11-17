from decimal import Decimal

from sqlalchemy import func, select

from app.models import (
    ActivityLog,
    CustomerBadge,
    LoyaltyTransaction,
    Order,
    ProductStockMovement,
    Referral,
)


def checkout(client, headers, items, address="12 Street 240", phone="012345678"):
    return client.post("/api/checkout", headers=headers, json={"items": items, "address": address, "phone": phone})


def place(client, make, customer, product, quantity=1):
    response = checkout(client, make.headers(customer), [{"product_id": product.id, "quantity": quantity}])
    assert response.status_code == 201, response.json()
    return response.json()["data"]


def walk(client, headers, order_id, *statuses):
    for status in statuses:
        response = client.patch(f"/api/backend/orders/{order_id}/status", headers=headers, json={"status": status})
        assert response.status_code == 200, response.json()
    return response.json()["data"]


COMPLETE = ("CONFIRMED", "PROCESSING", "SHIPPED", "COMPLETED")


# ---------------------------------------------------------------- Checkout


def test_an_order_is_priced_from_the_catalog_and_takes_stock(client, make, db):
    product = make.product(price="12.50", stock=10, normal_points=12, bonus_multiplier=Decimal(2))
    order = place(client, make, make.customer(), product, 3)
    assert order["reference"] == f"ORD-{order['id']:06d}" and order["status"] == "PENDING"
    assert order["total"] == 37.5 and order["payment"] == {"method": "CASH_ON_DELIVERY", "status": "PENDING"}
    assert order["items"][0] == {"product_id": product.id, "name": product.name, "quantity": 3, "unit_price": 12.5, "base_points": 12, "bonus_multiplier": 2.0}
    db.expire_all()
    assert product.stock() == 7
    assert db.scalars(select(ProductStockMovement).where(ProductStockMovement.type == "SALE")).one().quantity == -3


def test_the_same_product_on_two_lines_is_one_order_line(client, make):
    product = make.product(stock=10)
    response = checkout(client, make.headers(make.customer()), [{"product_id": product.id, "quantity": 1}, {"product_id": product.id, "quantity": 2}])
    assert [item["quantity"] for item in response.json()["data"]["items"]] == [3]


def test_ordering_more_than_is_in_stock_changes_nothing(client, make, db):
    product = make.product(name="Tea", stock=2)
    response = checkout(client, make.headers(make.customer()), [{"product_id": product.id, "quantity": 3}])
    assert response.json()["errors"] == {"items": ["Only 2 of Tea left in stock."]}
    assert db.scalar(select(func.count(Order.id))) == 0


def test_products_that_are_gone_cannot_be_ordered(client, make):
    hidden = make.product(category=make.category(is_active=False), stock=5)
    response = checkout(client, make.headers(make.customer()), [{"product_id": hidden.id, "quantity": 1}])
    assert response.json()["errors"] == {"items": ["An item in your cart is no longer available."]}


def test_checkout_details_are_validated(client, make):
    headers = make.headers(make.customer())
    errors = client.post("/api/checkout", headers=headers, json={"items": [], "phone": "abc"}).json()["errors"]
    assert errors["items"] == ["Your cart is empty."]
    assert errors["address"] == ["Enter a delivery address and phone number."]
    assert errors["phone"] == ["Enter a valid phone number."]
    quantity = checkout(client, headers, [{"product_id": 1, "quantity": 0}]).json()["errors"]
    assert quantity == {"items.0.quantity": ["The items.0.quantity field must be at least 1."]}


def test_checkout_is_for_customers_only(client, make):
    assert client.post("/api/checkout", headers=make.headers(make.staff()), json={}).status_code == 401


# ---------------------------------------------------------------- Lifecycle


def test_orders_can_only_take_the_next_step(client, make):
    order = place(client, make, make.customer(), make.product(stock=5))
    headers = make.headers(make.staff())
    response = client.patch(f"/api/backend/orders/{order['id']}/status", headers=headers, json={"status": "SHIPPED"})
    assert response.json()["errors"]["status"] == [f"{order['reference']} is PENDING and cannot become SHIPPED."]
    shipped = walk(client, headers, order["id"], "CONFIRMED", "PROCESSING", "SHIPPED")
    assert shipped["shipment"]["status"] == "SHIPPED"
    cancel = client.patch(f"/api/backend/orders/{order['id']}/status", headers=headers, json={"status": "CANCELLED"})
    assert cancel.status_code == 422


def test_cancelling_returns_stock_and_voids_payment(client, make, db):
    product = make.product(stock=5)
    order = place(client, make, make.customer(), product, 2)
    cancelled = walk(client, make.headers(make.staff()), order["id"], "CANCELLED")
    assert cancelled["payment"]["status"] == "CANCELLED"
    db.expire_all()
    assert product.stock() == 5


def test_completing_an_order_earns_points_scaled_by_tier_and_campaign(client, make, db):
    silver = make.tier(name="Silver", multiplier=Decimal("1.5"))
    customer = make.customer(tier=silver)
    make.campaign(multiplier=Decimal(2))
    make.campaign(multiplier=Decimal("1.5"))
    product = make.product(price=10, normal_points=10, bonus_multiplier=Decimal(2), stock=5)
    order = place(client, make, customer, product, 3)
    completed = walk(client, make.headers(make.staff()), order["id"], *COMPLETE)
    # 10 points × 2 bonus × 3 units × 1.5 tier × 2 campaign
    assert completed["points_earned"] == 180
    assert completed["payment"]["status"] == "PAID" and completed["shipment"]["status"] == "DELIVERED"
    db.expire_all()
    assert (customer.points_balance, customer.qualifying_points, float(customer.total_spent)) == (180, 180, 30.0)
    earn = db.scalars(select(LoyaltyTransaction)).one()
    assert (earn.type, earn.reason) == ("EARN", f"Completed order {order['reference']}")


def test_cancelled_orders_earn_nothing(client, make, db):
    customer = make.customer()
    order = place(client, make, customer, make.product(stock=5))
    walk(client, make.headers(make.staff()), order["id"], "CANCELLED")
    assert db.scalar(select(func.count(LoyaltyTransaction.id))) == 0


def test_enough_points_move_the_customer_up_a_tier_unless_it_is_locked(client, make, db):
    bronze = make.tier(name="Bronze", min_points=0, max_points=99)
    make.tier(name="Silver", min_points=100)
    customer = make.customer(tier=bronze)
    locked = make.customer(tier=bronze, tier_locked=True)
    staff_headers = make.headers(make.staff())
    for who in (customer, locked):
        order = place(client, make, who, make.product(normal_points=100, stock=1))
        walk(client, staff_headers, order["id"], *COMPLETE)
    db.expire_all()
    assert customer.tier.name == "Silver" and locked.tier.name == "Bronze"
    log = db.scalars(select(ActivityLog).where(ActivityLog.event == "TIER_CHANGED")).one()
    assert log.description.startswith("Bronze → Silver: Completed order")


def test_the_referral_bonus_is_paid_once_on_the_first_completed_order(client, make, db):
    referrer, friend = make.customer(), make.customer()
    db.add(Referral(referrer_id=referrer.id, referred_id=friend.id))
    db.commit()
    staff_headers = make.headers(make.staff())
    for _ in range(2):
        order = place(client, make, friend, make.product(normal_points=0, stock=1))
        walk(client, staff_headers, order["id"], *COMPLETE)
    db.expire_all()
    assert (referrer.points_balance, friend.points_balance) == (100, 50)
    assert db.scalars(select(Referral)).one().is_completed is True


def test_badges_are_awarded_once_when_a_threshold_is_reached(client, make, db):
    make.badge(code="FIRST", metric="orders", threshold=1)
    make.badge(code="BIG", metric="spend", threshold=1000)
    customer = make.customer()
    staff_headers = make.headers(make.staff())
    for _ in range(2):
        walk(client, staff_headers, place(client, make, customer, make.product(stock=1))["id"], *COMPLETE)
    assert db.scalar(select(func.count(CustomerBadge.id))) == 1


def test_orders_can_be_listed_and_filtered_and_need_the_orders_page(client, make):
    customer = make.customer()
    place(client, make, customer, make.product(stock=1))
    place(client, make, make.customer(), make.product(stock=1))
    headers = make.headers(make.staff(["dashboard"]))
    assert client.get("/api/backend/orders", headers=headers).json()["meta"]["total"] == 2
    assert len(client.get(f"/api/backend/orders?customer_id={customer.id}", headers=headers).json()["data"]) == 1
    assert client.get("/api/backend/orders?status=LOST", headers=headers).status_code == 422
    assert client.patch("/api/backend/orders/1/status", headers=headers, json={"status": "CONFIRMED"}).status_code == 403


# ---------------------------------------------------------------- Points


def test_transactions_are_listed_newest_first_with_the_customer_and_filtered(client, make):
    staff = make.staff()
    headers = make.headers(staff)
    dara = make.customer(name="Dara", email="dara@example.com")
    sok = make.customer(name="Sok", email="sok@example.com")
    for customer, points in ((dara, 50), (sok, 20), (dara, -10)):
        client.post("/api/backend/points/adjustments", headers=headers, json={"customer_id": customer.id, "points": points, "reason": "Test"})
    rows = client.get("/api/backend/points/transactions", headers=headers).json()["data"]
    assert [row["points"] for row in rows] == [-10, 20, 50]
    assert rows[0]["customer"] == {"id": dara.id, "name": "Dara", "email": dara.email}
    assert rows[0]["creator"] == {"id": staff.id, "name": staff.name}
    assert len(client.get("/api/backend/points/transactions?search=dara", headers=headers).json()["data"]) == 2
    assert len(client.get("/api/backend/points/transactions?direction=deduct", headers=headers).json()["data"]) == 1
    assert len(client.get(f"/api/backend/points/transactions?search={sok.id}", headers=headers).json()["data"]) == 1
    bad = client.get("/api/backend/points/transactions?date_from=2026-11-10&date_to=2026-11-01", headers=headers)
    assert bad.json()["errors"]["date_to"] == ["The date to field must be a date after or equal to date from."]


def test_customer_picker_shows_balance_and_tier(client, make):
    gold = make.tier(name="Gold")
    customer = make.customer(tier=gold, name="Chenda", points=75)
    rows = client.get("/api/backend/points/customers?search=chen", headers=make.headers(make.staff())).json()["data"]
    assert rows == [{"id": customer.id, "tier_id": gold.id, "name": "Chenda", "email": customer.email, "points_balance": 75, "tier": {"id": gold.id, "name": "Gold"}}]


def test_adjustments_are_recorded_without_changing_tier_progress(client, make, db):
    customer = make.customer(points=100)
    response = client.post(
        "/api/backend/points/adjustments", headers=make.headers(make.staff()), json={"customer_id": customer.id, "points": -40, "reason": " Typo "}
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert (data["type"], data["balance_before"], data["balance_after"], data["reason"]) == ("ADJUST", 100, 60, "Manual adjustment: Typo")
    db.expire_all()
    assert (customer.points_balance, customer.qualifying_points) == (60, 0)
    assert db.scalars(select(ActivityLog)).one().description == f"-40 points for {customer.name}: Typo"


def test_a_deduction_cannot_make_the_balance_negative(client, make):
    customer = make.customer(name="Vuthy", points=1500)
    response = client.post(
        "/api/backend/points/adjustments", headers=make.headers(make.staff()), json={"customer_id": customer.id, "points": -2000, "reason": "x"}
    )
    assert response.json()["errors"]["points"] == ["Vuthy only has 1,500 points, so this would make the balance negative."]


def test_adjustment_fields_are_validated(client, make):
    errors = client.post(
        "/api/backend/points/adjustments", headers=make.headers(make.staff()), json={"customer_id": 999, "points": 0}
    ).json()["errors"]
    assert errors == {
        "customer_id": ["That customer no longer exists."],
        "points": ["Points cannot be zero."],
        "reason": ["Enter a reason for the adjustment."],
    }


def test_the_ledger_cannot_be_changed_and_needs_the_points_page(client, make):
    headers = make.headers(make.staff())
    assert client.delete("/api/backend/points/transactions/1", headers=headers).status_code in (404, 405)
    assert client.get("/api/backend/points/transactions", headers=make.headers(make.staff(["orders"]))).status_code == 403


def test_customers_see_only_their_own_history(client, make):
    staff_headers = make.headers(make.staff())
    mine, theirs = make.customer(), make.customer()
    for customer in (mine, theirs):
        client.post("/api/backend/points/adjustments", headers=staff_headers, json={"customer_id": customer.id, "points": 10, "reason": "Gift"})
    rows = client.get("/api/points", headers=make.headers(mine)).json()["data"]
    assert len(rows) == 1 and rows[0]["customer_id"] == mine.id and "created_by" not in rows[0]
    assert client.get("/api/points", headers=staff_headers).status_code == 401

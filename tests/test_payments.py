from datetime import timedelta

from sqlalchemy import select

from app import clock
from app.config import get_settings
from app.models import ActivityLog, Order, Payment
from app.services import payments
from app.services.payments import FakeKhqr


def checkout(client, headers, product, quantity=1, method="KHQR"):
    body = {"items": [{"product_id": product.id, "quantity": quantity}], "address": "12 Street 240", "phone": "012345678"}
    if method is not None:
        body["payment_method"] = method
    return client.post("/api/checkout", headers=headers, json=body)


def place(client, make, customer, product, quantity=1):
    response = checkout(client, make.headers(customer), product, quantity)
    assert response.status_code == 201, response.json()
    return response.json()["data"]


def status_of(client, headers, order_id):
    response = client.get(f"/api/orders/{order_id}/payment", headers=headers)
    assert response.status_code == 200, response.json()
    return response.json()["data"]


def move(client, headers, order_id, status):
    return client.patch(f"/api/backend/orders/{order_id}/status", headers=headers, json={"status": status})


def pay(db, order_id):
    """What the customer's banking app does: settle the KHQR with the provider."""
    FakeKhqr.pay(db.scalars(select(Payment).where(Payment.order_id == order_id)).one().provider_ref)


# ---------------------------------------------------------------- Checkout


def test_a_khqr_order_opens_a_pending_payment_with_a_qr(client, make, db):
    product = make.product(price="12.50", stock=5)
    order = place(client, make, make.customer(), product, 2)
    payment = order["payment"]
    assert order["status"] == "PENDING"
    assert payment["method"] == "KHQR" and payment["status"] == "PENDING"
    assert payment["currency"] == "USD" and payment["amount"] == 25.0
    assert payment["qr"] and payment["paid_at"] is None
    assert payment["expires_at"] == (clock.now() + timedelta(minutes=get_settings().khqr_payment_minutes)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    db.expire_all()
    row = db.scalars(select(Payment)).one()
    assert row.provider == "FAKE" and len(row.provider_ref) == 32
    assert product.stock() == 3


def test_cash_on_delivery_is_still_the_default(client, make):
    product = make.product(stock=5)
    response = checkout(client, make.headers(make.customer()), product, method=None)
    assert response.json()["data"]["payment"] == {"method": "CASH_ON_DELIVERY", "status": "PENDING"}


def test_the_payment_method_is_validated(client, make):
    response = checkout(client, make.headers(make.customer()), make.product(stock=5), method="BITCOIN")
    assert response.json()["errors"] == {"payment_method": ["Choose cash on delivery or KHQR."]}


def test_khqr_is_refused_when_no_provider_is_set(client, make, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "payment_provider", "")
    response = checkout(client, make.headers(make.customer()), make.product(stock=5))
    assert response.json()["errors"] == {"payment_method": ["KHQR payment is not available right now."]}
    assert db.scalars(select(Order)).first() is None


# ---------------------------------------------------------------- Paying


def test_checking_an_unpaid_payment_changes_nothing(client, make):
    customer = make.customer()
    order = place(client, make, customer, make.product(stock=5))
    checked = status_of(client, make.headers(customer), order["id"])
    assert checked["status"] == "PENDING" and checked["payment"]["status"] == "PENDING"


def test_a_settled_payment_confirms_the_order_once(client, make, db):
    customer = make.customer()
    headers = make.headers(customer)
    order = place(client, make, customer, make.product(stock=5))
    pay(db, order["id"])
    checked = status_of(client, headers, order["id"])
    assert checked["status"] == "CONFIRMED"
    assert checked["payment"]["status"] == "PAID" and checked["payment"]["paid_at"]
    assert checked["payment"]["qr"] is None
    status_of(client, headers, order["id"])
    events = db.scalars(select(ActivityLog.event).where(ActivityLog.event == "PAYMENT_RECEIVED")).all()
    assert events == ["PAYMENT_RECEIVED"]


def test_points_are_still_paid_when_the_order_completes(client, make, db):
    staff = make.headers(make.staff())
    customer = make.customer()
    order = place(client, make, customer, make.product(price="10", stock=5, normal_points=10))
    pay(db, order["id"])
    status_of(client, make.headers(customer), order["id"])
    db.expire_all()
    assert customer.points_balance == 0
    paid_at = db.scalars(select(Payment)).one().paid_at
    for status in ("PROCESSING", "SHIPPED", "COMPLETED"):
        assert move(client, staff, order["id"], status).status_code == 200
    db.expire_all()
    assert customer.points_balance == 10
    assert db.scalars(select(Payment)).one().paid_at == paid_at


def test_customers_only_see_their_own_payments(client, make):
    order = place(client, make, make.customer(), make.product(stock=5))
    response = client.get(f"/api/orders/{order['id']}/payment", headers=make.headers(make.customer()))
    assert response.status_code == 404


def test_the_fake_provider_can_be_paid_from_the_api(client, make):
    customer = make.customer()
    order = place(client, make, customer, make.product(stock=5))
    response = client.post(f"/api/orders/{order['id']}/payment/simulate", headers=make.headers(customer))
    assert response.status_code == 200, response.json()
    assert response.json()["data"]["payment"]["status"] == "PAID"


def test_payments_cannot_be_simulated_with_a_real_provider(client, make, monkeypatch):
    customer = make.customer()
    order = place(client, make, customer, make.product(stock=5))
    monkeypatch.setattr(get_settings(), "payment_provider", "bakong")
    response = client.post(f"/api/orders/{order['id']}/payment/simulate", headers=make.headers(customer))
    assert response.status_code == 404


# ---------------------------------------------------------------- Staff


def test_staff_cannot_confirm_an_unpaid_khqr_order(client, make):
    staff = make.headers(make.staff())
    order = place(client, make, make.customer(), make.product(stock=5))
    response = move(client, staff, order["id"], "CONFIRMED")
    assert response.json()["errors"] == {"status": [f"{order['reference']} is waiting for its KHQR payment."]}


def test_cancelling_a_paid_order_marks_the_payment_for_a_refund(client, make, db):
    staff = make.headers(make.staff())
    customer = make.customer()
    product = make.product(stock=5)
    order = place(client, make, customer, product)
    pay(db, order["id"])
    status_of(client, make.headers(customer), order["id"])
    assert move(client, staff, order["id"], "CANCELLED").json()["data"]["payment"]["status"] == "REFUND_DUE"
    db.expire_all()
    assert product.stock() == 5


# ---------------------------------------------------------------- Expiry


def test_unpaid_payments_expire_and_give_the_stock_back(client, make, db):
    product = make.product(stock=5)
    order = place(client, make, make.customer(), product, 2)
    clock.travel(timedelta(minutes=get_settings().khqr_payment_minutes + 1))
    assert payments.expire_overdue(db) == 1
    db.commit()
    db.expire_all()
    row = db.get(Order, order["id"])
    assert row.status == "CANCELLED" and row.payment.status == "EXPIRED"
    assert product.stock() == 5
    assert payments.expire_overdue(db) == 0


def test_a_payment_settled_at_the_last_moment_is_kept(client, make, db):
    order = place(client, make, make.customer(), make.product(stock=5))
    pay(db, order["id"])
    clock.travel(timedelta(minutes=get_settings().khqr_payment_minutes + 1))
    assert payments.expire_overdue(db) == 0
    db.commit()
    db.expire_all()
    row = db.get(Order, order["id"])
    assert row.status == "CONFIRMED" and row.payment.status == "PAID"


def test_checking_an_overdue_payment_expires_it(client, make):
    customer = make.customer()
    order = place(client, make, customer, make.product(stock=5))
    clock.travel(timedelta(minutes=get_settings().khqr_payment_minutes + 1))
    checked = status_of(client, make.headers(customer), order["id"])
    assert checked["status"] == "CANCELLED" and checked["payment"]["status"] == "EXPIRED"


def test_an_unreachable_provider_leaves_the_payment_waiting(client, make, db, monkeypatch):
    order = place(client, make, make.customer(), make.product(stock=5))

    def down(_self, _payment):
        raise payments.ProviderUnavailable("Bakong is down")

    monkeypatch.setattr(FakeKhqr, "check", down)
    clock.travel(timedelta(minutes=get_settings().khqr_payment_minutes + 1))
    assert payments.expire_overdue(db) == 0
    db.commit()
    db.expire_all()
    assert db.get(Order, order["id"]).payment.status == "PENDING"

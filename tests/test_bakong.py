import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app import clock
from app.config import get_settings
from app.models import ActivityLog, Order, Payment
from app.services import khqr, payments
from app.services.payments import BakongKhqr, FakeKhqr

ACCOUNT = "khmershop@aclb"


class FakeBakong:
    """Answers the Bakong Open API the way its documentation shows."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.paid: dict[str, dict] = {}
        self.status = 200
        self.error_code: int | None = None
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("no route to host", request=request)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "nope"})
        md5 = json.loads(request.content)["md5"]
        if self.error_code is not None:
            return httpx.Response(200, json={"data": None, "errorCode": self.error_code, "responseCode": 1, "responseMessage": "x"})
        if md5 in self.paid:
            return httpx.Response(200, json={"responseCode": 0, "responseMessage": "Getting transaction successfully.", "data": self.paid[md5]})
        return httpx.Response(
            200,
            json={"data": None, "errorCode": 1, "responseCode": 1, "responseMessage": "Transaction could not be found. Please check and try again."},
        )

    def pay(self, payment: Payment, **changes):
        data = {"hash": "8465d722", "fromAccountId": "customer@abaa", "toAccountId": ACCOUNT, "currency": payment.currency, "amount": float(payment.amount)}
        self.paid[payment.provider_ref] = data | changes


@pytest.fixture
def bakong(monkeypatch):
    settings = get_settings()
    for name, value in {
        "payment_provider": "bakong",
        "bakong_token": "test-token",
        "bakong_account_id": ACCOUNT,
        "bakong_merchant_name": "KhmerShop",
        "bakong_merchant_city": "Phnom Penh",
        "bakong_api_url": "https://bakong.test",
    }.items():
        monkeypatch.setattr(settings, name, value)
    api = FakeBakong()
    monkeypatch.setattr(BakongKhqr, "transport", httpx.MockTransport(api))
    return api


def place(client, make, customer=None, price="12.50", quantity=1):
    product = make.product(price=price, stock=5)
    customer = customer or make.customer()
    response = client.post(
        "/api/checkout",
        headers=make.headers(customer),
        json={"items": [{"product_id": product.id, "quantity": quantity}], "address": "12 Street 240", "phone": "012345678", "payment_method": "KHQR"},
    )
    assert response.status_code == 201, response.json()
    return customer, response.json()["data"]


def payment_of(db, order_id) -> Payment:
    db.expire_all()
    return db.scalars(select(Payment).where(Payment.order_id == order_id)).one()


def check(client, make, customer, order_id):
    response = client.get(f"/api/orders/{order_id}/payment", headers=make.headers(customer))
    assert response.status_code == 200, response.json()
    return response.json()["data"]


# ---------------------------------------------------------------- The QR


def test_checkout_makes_a_real_khqr_for_the_shop(client, make, db, bakong):
    _, order = place(client, make, quantity=2)
    payload = order["payment"]["qr"]
    found = khqr.parse(payload)
    assert khqr.fields(found["29"]) == {"00": ACCOUNT}
    assert (found["53"], found["54"], found["59"]) == ("840", "25", "KhmerShop")
    assert khqr.fields(found["62"]) == {"01": order["reference"]}
    payment = payment_of(db, order["id"])
    assert payment.provider == "BAKONG" and payment.provider_ref == khqr.md5(payload)
    assert khqr.fields(found["99"])["01"] == khqr.epoch_ms(payment.expires_at)


def test_khqr_is_off_until_bakong_is_set_up(client, make, db, bakong, monkeypatch):
    monkeypatch.setattr(get_settings(), "bakong_token", "")
    product = make.product(stock=5)
    response = client.post(
        "/api/checkout",
        headers=make.headers(make.customer()),
        json={"items": [{"product_id": product.id, "quantity": 1}], "address": "12 Street 240", "phone": "012345678", "payment_method": "KHQR"},
    )
    assert response.json()["errors"] == {"payment_method": ["KHQR payment is not available right now."]}
    assert db.scalars(select(Order)).first() is None
    assert payments.not_ready() == "Set BAKONG_TOKEN to the Bakong Open API token."


# ---------------------------------------------------------------- Checking


def test_the_check_asks_bakong_by_md5_with_the_token(client, make, db, bakong):
    customer, order = place(client, make)
    assert check(client, make, customer, order["id"])["payment"]["status"] == "PENDING"
    request = bakong.requests[-1]
    assert str(request.url) == "https://bakong.test/v1/check_transaction_by_md5"
    assert request.headers["Authorization"] == "Bearer test-token"
    assert json.loads(request.content) == {"md5": payment_of(db, order["id"]).provider_ref}


def test_a_payment_bakong_has_seen_confirms_the_order(client, make, db, bakong):
    customer, order = place(client, make)
    bakong.pay(payment_of(db, order["id"]))
    checked = check(client, make, customer, order["id"])
    assert checked["status"] == "CONFIRMED" and checked["payment"]["status"] == "PAID"


def test_a_transaction_that_does_not_match_is_not_counted(client, make, db, bakong):
    customer, order = place(client, make)
    bakong.pay(payment_of(db, order["id"]), amount=1.0)
    assert check(client, make, customer, order["id"])["payment"]["status"] == "PENDING"
    bakong.pay(payment_of(db, order["id"]), toAccountId="someone@else")
    assert check(client, make, customer, order["id"])["payment"]["status"] == "PENDING"


def test_a_failed_transaction_cancels_the_order(client, make, db, bakong):
    customer, order = place(client, make)
    bakong.error_code = 3
    checked = check(client, make, customer, order["id"])
    assert checked["status"] == "CANCELLED" and checked["payment"]["status"] == "FAILED"


@pytest.mark.parametrize("trouble", ["down", 401, 429, 500, 6])
def test_trouble_with_bakong_leaves_the_payment_waiting(client, make, db, bakong, trouble):
    customer, order = place(client, make)
    if trouble == "down":
        bakong.down = True
    elif trouble == 6:
        bakong.error_code = 6
    else:
        bakong.status = trouble
    checked = check(client, make, customer, order["id"])
    assert checked["status"] == "PENDING" and checked["payment"]["status"] == "PENDING"


# ---------------------------------------------------------------- Grace and late payments


def test_an_unreachable_provider_only_delays_expiry_by_the_grace_period(client, make, db, bakong):
    _, order = place(client, make)
    bakong.down = True
    settings = get_settings()
    clock.travel(timedelta(minutes=settings.khqr_payment_minutes + 1))
    assert payments.expire_overdue(db) == 0
    clock.travel(timedelta(minutes=settings.khqr_unreachable_grace_minutes))
    assert payments.expire_overdue(db) == 1
    db.commit()
    assert payment_of(db, order["id"]).status == "EXPIRED"


def test_a_payment_after_expiry_is_marked_for_a_refund(client, make, db, bakong):
    _, order = place(client, make)
    clock.travel(timedelta(minutes=get_settings().khqr_payment_minutes + 1))
    assert payments.expire_overdue(db) == 1
    db.commit()
    bakong.pay(payment_of(db, order["id"]))
    assert payments.catch_late(db) == 1
    db.commit()
    payment = payment_of(db, order["id"])
    assert payment.status == "REFUND_DUE" and payment.paid_at is not None
    assert db.get(Order, order["id"]).status == "CANCELLED"
    assert db.scalars(select(ActivityLog.event).where(ActivityLog.event == "PAYMENT_LATE")).all() == ["PAYMENT_LATE"]
    assert payments.catch_late(db) == 0


def test_a_payment_after_staff_cancelled_is_marked_for_a_refund(client, make, db):
    customer = make.customer()
    _, order = place(client, make, customer)
    staff = make.headers(make.staff())
    assert client.patch(f"/api/backend/orders/{order['id']}/status", headers=staff, json={"status": "CANCELLED"}).status_code == 200
    FakeKhqr.pay(payment_of(db, order["id"]).provider_ref)
    assert payments.catch_late(db) == 1
    db.commit()
    assert payment_of(db, order["id"]).status == "REFUND_DUE"


def test_closed_payments_are_only_rechecked_for_a_while(client, make, db, bakong):
    _, order = place(client, make)
    settings = get_settings()
    clock.travel(timedelta(minutes=settings.khqr_payment_minutes + 1))
    payments.expire_overdue(db)
    db.commit()
    clock.travel(timedelta(minutes=settings.khqr_late_check_minutes))
    asked = len(bakong.requests)
    bakong.pay(payment_of(db, order["id"]))
    assert payments.catch_late(db) == 0
    assert len(bakong.requests) == asked

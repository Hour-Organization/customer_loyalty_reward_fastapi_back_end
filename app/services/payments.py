"""Online (KHQR) payments: opening one for an order, asking the provider whether it was paid, expiring the
ones left unpaid, and catching payments that arrive after their order was closed.

Only the server decides that a payment is paid, by asking the provider: nothing the browser says counts.
A settled payment confirms its order; points are still paid when the order completes.
"""

import hashlib
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Literal, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.config import get_settings
from app.models import Order, Payment
from app.serial import iso, num
from app.services import khqr
from app.services.records import log_activity
from app.validation import fail

log = logging.getLogger("app.payments")

Outcome = Literal["PAID", "PENDING", "FAILED"]


class ProviderUnavailable(Exception):
    """The provider could not be asked, or its answer cannot be trusted; leave the payment as it is."""


class Provider(Protocol):
    name: str

    def ready(self) -> str | None:
        """Why new KHQR payments cannot be taken (a setting is missing), or None when they can."""

    def start(self, payment: Payment, order: Order) -> None:
        """Fill in `qr_payload` and `provider_ref` for a new payment."""

    def check(self, payment: Payment) -> Outcome:
        """Whether the provider has seen the payment settled. Raises ProviderUnavailable."""


class FakeKhqr:
    """Stands in for Bakong in development and tests. Like Bakong, it knows a payment by the MD5 of its
    payload; the payment counts as settled once `pay()` was called with that MD5. The payload is not a real
    KHQR, so no banking app will pay it. The settled set lives in this process only."""

    name = "FAKE"
    settled: ClassVar[set[str]] = set()

    def ready(self) -> str | None:
        return None

    def start(self, payment: Payment, order: Order) -> None:
        payment.qr_payload = f"FAKE-KHQR|{order.reference()}|{payment.amount:.2f}|{payment.currency}|{clock.now().isoformat()}"
        payment.provider_ref = hashlib.md5(payment.qr_payload.encode()).hexdigest()

    def check(self, payment: Payment) -> Outcome:
        return "PAID" if payment.provider_ref in self.settled else "PENDING"

    @classmethod
    def pay(cls, provider_ref: str) -> None:
        cls.settled.add(provider_ref)


class BakongKhqr:
    """The National Bank of Cambodia's Bakong: a dynamic KHQR to the shop's Bakong account, checked with
    the Bakong Open API's check_transaction_by_md5."""

    name = "BAKONG"
    # Tests swap in an httpx.MockTransport.
    transport: ClassVar[httpx.BaseTransport | None] = None

    @staticmethod
    def merchant() -> khqr.Merchant:
        settings = get_settings()
        return khqr.Merchant(
            account_id=settings.bakong_account_id.strip(),
            name=settings.bakong_merchant_name.strip(),
            city=settings.bakong_merchant_city.strip(),
            merchant_id=settings.bakong_merchant_id.strip(),
            acquiring_bank=settings.bakong_acquiring_bank.strip(),
        )

    def ready(self) -> str | None:
        if not get_settings().bakong_token.strip():
            return "Set BAKONG_TOKEN to the Bakong Open API token."
        try:
            self.merchant().check()
        except khqr.KhqrError as error:
            return str(error)
        return None

    def start(self, payment: Payment, order: Order) -> None:
        payment.qr_payload = khqr.build(
            self.merchant(),
            Decimal(payment.amount),
            payment.currency,
            order.reference(),
            clock.now(),
            payment.expires_at,
        )
        payment.provider_ref = khqr.md5(payment.qr_payload)

    def check(self, payment: Payment) -> Outcome:
        body = self.call("/v1/check_transaction_by_md5", {"md5": payment.provider_ref})
        if body.get("responseCode") == 0:
            self._same_payment(payment, body.get("data") or {})
            return "PAID"
        error = body.get("errorCode")
        if error == 1:  # Transaction could not be found (not paid yet).
            return "PENDING"
        if error == 3:  # Transaction failed.
            return "FAILED"
        raise ProviderUnavailable(f"Bakong answered errorCode {error}: {body.get('responseMessage')}")

    def call(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to the Bakong Open API. Anything but a readable answer is ProviderUnavailable."""
        settings = get_settings()
        try:
            with httpx.Client(
                base_url=settings.bakong_api_url.rstrip("/"),
                timeout=settings.bakong_timeout_seconds,
                transport=self.transport,
            ) as client:
                response = client.post(path, json=payload, headers={"Authorization": f"Bearer {settings.bakong_token.strip()}"})
        except httpx.HTTPError as error:
            raise ProviderUnavailable(f"Could not reach Bakong: {error}") from error
        if response.status_code in (401, 403):
            log.error("Bakong refused the token (HTTP %s). Renew BAKONG_TOKEN: tokens last about 90 days.", response.status_code)
            raise ProviderUnavailable(f"Bakong refused the token (HTTP {response.status_code}).")
        if response.status_code != 200:
            raise ProviderUnavailable(f"Bakong answered HTTP {response.status_code}.")
        try:
            body = response.json()
        except ValueError as error:
            raise ProviderUnavailable("Bakong's answer was not JSON.") from error
        if body.get("errorCode") == 6:
            log.error("Bakong says the token is unauthorized. Renew BAKONG_TOKEN.")
            raise ProviderUnavailable("Bakong says the token is unauthorized.")
        return body

    def _same_payment(self, payment: Payment, data: dict[str, Any]) -> None:
        """The MD5 already pins the QR's details; refuse to count a transaction that does not match them."""
        try:
            amount = Decimal(str(data.get("amount")))
        except InvalidOperation:
            amount = None
        account = get_settings().bakong_account_id.strip()
        if data.get("toAccountId") != account or data.get("currency") != payment.currency or amount != Decimal(payment.amount):
            log.error(
                "Bakong reported a transaction for payment %s that does not match it: %s %s to %s",
                payment.id,
                data.get("amount"),
                data.get("currency"),
                data.get("toAccountId"),
            )
            raise ProviderUnavailable("Bakong's transaction does not match the payment.")


PROVIDERS: dict[str, type] = {FakeKhqr.name: FakeKhqr, BakongKhqr.name: BakongKhqr}


def configured() -> str:
    """The provider new KHQR payments use ("FAKE", "BAKONG"), or "" when KHQR checkout is off."""
    return get_settings().payment_provider.strip().upper()


def _provider(name: str | None) -> Provider:
    found = PROVIDERS.get(name or "")
    if found is None:
        raise ProviderUnavailable(f"No payment provider called {name!r} is set up.")
    return found()


def not_ready() -> str | None:
    """Why KHQR checkout is off right now, or None when it is on."""
    name = configured()
    if name not in PROVIDERS:
        return "PAYMENT_PROVIDER is not set to fake or bakong." if name else "PAYMENT_PROVIDER is not set."
    return _provider(name).ready()


def ensure_available(method: str) -> None:
    """Refuse a KHQR checkout while no provider is set up."""
    if method == Payment.CASH_ON_DELIVERY:
        return
    reason = not_ready()
    if reason:
        log.warning("KHQR checkout refused: %s", reason)
        fail("payment_method", "KHQR payment is not available right now.")


def start(order: Order, payment: Payment) -> None:
    """Ask the provider for the QR of a new KHQR payment and give it its time limit."""
    payment.provider = configured()
    payment.expires_at = clock.now() + timedelta(minutes=get_settings().khqr_payment_minutes)
    try:
        _provider(payment.provider).start(payment, order)
    except (khqr.KhqrError, ProviderUnavailable) as error:
        log.error("Could not make the KHQR for %s: %s", order.reference(), error)
        fail("payment_method", "KHQR payment is not available right now.")


def refresh(db: Session, order: Order) -> None:
    """Bring a waiting KHQR payment up to date: paid confirms the order, refused or out of time cancels it.
    Safe to call any number of times. A provider that cannot be reached leaves everything as it was, until
    `khqr_unreachable_grace_minutes` after the time limit, when the payment expires unchecked."""
    payment = order.payment
    if payment is None or not payment.awaiting_online():
        return
    db.refresh(payment, with_for_update=True)
    if not payment.awaiting_online():
        return
    now = clock.now()
    try:
        outcome = _provider(payment.provider).check(payment)
    except ProviderUnavailable:
        log.warning("Could not check the payment for %s", order.reference(), exc_info=True)
        grace = timedelta(minutes=get_settings().khqr_unreachable_grace_minutes)
        if payment.expires_at is not None and payment.expires_at + grace <= now:
            _close(db, order, payment, Payment.EXPIRED, "expired; the provider could not be asked")
            db.flush()
        return
    if outcome == "PAID":
        _settled(db, order, payment)
    elif outcome == "FAILED":
        _close(db, order, payment, Payment.FAILED, "was refused")
    elif payment.expires_at is not None and payment.expires_at <= now:
        _close(db, order, payment, Payment.EXPIRED, "expired unpaid")
    db.flush()


def expire_overdue(db: Session) -> int:
    """Check every KHQR payment past its time limit one last time, then expire the unpaid ones."""
    overdue = db.scalars(
        select(Order)
        .join(Order.payment)
        .where(Payment.status == Payment.PENDING, Payment.method != Payment.CASH_ON_DELIVERY, Payment.expires_at < clock.now())
        .order_by(Order.id)
    )
    expired = 0
    for order in list(overdue):
        refresh(db, order)
        expired += order.payment.status == Payment.EXPIRED
    return expired


def catch_late(db: Session) -> int:
    """Ask again about KHQRs closed unpaid (expired, or cancelled by staff while the customer could still
    scan) until `khqr_late_check_minutes` after their time limit. One the customer paid after all is marked
    REFUND_DUE, since its order stays cancelled. Returns how many were found paid."""
    since = clock.now() - timedelta(minutes=get_settings().khqr_late_check_minutes)
    closed = db.scalars(
        select(Payment)
        .where(
            Payment.status.in_([Payment.EXPIRED, Payment.CANCELLED]),
            Payment.method != Payment.CASH_ON_DELIVERY,
            Payment.provider_ref.is_not(None),
            Payment.expires_at >= since,
        )
        .order_by(Payment.id)
    )
    found = 0
    for payment in list(closed):
        try:
            outcome = _provider(payment.provider).check(payment)
        except ProviderUnavailable:
            log.warning("Could not re-check closed payment %s", payment.id, exc_info=True)
            continue
        if outcome != "PAID":
            continue
        db.refresh(payment, with_for_update=True)
        if payment.status not in (Payment.EXPIRED, Payment.CANCELLED):
            continue
        payment.status = Payment.REFUND_DUE
        payment.paid_at = clock.now()
        order = db.get(Order, payment.order_id)
        log_activity(
            db,
            "PAYMENT_LATE",
            f"KHQR payment of ${payment.amount:.2f} for cancelled order {order.reference()} arrived late; refund due",
            order.customer,
        )
        found += 1
    db.flush()
    return found


def _settled(db: Session, order: Order, payment: Payment) -> None:
    payment.status = Payment.PAID
    payment.paid_at = clock.now()
    payment.qr_payload = None
    if order.status == Order.PENDING:
        order.status = Order.CONFIRMED
    log_activity(db, "PAYMENT_RECEIVED", f"Received KHQR payment of ${payment.amount:.2f} for {order.reference()}", order.customer)


def _close(db: Session, order: Order, payment: Payment, status: str, why: str) -> None:
    from app.services import orders

    payment.status = status
    payment.qr_payload = None
    if order.status != Order.CANCELLED:
        orders.cancel(db, order)
    log_activity(db, f"PAYMENT_{status}", f"KHQR payment for {order.reference()} {why}; the order was cancelled", order.customer)


def on_cancel(payment: Payment | None) -> None:
    """A cancelled order owes the customer their money back if they already paid online."""
    if payment is None:
        return
    if payment.status == Payment.PAID and payment.is_online():
        payment.status = Payment.REFUND_DUE
    elif payment.status == Payment.PENDING:
        payment.status = Payment.CANCELLED
    payment.qr_payload = None


def to_api(payment: Payment | None) -> dict[str, Any] | None:
    """The order's payment as the API returns it. Cash on delivery keeps its original two fields."""
    if payment is None:
        return None
    shown: dict[str, Any] = {"method": payment.method, "status": payment.status}
    if payment.is_online():
        shown |= {
            "currency": payment.currency,
            "amount": num(payment.amount),
            "qr": payment.qr_payload if payment.status == Payment.PENDING else None,
            "expires_at": iso(payment.expires_at),
            "paid_at": iso(payment.paid_at),
        }
    return shown

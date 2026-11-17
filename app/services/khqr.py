"""KHQR, Cambodia's EMVCo merchant-presented QR, as Bakong and every Cambodian banking app read it.

A payload is a run of tag-length-value fields (two-digit tag, two-digit length, value) ending in tag 63, a
CRC-16/CCITT-FALSE over everything before it, "6304" included. A dynamic KHQR (one with an amount) also
carries tag 99: when it was made and when it expires, in milliseconds since the epoch; banking apps refuse
it after that. Bakong knows a payment by the MD5 of the whole payload.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

CURRENCIES = {"USD": "840", "KHR": "116"}
NAME_MAX = 25
CITY_MAX = 15
BILL_MAX = 25


class KhqrError(ValueError):
    """The KHQR cannot be made from these details (a setting is missing or too long)."""


def field(tag: str, value: str) -> str:
    if not value:
        return ""
    if len(value) > 99:
        raise KhqrError(f"KHQR tag {tag} is longer than 99 characters.")
    return f"{tag}{len(value):02d}{value}"


def crc16(data: str) -> str:
    """CRC-16/CCITT-FALSE (polynomial 0x1021, start 0xFFFF), as four upper-case hex digits."""
    crc = 0xFFFF
    for byte in data.encode():
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return f"{crc:04X}"


def with_crc(payload: str) -> str:
    """Close a payload with its tag 63."""
    payload += "6304"
    return payload + crc16(payload)


def md5(payload: str) -> str:
    """What Bakong's check_transaction_by_md5 knows the payment by."""
    return hashlib.md5(payload.encode()).hexdigest()


def amount_text(amount: Decimal, currency: str) -> str:
    """Riel has no decimals; dollars have at most two, without trailing zeros (9, 9.5, 12.34)."""
    if currency == "KHR":
        return str(amount.quantize(Decimal(1), ROUND_HALF_UP))
    cents = amount.quantize(Decimal("0.01"), ROUND_HALF_UP)
    text = f"{cents:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def epoch_ms(moment: datetime) -> str:
    """A naive UTC datetime (as app.clock gives) in milliseconds since the epoch."""
    return str(int(moment.replace(tzinfo=UTC).timestamp() * 1000))


@dataclass(frozen=True)
class Merchant:
    """Who the money goes to. With `merchant_id` the QR is a merchant KHQR (tag 30), otherwise an
    individual one (tag 29) for a Bakong account such as "shop@aclb"."""

    account_id: str
    name: str
    city: str
    merchant_id: str = ""
    acquiring_bank: str = ""
    category_code: str = "5999"

    def check(self) -> None:
        if not self.account_id or "@" not in self.account_id:
            raise KhqrError("Set BAKONG_ACCOUNT_ID to the shop's Bakong account, e.g. shop@aclb.")
        if not self.name or len(self.name) > NAME_MAX:
            raise KhqrError(f"BAKONG_MERCHANT_NAME must be 1 to {NAME_MAX} characters.")
        if not self.city or len(self.city) > CITY_MAX:
            raise KhqrError(f"BAKONG_MERCHANT_CITY must be 1 to {CITY_MAX} characters.")
        if self.merchant_id and not self.acquiring_bank:
            raise KhqrError("A merchant KHQR (BAKONG_MERCHANT_ID) also needs BAKONG_ACQUIRING_BANK.")
        if not all(32 <= ord(ch) < 127 for ch in self.name + self.city):
            raise KhqrError("The merchant name and city must be plain ASCII (Latin letters).")


def build(merchant: Merchant, amount: Decimal, currency: str, bill_number: str, created: datetime, expires: datetime) -> str:
    """A dynamic KHQR for one payment."""
    merchant.check()
    if currency not in CURRENCIES:
        raise KhqrError(f"KHQR takes USD or KHR, not {currency}.")
    if amount <= 0:
        raise KhqrError("A KHQR payment needs an amount above zero.")
    if len(bill_number) > BILL_MAX:
        raise KhqrError(f"The bill number must be at most {BILL_MAX} characters.")

    if merchant.merchant_id:
        account = field("30", field("00", merchant.account_id) + field("01", merchant.merchant_id) + field("02", merchant.acquiring_bank))
    else:
        account = field("29", field("00", merchant.account_id) + field("02", merchant.acquiring_bank))
    payload = (
        field("00", "01")
        + field("01", "12")  # dynamic: the amount is fixed
        + account
        + field("52", merchant.category_code)
        + field("53", CURRENCIES[currency])
        + field("54", amount_text(amount, currency))
        + field("58", "KH")
        + field("59", merchant.name)
        + field("60", merchant.city)
        + field("62", field("01", bill_number))
        + field("99", field("00", epoch_ms(created)) + field("01", epoch_ms(expires)))
    )
    return with_crc(payload)


def parse(payload: str) -> dict[str, str]:
    """The payload's top-level fields by tag. Raises KhqrError if it is malformed or its CRC is wrong."""
    if len(payload) < 8 or payload[-8:-4] != "6304" or crc16(payload[:-4]) != payload[-4:]:
        raise KhqrError("The KHQR's checksum does not match.")
    return fields(payload)


def fields(data: str) -> dict[str, str]:
    """Split tag-length-value fields (also used for the templates inside tags 29, 30, 62 and 99)."""
    found: dict[str, str] = {}
    i = 0
    while i < len(data):
        try:
            tag, length = data[i : i + 2], int(data[i + 2 : i + 4])
        except ValueError as error:
            raise KhqrError(f"Malformed KHQR field at position {i}.") from error
        found[tag] = data[i + 4 : i + 4 + length]
        i += 4 + length
    return found

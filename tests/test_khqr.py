import hashlib
from datetime import datetime
from decimal import Decimal

import pytest

from app.services import khqr

# A live KHQR published in ABA PayWay's KHQR guideline (developer.payway.com.kh/khqr-guideline-3192101f0):
# a dynamic merchant KHQR for 100 riel, with Bakong's creation/expiry timestamps in tag 99.
PAYWAY_EXAMPLE = (
    "00020101021230510016abaakhppxxx@abaa01151250212145328460208ABA Bank52045987530311654031005802KH"
    "5925OLD ME 25 CHAR WINNER IP26010Phnom Penh62570115MC-REF-KH-1500068340010PAYWAY@ABA0208104514230604A227"
    "9934001317598053453370113175980552533763049FBD"
)

SHOP = khqr.Merchant(account_id="khmershop@aclb", name="KhmerShop", city="Phnom Penh")
CREATED = datetime(2026, 11, 17, 3, 0, 0)
EXPIRES = datetime(2026, 11, 17, 3, 15, 0)


def test_the_crc_is_ccitt_false():
    assert khqr.crc16("123456789") == "29B1"


def test_the_crc_matches_a_published_khqr():
    assert khqr.with_crc(PAYWAY_EXAMPLE[:-8]) == PAYWAY_EXAMPLE
    assert khqr.parse(PAYWAY_EXAMPLE)["54"] == "100"


def test_an_individual_khqr_carries_the_payment():
    payload = khqr.build(SHOP, Decimal("12.50"), "USD", "ORD-000012", CREATED, EXPIRES)
    found = khqr.parse(payload)
    assert found["00"] == "01" and found["01"] == "12"
    assert khqr.fields(found["29"]) == {"00": "khmershop@aclb"}
    assert (found["52"], found["53"], found["54"], found["58"]) == ("5999", "840", "12.5", "KH")
    assert (found["59"], found["60"]) == ("KhmerShop", "Phnom Penh")
    assert khqr.fields(found["62"]) == {"01": "ORD-000012"}
    assert khqr.fields(found["99"]) == {"00": "1794884400000", "01": "1794885300000"}
    assert list(found)[-1] == "63"
    assert khqr.md5(payload) == hashlib.md5(payload.encode()).hexdigest()


def test_a_merchant_khqr_uses_tag_30():
    merchant = khqr.Merchant(account_id="shop@aclb", name="Shop", city="Siem Reap", merchant_id="123", acquiring_bank="ACLEDA")
    found = khqr.parse(khqr.build(merchant, Decimal(4000), "KHR", "ORD-000001", CREATED, EXPIRES))
    assert "29" not in found
    assert khqr.fields(found["30"]) == {"00": "shop@aclb", "01": "123", "02": "ACLEDA"}
    assert (found["53"], found["54"]) == ("116", "4000")


@pytest.mark.parametrize(
    ("amount", "currency", "text"),
    [("9.00", "USD", "9"), ("9.50", "USD", "9.5"), ("12.345", "USD", "12.35"), ("4000.4", "KHR", "4000"), ("10", "USD", "10")],
)
def test_amounts_are_written_without_trailing_zeros(amount, currency, text):
    assert khqr.amount_text(Decimal(amount), currency) == text


@pytest.mark.parametrize(
    ("merchant", "message"),
    [
        (khqr.Merchant(account_id="", name="Shop", city="Phnom Penh"), "BAKONG_ACCOUNT_ID"),
        (khqr.Merchant(account_id="shop@aclb", name="A" * 26, city="Phnom Penh"), "BAKONG_MERCHANT_NAME"),
        (khqr.Merchant(account_id="shop@aclb", name="Shop", city="Kampong Chhnang City"), "BAKONG_MERCHANT_CITY"),
        (khqr.Merchant(account_id="shop@aclb", name="ហាង", city="Phnom Penh"), "ASCII"),
        (khqr.Merchant(account_id="shop@aclb", name="Shop", city="Phnom Penh", merchant_id="1"), "BAKONG_ACQUIRING_BANK"),
    ],
)
def test_bad_settings_are_refused(merchant, message):
    with pytest.raises(khqr.KhqrError, match=message):
        khqr.build(merchant, Decimal(1), "USD", "ORD-000001", CREATED, EXPIRES)


def test_a_tampered_khqr_fails_its_checksum():
    payload = khqr.build(SHOP, Decimal("12.50"), "USD", "ORD-000012", CREATED, EXPIRES)
    with pytest.raises(khqr.KhqrError):
        khqr.parse(payload.replace("12.5", "02.5"))

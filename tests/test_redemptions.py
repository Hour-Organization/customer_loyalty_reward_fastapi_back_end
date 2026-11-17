from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app import clock
from app.models import LoyaltyTransaction, Redemption, RestockRequest, Setting
from app.services import stock_ledger


@pytest.fixture
def world(make):
    """A stock admin, a location, a tier and a customer with 1,000 points."""
    staff = make.staff()
    location = make.location()
    tier = make.tier(min_points=0)
    customer = make.customer(tier=tier, points=1000)

    class World:
        pass

    world = World()
    world.staff, world.location, world.tier, world.customer = staff, location, tier, customer
    world.customer_headers = make.headers(customer)
    world.staff_headers = make.headers(staff)

    def reward(stock=5, **fields):
        reward = make.reward(tier=tier, **{"points_cost": 200, **fields})
        if stock:
            make.receive(staff, reward, stock, location)
        return reward

    world.reward = reward
    return world


def redeem(client, world, reward, variant_id=None):
    return client.post("/api/redemptions", headers=world.customer_headers, json={"reward_id": reward.id, "variant_id": variant_id})


def available(make, reward):
    make.db.expire_all()
    return stock_ledger.available_by_reward(make.db, reward.id).get(reward.id, 0)


def test_redeeming_spends_points_and_reserves_a_unit(client, make, world):
    reward = world.reward()
    response = redeem(client, world, reward)
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["status"] == "RESERVED" and data["location_id"] == world.location.id and data["fulfillment"] is None
    assert data["reward_name"] == reward.name
    make.db.expire_all()
    assert world.customer.points_balance == 800
    assert available(make, reward) == 4


def test_a_redemption_holds_for_the_configured_time(client, make, world, db):
    db.add(Setting(key=Setting.RESERVATION_HOURS, value="24"))
    db.commit()
    data = redeem(client, world, world.reward()).json()["data"]
    assert data["reserved_until"] == (clock.now() + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def test_rewards_that_cannot_be_redeemed_leave_points_alone(client, make, world, db):
    cases = {
        "Reward unavailable.": world.reward(is_active=False),
        "Insufficient points.": world.reward(points_cost=5000),
        "Reward is out of stock.": world.reward(stock=0),
    }
    gold = make.tier(name="Gold", min_points=5000)
    cases["Requires Gold tier."] = make.reward(tier=gold, points_cost=100)
    for message, reward in cases.items():
        assert redeem(client, world, reward).json()["errors"] == {"redemption": [message]}
    db.expire_all()
    assert world.customer.points_balance == 1000


def test_a_reward_can_only_be_open_once_per_customer(client, world):
    reward = world.reward()
    redeem(client, world, reward)
    assert redeem(client, world, reward).json()["errors"]["redemption"] == ["You already redeemed this reward."]


def test_rewards_with_options_need_one_chosen(client, make, world):
    reward = world.reward(stock=0)
    variant = make.variant(reward, size="L", color="Red")
    make.receive(world.staff, reward, 2, world.location, variant_id=variant.id)
    assert redeem(client, world, reward).json()["errors"]["redemption"] == ["Choose an option for this reward."]
    data = redeem(client, world, reward, variant.id).json()["data"]
    assert data["reward_name"] == f"{reward.name} (L / Red)"


def test_customers_can_cancel_their_own_redemption_for_a_refund(client, make, world):
    reward = world.reward()
    redemption = redeem(client, world, reward).json()["data"]
    response = client.post(f"/api/redemptions/{redemption['id']}/cancel", headers=world.customer_headers)
    assert response.json()["data"]["status"] == "CANCELLED"
    assert response.json()["data"]["close_reason"] == "Cancelled by customer"
    make.db.expire_all()
    assert world.customer.points_balance == 1000
    assert available(make, reward) == 5
    other = make.headers(make.customer(tier=world.tier, points=500))
    assert client.post(f"/api/redemptions/{redemption['id']}/cancel", headers=other).status_code == 404


def test_staff_fulfilment_takes_the_reserved_unit_out_of_stock(client, make, world, db):
    reward = world.reward()
    redemption = redeem(client, world, reward).json()["data"]
    response = client.post(f"/api/backend/redemptions/{redemption['id']}/fulfill", headers=world.staff_headers, json={"method": "SHIPPED"})
    data = response.json()["data"]
    assert data["status"] == "FULFILLED" and data["fulfillment"] == "SHIPPED" and data["fulfilled_at"]
    db.expire_all()
    level = stock_ledger.levels(db, reward.id)[stock_ledger.key(reward.id, None, world.location.id)]
    assert (level["on_hand"], level["reserved"], level["available"]) == (4, 0, 4)
    again = client.post(f"/api/backend/redemptions/{redemption['id']}/fulfill", headers=world.staff_headers, json={"method": "SHIPPED"})
    assert again.json()["errors"]["redemption"] == ["Only reserved redemptions can be fulfilled."]


def fulfilled(client, world, reward):
    redemption = redeem(client, world, reward).json()["data"]
    client.post(f"/api/backend/redemptions/{redemption['id']}/fulfill", headers=world.staff_headers, json={"method": "PICKED_UP"})
    return redemption


def return_it(client, headers, world, redemption, refund):
    return client.post(
        "/api/backend/stock-movements",
        headers=headers,
        json={
            "type": "return", "reward_id": redemption["reward_id"], "location_id": world.location.id, "quantity": 1,
            "reason": "Damaged box", "redemption_id": redemption["id"], "refund_points": refund,
        },
    )  # fmt: skip


def test_a_returned_redemption_goes_back_into_stock_with_a_refund(client, make, world, db):
    reward = world.reward()
    redemption = fulfilled(client, world, reward)
    response = return_it(client, world.staff_headers, world, redemption, True)
    assert response.status_code == 201, response.json()
    assert response.json()["data"][0]["reference"] == f"RDM-{redemption['id']:06d}"
    db.expire_all()
    returned = db.get(Redemption, redemption["id"])
    assert (returned.status, returned.close_reason) == ("RETURNED", "Damaged box")
    assert world.customer.points_balance == 1000
    assert available(make, reward) == 5
    again = return_it(client, world.staff_headers, world, redemption, False)
    assert again.json()["errors"]["redemption"] == ["Only fulfilled redemptions can be returned."]


def test_clerks_can_return_stock_but_not_refund_points(client, make, world, db):
    reward = world.reward()
    redemption = fulfilled(client, world, reward)
    clerk = make.headers(make.staff(["stock", "fulfillment"]))
    assert return_it(client, clerk, world, redemption, True).json()["errors"]["redemption"] == [
        "Refunding points needs stock manager authority."
    ]
    assert return_it(client, clerk, world, redemption, False).status_code == 201
    db.expire_all()
    assert world.customer.points_balance == 800


def test_a_serial_goes_back_in_with_the_return(client, make, world, db):
    reward = world.reward(stock=0)
    make.stock_item(reward, tracking="serial")
    make.receive(world.staff, reward, 2, world.location, serials=["sn-1", "SN-2"])
    redemption = fulfilled(client, world, reward)
    db.expire_all()
    assert db.get(Redemption, redemption["id"]).serial == "SN-1"
    return_it(client, world.staff_headers, world, redemption, False)
    db.expire_all()
    level = stock_ledger.levels(db, reward.id)[stock_ledger.key(reward.id, None, world.location.id)]
    assert level["serials"] == ["SN-1", "SN-2"]


def test_staff_cancelling_needs_a_reason_and_manager_authority(client, make, world):
    redemption = redeem(client, world, world.reward()).json()["data"]
    clerk = make.headers(make.staff(["fulfillment"]))
    url = f"/api/backend/redemptions/{redemption['id']}/cancel"
    assert client.post(url, headers=clerk, json={"reason": "x"}).json()["message"] == "This action needs stock manager authority."
    assert client.post(url, headers=world.staff_headers, json={}).json()["errors"]["reason"] == ["The reason field is required."]
    assert client.post(url, headers=world.staff_headers, json={"reason": "Duplicate"}).json()["data"]["status"] == "CANCELLED"


def test_clerks_can_fulfil_and_extend(client, make, world):
    redemption = redeem(client, world, world.reward()).json()["data"]
    clerk = make.headers(make.staff(["fulfillment"]))
    extended = client.post(f"/api/backend/redemptions/{redemption['id']}/extend", headers=clerk, json={"hours": 24}).json()["data"]
    assert extended["reserved_until"] > redemption["reserved_until"]
    response = client.post(f"/api/backend/redemptions/{redemption['id']}/fulfill", headers=clerk, json={"method": "PICKED_UP"})
    assert response.status_code == 200


def test_overdue_reservations_expire_and_refund_when_the_account_is_read(client, make, world, db):
    reward = world.reward()
    redeem(client, world, reward)
    clock.travel(timedelta(hours=73))
    account = client.get("/api/account", headers=world.customer_headers).json()["data"]
    assert account["redemptions"][0]["status"] == "EXPIRED"
    assert account["profile"]["points_balance"] == 1000
    assert available(make, reward) == 5


def test_sold_out_rewards_can_be_backordered_and_received_stock_fills_them_oldest_first(client, make, world, db):
    reward = world.reward(stock=0)
    make.stock_item(reward, allow_backorder=True)
    first = redeem(client, world, reward).json()["data"]
    second_customer = make.customer(tier=world.tier, points=500)
    second = client.post("/api/redemptions", headers=make.headers(second_customer), json={"reward_id": reward.id}).json()["data"]
    assert first["status"] == second["status"] == "BACKORDERED"
    response = client.post(
        "/api/backend/stock-movements",
        headers=world.staff_headers,
        json={"type": "receive", "reward_id": reward.id, "location_id": world.location.id, "quantity": 1, "source": "PURCHASE", "unit_cost": 2},
    )
    assert response.status_code == 201
    db.expire_all()
    assert [db.get(Redemption, r["id"]).status for r in (first, second)] == ["RESERVED", "BACKORDERED"]


def test_restock_requests_are_flagged_when_stock_arrives_and_only_owners_remove_them(client, make, world, db):
    reward = world.reward(stock=0)
    created = client.post("/api/restock-requests", headers=world.customer_headers, json={"reward_id": reward.id})
    assert created.status_code == 201
    again = client.post("/api/restock-requests", headers=world.customer_headers, json={"reward_id": reward.id})
    assert again.json()["errors"]["reward_id"] == ["You will already be notified when this is back in stock."]
    client.post(
        "/api/backend/stock-movements",
        headers=world.staff_headers,
        json={"type": "receive", "reward_id": reward.id, "location_id": world.location.id, "quantity": 3, "source": "DONATION", "unit_cost": 0},
    )
    db.expire_all()
    assert db.scalars(select(RestockRequest)).one().notified_at is not None
    request_id = created.json()["data"]["id"]
    stranger = make.headers(make.customer())
    assert client.delete(f"/api/restock-requests/{request_id}", headers=stranger).status_code == 404
    assert client.delete(f"/api/restock-requests/{request_id}", headers=world.customer_headers).status_code == 200


def test_the_stock_snapshot_lists_redemptions_for_fulfilment(client, world):
    redemption = redeem(client, world, world.reward()).json()["data"]
    data = client.get("/api/backend/stock", headers=world.staff_headers).json()["data"]
    assert data["redemptions"][0]["id"] == redemption["id"]
    assert data["reservation_hours"] == 72
    assert data["rewards"][0]["stock_item"]["sku"].startswith("RW-")


def test_spending_is_recorded_in_the_ledger(client, world, db):
    redeem(client, world, world.reward())
    spend = db.scalars(select(LoyaltyTransaction)).one()
    assert (spend.type, spend.points, spend.balance_after) == ("SPEND", -200, 800)
    assert db.scalar(select(func.count(Redemption.id))) == 1

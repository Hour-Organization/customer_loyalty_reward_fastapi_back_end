from datetime import timedelta

from sqlalchemy import select

from app import clock
from app.models import RewardVariant, StocktakeLine
from app.services import stock_ledger


def move(client, headers, **body):
    return client.post("/api/backend/stock-movements", headers=headers, json=body)


def receive(client, headers, reward, location, quantity, **extra):
    return move(
        client, headers, type="receive", reward_id=reward.id, location_id=location.id, quantity=quantity,
        source="PURCHASE", unit_cost=1.5, **extra,
    )  # fmt: skip


def level(make, reward, location, variant_id=None):
    make.db.expire_all()
    return stock_ledger.levels(make.db, reward.id).get(stock_ledger.key(reward.id, variant_id, location.id))


def test_snapshot_lists_rewards_with_default_stock_rules_and_the_ledger(client, make):
    staff = make.staff()
    reward = make.reward()
    location = make.location()
    make.receive(staff, reward, 3, location)
    data = client.get("/api/backend/stock", headers=make.headers(staff)).json()["data"]
    assert data["rewards"][0]["stock_item"] == {
        "sku": f"RW-{reward.id}", "unit_cost": 0.0, "supplier_id": None, "tracking": "none", "low_stock_threshold": 5,
        "reorder_point": 10, "reorder_qty": 25, "out_of_stock": "disable", "allow_backorder": False,
    }  # fmt: skip
    movement = data["movements"][0]
    assert (movement["type"], movement["on_hand_delta"], movement["actor_name"]) == ("RECEIVE", 3, staff.name)


def test_receiving_needs_an_active_location_and_a_whole_quantity(client, make):
    headers = make.headers(make.staff())
    reward, closed = make.reward(), make.location(is_active=False, name="Old Shop")
    assert receive(client, headers, reward, closed, 2).json()["errors"]["location_id"] == ["Old Shop is inactive."]
    errors = receive(client, headers, reward, make.location(), 0).json()["errors"]
    assert errors["quantity"] == ["Enter a whole quantity of at least 1."]
    missing = move(client, headers, type="receive", reward_id=reward.id, location_id=closed.id, quantity=1).json()["errors"]
    assert missing["source"] == ["The source field is required."]


def test_batch_items_keep_the_first_expiry_and_issue_first_expiry_first_out(client, make):
    headers = make.headers(make.staff())
    reward, location = make.reward(), make.location()
    make.stock_item(reward, tracking="batch")
    assert receive(client, headers, reward, location, 2).json()["errors"]["batch"] == ["Enter a batch or lot code."]
    soon = (clock.today() + timedelta(days=10)).isoformat()
    later = (clock.today() + timedelta(days=90)).isoformat()
    receive(client, headers, reward, location, 5, batch="late", expires_at=later)
    receive(client, headers, reward, location, 3, batch="soon", expires_at=soon)
    clash = receive(client, headers, reward, location, 1, batch="SOON", expires_at=later)
    assert clash.json()["errors"]["expires_at"] == [f"Batch SOON already expires on {soon}."]
    out = move(client, headers, type="adjust", direction="out", reward_id=reward.id, location_id=location.id, quantity=4, reason="Count")
    assert [(m["batch"], m["on_hand_delta"]) for m in out.json()["data"]] == [("SOON", -3), ("LATE", -1)]


def test_stock_out_cannot_exceed_what_is_available(client, make):
    headers = make.headers(make.staff())
    reward, location = make.reward(), make.location()
    receive(client, headers, reward, location, 2)
    response = move(client, headers, type="writeoff", kind="DAMAGE", reward_id=reward.id, location_id=location.id, quantity=3, reason="Broken")
    assert response.json()["errors"]["quantity"] == ["Only 2 units available here."]
    empty = move(client, headers, type="writeoff", kind="LOSS", reward_id=reward.id, location_id=make.location().id, quantity=1, reason="x")
    assert empty.json()["errors"]["quantity"] == ["There is no stock of this item at that location."]


def test_expiry_write_offs_only_take_expired_batches(client, make):
    staff = make.staff()
    headers = make.headers(staff)
    reward, location = make.reward(), make.location()
    make.stock_item(reward, tracking="batch")
    receive(client, headers, reward, location, 4, batch="FRESH", expires_at=(clock.today() + timedelta(days=30)).isoformat())
    expire = move(client, headers, type="writeoff", kind="EXPIRY", reward_id=reward.id, location_id=location.id, quantity=1, reason="Expired")
    assert expire.json()["errors"]["quantity"] == ["Not enough expired stock here."]
    clock.travel(timedelta(days=31))
    assert level(make, reward, location)["available"] == 0
    # A month idle signs staff out, so sign in again.
    headers = make.headers(staff)
    ok = move(client, headers, type="writeoff", kind="EXPIRY", reward_id=reward.id, location_id=location.id, quantity=4, reason="Expired")
    assert ok.status_code == 201 and level(make, reward, location)["on_hand"] == 0


def test_serial_items_need_one_unique_serial_per_unit(client, make):
    headers = make.headers(make.staff())
    reward, location = make.reward(), make.location()
    make.stock_item(reward, tracking="serial")
    short = receive(client, headers, reward, location, 2, serials=["A1"])
    assert short.json()["errors"]["serials"] == ["Enter 2 unique serial numbers (one per unit)."]
    receive(client, headers, reward, location, 2, serials=["a1", "A2"])
    again = receive(client, headers, reward, location, 1, serials=["A1"])
    assert again.json()["errors"]["serials"] == ["Serial A1 is already in stock."]
    assert level(make, reward, location)["serials"] == ["A1", "A2"]


def test_transfers_move_stock_between_locations(client, make):
    headers = make.headers(make.staff())
    reward, here, there = make.reward(), make.location(), make.location(name="Siem Reap")
    receive(client, headers, reward, here, 5)
    same = move(client, headers, type="transfer", reward_id=reward.id, location_id=here.id, to_location_id=here.id, quantity=1)
    assert same.json()["errors"]["to_location_id"] == ["Choose a different destination."]
    response = move(client, headers, type="transfer", reward_id=reward.id, location_id=here.id, to_location_id=there.id, quantity=2)
    types = [(m["type"], m["reason"]) for m in response.json()["data"]]
    assert types == [("TRANSFER_OUT", "Transfer to Siem Reap"), ("TRANSFER_IN", "Transfer to Siem Reap")]
    assert (level(make, reward, here)["on_hand"], level(make, reward, there)["on_hand"]) == (3, 2)


def test_adjustments_and_write_offs_need_stock_manager_authority(client, make):
    clerk = make.headers(make.staff(["stock"]))
    reward, location = make.reward(), make.location()
    response = move(client, clerk, type="adjust", direction="in", reward_id=reward.id, location_id=location.id, quantity=1, reason="x")
    assert response.status_code == 403 and response.json()["message"] == "This action needs stock manager authority."
    assert receive(client, clerk, reward, location, 1).status_code == 201


def test_roles_without_an_inventory_page_cannot_see_stock(client, make):
    response = client.get("/api/backend/stock", headers=make.headers(make.staff(["orders"])))
    assert response.status_code == 403 and response.json()["message"] == "Your role has no inventory access."


def test_movement_list_is_paginated_and_filtered_with_the_net_change(client, make):
    headers = make.headers(make.staff())
    reward, location = make.reward(), make.location()
    for quantity in (5, 3):
        receive(client, headers, reward, location, quantity)
    move(client, headers, type="writeoff", kind="LOSS", reward_id=reward.id, location_id=location.id, quantity=2, reason="Lost")
    body = client.get(f"/api/backend/stock-movements?per_page=2&reward_id={reward.id}", headers=headers).json()
    assert body["meta"]["total"] == 3 and len(body["data"]) == 2 and body["net_on_hand"] == 6
    assert client.get("/api/backend/stock-movements?type=LOSS", headers=headers).json()["net_on_hand"] == -2
    assert client.get("/api/backend/stock-movements?from=11/01/2026", headers=headers).status_code == 422


# ---------------------------------------------------------------- Setup


def test_stock_rules_are_saved_with_generated_variant_skus(client, make):
    reward = make.reward()
    response = client.put(
        f"/api/backend/stock-items/{reward.id}",
        headers=make.headers(make.staff()),
        json={
            "sku": " gift-1 ", "unit_cost": 3, "supplier_id": None, "tracking": "serial", "low_stock_threshold": 1,
            "reorder_point": 2, "reorder_qty": 5, "out_of_stock": "hide", "allow_backorder": True,
            "variants": [{"size": "S", "color": ""}, {"size": "", "color": "Red", "sku": "custom"}],
        },
    )  # fmt: skip
    assert response.status_code == 200, response.json()
    data = response.json()["data"]
    assert data["stock_item"]["sku"] == "GIFT-1" and data["stock_item"]["tracking"] == "serial"
    assert [v["sku"] for v in data["variants"]] == ["GIFT-1-1", "CUSTOM"]


def stock_rules(**overrides):
    return {
        "sku": "ITEM", "unit_cost": 1, "tracking": "none", "low_stock_threshold": 1, "reorder_point": 1, "reorder_qty": 1,
        "out_of_stock": "disable", "allow_backorder": False, "variants": [], **overrides,
    }  # fmt: skip


def test_tracking_and_variants_holding_stock_cannot_change(client, make, db):
    staff = make.staff()
    headers = make.headers(staff)
    reward, location = make.reward(), make.location()
    make.receive(staff, reward, 2, location)
    tracking = client.put(f"/api/backend/stock-items/{reward.id}", headers=headers, json=stock_rules(tracking="batch"))
    assert tracking.json()["message"] == "Tracking can only change while the item has no stock on hand or reserved."
    standard = client.put(f"/api/backend/stock-items/{reward.id}", headers=headers, json=stock_rules(variants=[{"size": "S"}]))
    assert standard.json()["message"] == "Standard still has stock. Move it to zero first."
    blank = client.put(f"/api/backend/stock-items/{reward.id}", headers=headers, json=stock_rules(variants=[{"size": " "}]))
    assert blank.json()["errors"]["variants"] == ["Each variant needs a size or colour."]
    assert db.scalar(select(RewardVariant.id)) is None


def test_locations_holding_stock_cannot_be_deactivated(client, make):
    staff = make.staff()
    headers = make.headers(staff)
    reward, location, spare = make.reward(), make.location(name="Main"), make.location()
    make.receive(staff, reward, 1, location)
    response = client.patch(f"/api/backend/stock-locations/{location.id}", headers=headers, json={"is_active": False})
    assert response.json()["message"] == "Transfer or write off Main's stock before deactivating it."
    assert client.patch(f"/api/backend/stock-locations/{spare.id}", headers=headers, json={"is_active": False}).status_code == 200
    created = client.post("/api/backend/stock-locations", headers=headers, json={"name": "Kampot", "type": "kiosk"})
    assert created.json()["errors"]["type"] == ["Choose a location type."]


def test_suppliers_can_be_added_and_updated(client, make):
    headers = make.headers(make.staff())
    created = client.post("/api/backend/suppliers", headers=headers, json={"name": "Mekong Print", "kind": "supplier", "email": ""})
    assert created.status_code == 201 and created.json()["data"]["email"] is None
    supplier_id = created.json()["data"]["id"]
    updated = client.patch(f"/api/backend/suppliers/{supplier_id}", headers=headers, json={"kind": "partner", "email": "bad"})
    assert updated.json()["errors"]["email"] == ["Enter a valid email address."]
    assert client.patch(f"/api/backend/suppliers/{supplier_id}", headers=headers, json={"kind": "partner"}).json()["data"]["kind"] == "partner"


def test_setup_needs_stock_manager_authority(client, make):
    clerk = make.headers(make.staff(["stock"]))
    assert client.post("/api/backend/suppliers", headers=clerk, json={"name": "A", "kind": "supplier"}).status_code == 403
    assert client.put("/api/backend/stock-settings", headers=make.headers(make.staff(["stock"], "manager")), json={"reservation_hours": 5}).status_code == 403


# ---------------------------------------------------------------- Stocktakes


def test_stocktake_snapshots_expected_quantities_per_batch(client, make):
    headers = make.headers(make.staff())
    reward, location = make.reward(), make.location()
    make.stock_item(reward, tracking="batch")
    receive(client, headers, reward, location, 4, batch="A")
    receive(client, headers, reward, location, 2, batch="B")
    data = client.post("/api/backend/stocktakes", headers=headers, json={"location_id": location.id}).json()["data"]
    assert data["reference"] == f"ST-{data['id']:06d}" and data["status"] == "OPEN"
    assert [(line["batch"], line["expected"]) for line in data["lines"]] == [("A", 4), ("B", 2)]
    again = client.post("/api/backend/stocktakes", headers=headers, json={"location_id": location.id})
    assert again.json()["message"] == f"{location.name} already has an open stocktake."


def test_posting_writes_count_variances_against_the_current_level(client, make):
    headers = make.headers(make.staff())
    reward, location = make.reward(), make.location()
    receive(client, headers, reward, location, 5)
    stocktake = client.post("/api/backend/stocktakes", headers=headers, json={"location_id": location.id}).json()["data"]
    line = stocktake["lines"][0]
    unposted = client.post(f"/api/backend/stocktakes/{stocktake['id']}/post", headers=headers)
    assert unposted.json()["message"] == "Count every line before posting."
    receive(client, headers, reward, location, 1)
    client.patch(f"/api/backend/stocktakes/{stocktake['id']}/lines/{line['id']}", headers=headers, json={"counted": 4})
    posted = client.post(f"/api/backend/stocktakes/{stocktake['id']}/post", headers=headers).json()["data"]
    assert posted["status"] == "POSTED" and posted["lines"][0]["system_at_post"] == 6
    assert level(make, reward, location)["on_hand"] == 4


def test_serial_lines_are_counted_as_zero_or_one_and_lines_belong_to_their_stocktake(client, make):
    headers = make.headers(make.staff())
    reward, location = make.reward(), make.location()
    make.stock_item(reward, tracking="serial")
    receive(client, headers, reward, location, 1, serials=["X1"])
    stocktake = client.post("/api/backend/stocktakes", headers=headers, json={"location_id": location.id}).json()["data"]
    line = stocktake["lines"][0]
    url = f"/api/backend/stocktakes/{stocktake['id']}/lines/{line['id']}"
    assert client.patch(url, headers=headers, json={"counted": 2}).json()["errors"]["counted"] == ["Serial lines are counted as 0 or 1."]
    assert client.patch(url, headers=headers, json={}).json()["errors"]["counted"] == ["The counted field must be present."]
    other = make.location()
    receive(client, headers, reward, other, 1, serials=["Y1"])
    second = client.post("/api/backend/stocktakes", headers=headers, json={"location_id": other.id}).json()["data"]
    assert client.patch(f"/api/backend/stocktakes/{second['id']}/lines/{line['id']}", headers=headers, json={"counted": 1}).status_code == 404


def test_clerks_can_count_but_only_managers_post_or_cancel(client, make, db):
    headers = make.headers(make.staff())
    clerk = make.headers(make.staff(["stocktake"]))
    reward, location = make.reward(), make.location()
    receive(client, headers, reward, location, 1)
    stocktake = client.post("/api/backend/stocktakes", headers=clerk, json={"location_id": location.id}).json()["data"]
    line_id = db.scalars(select(StocktakeLine.id)).first()
    assert client.patch(f"/api/backend/stocktakes/{stocktake['id']}/lines/{line_id}", headers=clerk, json={"counted": 1}).status_code == 200
    assert client.post(f"/api/backend/stocktakes/{stocktake['id']}/post", headers=clerk).status_code == 403
    cancelled = client.post(f"/api/backend/stocktakes/{stocktake['id']}/cancel", headers=headers).json()["data"]
    assert cancelled["status"] == "CANCELLED"
    closed = client.patch(f"/api/backend/stocktakes/{stocktake['id']}/lines/{line_id}", headers=clerk, json={"counted": 1})
    assert closed.json()["message"] == "This stocktake is closed."

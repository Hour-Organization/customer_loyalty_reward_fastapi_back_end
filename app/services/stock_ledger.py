"""The reward stock ledger. Stock levels are never stored: they are the sum of the movements.

A level is kept per reward, variant and location, with its batches (and their expiry, set by the batch's
first receipt) and the serial numbers on hand. Units in expired batches and reserved units are not available.
"""

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import clock
from app.auth import random_string
from app.models import (
    Redemption,
    Reward,
    RewardStockItem,
    RewardStockMovement,
    StockLocation,
    User,
)
from app.validation import fail as _fail

Level = dict[str, Any]
Part = dict[str, Any]


def fail(message: str, field: str = "quantity"):
    _fail(field, message)


def plural(word: str, count: int) -> str:
    return word if count == 1 else f"{word}s"


def key(reward_id: int, variant_id: int | None, location_id: int) -> str:
    return f"{reward_id}|{'' if variant_id is None else variant_id}|{location_id}"


def levels(db: Session, reward_id: int | None = None) -> dict[str, Level]:
    """Every stock level, keyed by reward|variant|location."""
    statement = select(RewardStockMovement).order_by(RewardStockMovement.id)
    if reward_id:
        statement = statement.where(RewardStockMovement.reward_id == reward_id)
    expiries: dict[str, str] = {}
    found: dict[str, Level] = {}
    for movement in db.scalars(statement):
        if movement.batch and movement.expires_at:
            expiries.setdefault(f"{movement.reward_id}|{movement.batch}", movement.expires_at.isoformat())
        level = found.setdefault(
            key(movement.reward_id, movement.variant_id, movement.location_id),
            {
                "reward_id": movement.reward_id,
                "variant_id": movement.variant_id,
                "location_id": movement.location_id,
                "on_hand": 0,
                "reserved": 0,
                "batches": {},
                "serials": {},
            },
        )
        level["on_hand"] += movement.on_hand_delta
        level["reserved"] += movement.reserved_delta
        if movement.on_hand_delta and movement.batch:
            level["batches"][movement.batch] = level["batches"].get(movement.batch, 0) + movement.on_hand_delta
        for serial in movement.serials or []:
            if movement.on_hand_delta > 0:
                level["serials"][serial] = True
            elif movement.on_hand_delta < 0:
                level["serials"].pop(serial, None)

    today = clock.today().isoformat()
    for level in found.values():
        batches = [
            {"batch": batch, "on_hand": qty, "expires_at": expiries.get(f"{level['reward_id']}|{batch}")}
            for batch, qty in level["batches"].items()
            if qty != 0
        ]
        batches.sort(key=lambda batch: (batch["expires_at"] or "9999-12-31", batch["batch"]))
        expired = sum(batch["on_hand"] for batch in batches if batch["expires_at"] and batch["expires_at"] < today)
        level["batches"] = batches
        level["serials"] = sorted(level["serials"])
        level["expired"] = expired
        level["available"] = max(0, level["on_hand"] - expired - level["reserved"])
    return found


def _active_location_ids(db: Session) -> list[int]:
    return list(db.scalars(select(StockLocation.id).where(StockLocation.is_active.is_(True)).order_by(StockLocation.id)))


def available_by_reward(db: Session, reward_id: int | None = None) -> dict[int, int]:
    """Units available at active locations, per reward."""
    active = set(_active_location_ids(db))
    totals: dict[int, int] = {}
    for level in levels(db, reward_id).values():
        if level["location_id"] in active:
            totals[level["reward_id"]] = totals.get(level["reward_id"], 0) + level["available"]
    return totals


def availability(db: Session) -> dict[str, int]:
    """Units available at active locations, per "reward|variant" (a blank variant is the standard item)."""
    active = set(_active_location_ids(db))
    totals: dict[str, int] = {}
    for level in levels(db).values():
        if level["location_id"] in active:
            variant = "" if level["variant_id"] is None else level["variant_id"]
            name = f"{level['reward_id']}|{variant}"
            totals[name] = totals.get(name, 0) + level["available"]
    return totals


def best_location(db: Session, reward_id: int, variant_id: int | None) -> int | None:
    """The active location with the most units available, or None when none has any."""
    found = levels(db, reward_id)
    stocked = [
        (location_id, found.get(key(reward_id, variant_id, location_id), {}).get("available", 0))
        for location_id in _active_location_ids(db)
    ]
    stocked = [entry for entry in stocked if entry[1]]
    if not stocked:
        return None
    return sorted(stocked, key=lambda entry: -entry[1])[0][0]


def batch_expiry(db: Session, reward_id: int, batch: str) -> str | None:
    movement = db.scalars(
        select(RewardStockMovement)
        .where(
            RewardStockMovement.reward_id == reward_id,
            RewardStockMovement.batch == batch,
            RewardStockMovement.expires_at.is_not(None),
        )
        .order_by(RewardStockMovement.id)
    ).first()
    return movement.expires_at.isoformat() if movement else None


def normalize_serials(serials: list[Any] | None) -> list[str]:
    seen: dict[str, None] = {}
    for serial in serials or []:
        cleaned = str(serial if serial is not None else "").strip().upper()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


# ---------------------------------------------------------------- Movements


def hold(db: Session, redemption: Redemption, kind: str, reason: str, actor: User | None = None) -> None:
    """Reserve (RESERVE) or release (RELEASE) the redemption's unit without moving it."""
    reward = _reward(db, redemption.reward_id, lock=False)
    record(
        db,
        actor,
        reward.stock_settings(),
        {
            "type": kind,
            "reward_id": redemption.reward_id,
            "variant_id": redemption.variant_id,
            "location_id": redemption.location_id,
            "reference": redemption.reference(),
            "reason": reason,
        },
        [{"qty": 0}],
        1,
        1 if kind == "RESERVE" else -1,
    )


def fulfill(db: Session, actor: User, redemption: Redemption, reason: str, serial: str | None = None) -> Part:
    """Take the redemption's reserved unit out of stock."""
    line = {
        "reward_id": redemption.reward_id,
        "variant_id": redemption.variant_id,
        "location_id": redemption.location_id,
        "quantity": 1,
        "serials": [serial] if serial else [],
    }
    _, item = _check_line(db, line, need_active_location=False)
    parts = _resolve_out(_level_for(db, line), item, line, "reserved")
    record(
        db,
        actor,
        item,
        {
            "type": "FULFILL",
            "reward_id": redemption.reward_id,
            "variant_id": redemption.variant_id,
            "location_id": redemption.location_id,
            "reference": redemption.reference(),
            "reason": reason,
        },
        parts,
        -1,
        -1,
    )
    return parts[0]


def receive(db: Session, actor: User, data: dict[str, Any]) -> list[RewardStockMovement]:
    reward, item = _check_line(db, data, need_active_location=True)
    parts = _resolve_in(db, reward, item, data)
    reason = data.get("reason") or ("Partner donation" if data["source"] == "DONATION" else "Purchase receipt")
    return record(
        db,
        actor,
        item,
        {
            "type": "RECEIVE",
            "reward_id": reward.id,
            "variant_id": data.get("variant_id"),
            "location_id": data["location_id"],
            "supplier_id": data.get("supplier_id"),
            "source": data["source"],
            "unit_cost": data["unit_cost"],
            "reference": data.get("reference"),
            "reason": reason,
        },
        parts,
        1,
    )


def adjust(db: Session, actor: User, data: dict[str, Any]) -> list[RewardStockMovement]:
    stock_in = data["direction"] == "in"
    reward, item = _check_line(db, data, need_active_location=stock_in)
    parts = _resolve_in(db, reward, item, data) if stock_in else _resolve_out(_level_for(db, data), item, data, "available")
    return record(
        db,
        actor,
        item,
        {
            "type": "ADJUST",
            "reward_id": reward.id,
            "variant_id": data.get("variant_id"),
            "location_id": data["location_id"],
            "reason": data["reason"],
        },
        parts,
        1 if stock_in else -1,
    )


def transfer(db: Session, actor: User, data: dict[str, Any]) -> list[RewardStockMovement]:
    reward, item = _check_line(db, data, need_active_location=False)
    destination = db.get(StockLocation, data["to_location_id"])
    if destination is None or not destination.is_active:
        fail("Choose an active destination.")
    parts = _resolve_out(_level_for(db, data), item, data, "available")
    base = {
        "reward_id": reward.id,
        "variant_id": data.get("variant_id"),
        "reference": f"TRF-{random_string(8).upper()}",
        "reason": data.get("reason") or f"Transfer to {destination.name}",
    }
    out = record(db, actor, item, {**base, "type": "TRANSFER_OUT", "location_id": data["location_id"]}, parts, -1)
    return out + record(db, actor, item, {**base, "type": "TRANSFER_IN", "location_id": destination.id}, parts, 1)


def write_off(db: Session, actor: User, data: dict[str, Any]) -> list[RewardStockMovement]:
    reward, item = _check_line(db, data, need_active_location=False)
    mode = "expired" if data["kind"] == "EXPIRY" else "available"
    parts = _resolve_out(_level_for(db, data), item, data, mode)
    return record(
        db,
        actor,
        item,
        {
            "type": data["kind"],
            "reward_id": reward.id,
            "variant_id": data.get("variant_id"),
            "location_id": data["location_id"],
            "reason": data["reason"],
        },
        parts,
        -1,
    )


def return_stock(db: Session, actor: User, data: dict[str, Any]) -> list[RewardStockMovement]:
    reward, item = _check_line(db, data, need_active_location=True)
    parts = _resolve_in(db, reward, item, data)
    return record(
        db,
        actor,
        item,
        {
            "type": "RETURN",
            "reward_id": reward.id,
            "variant_id": data.get("variant_id"),
            "location_id": data["location_id"],
            "reference": data.get("reference"),
            "reason": data["reason"],
        },
        parts,
        1,
    )


def record(
    db: Session,
    actor: User | None,
    item: RewardStockItem,
    base: dict[str, Any],
    parts: list[Part],
    sign: int,
    reserved_delta: int = 0,
) -> list[RewardStockMovement]:
    """Write one movement per part (a batch, a set of serials, or a plain quantity)."""
    movements = []
    for part in parts:
        expires = part.get("expires_at")
        fields = {
            "unit_cost": item.unit_cost if item.unit_cost is not None else 0,
            **base,
            "actor_id": actor.id if actor else None,
            "on_hand_delta": sign * part["qty"],
            "reserved_delta": reserved_delta,
            "batch": part.get("batch"),
            "expires_at": date.fromisoformat(expires) if isinstance(expires, str) else expires,
            "serials": part.get("serials"),
        }
        movement = RewardStockMovement(**fields)
        movement.actor = actor
        db.add(movement)
        movements.append(movement)
    db.flush()
    return movements


# ---------------------------------------------------------------- Checks


def _reward(db: Session, reward_id: int, lock: bool = True) -> Reward:
    statement = select(Reward).where(Reward.id == reward_id).options(
        selectinload(Reward.stock_item), selectinload(Reward.variants)
    )
    reward = db.scalars(statement.with_for_update() if lock else statement).first()
    if reward is None:
        from fastapi import HTTPException

        raise HTTPException(404, f"No query results for model [Reward] {reward_id}")
    return reward


def _check_line(db: Session, line: dict[str, Any], need_active_location: bool) -> tuple[Reward, RewardStockItem]:
    reward = _reward(db, line["reward_id"])
    if line.get("variant_id") not in reward.variant_ids():
        fail("Choose a variant." if reward.variants else "This item has no variants.", "variant_id")
    location = db.get(StockLocation, line["location_id"])
    if location is None:
        from fastapi import HTTPException

        raise HTTPException(404, f"No query results for model [StockLocation] {line['location_id']}")
    if need_active_location and not location.is_active:
        fail(f"{location.name} is inactive.", "location_id")
    return reward, reward.stock_settings()


def _level_for(db: Session, line: dict[str, Any]) -> Level | None:
    return levels(db, line["reward_id"]).get(key(line["reward_id"], line.get("variant_id"), line["location_id"]))


def _resolve_in(db: Session, reward: Reward, item: RewardStockItem, line: dict[str, Any]) -> list[Part]:
    """What comes in: a batch (keeping its first expiry), one serial per unit, or a plain quantity."""
    qty = line["quantity"]
    if item.tracking == "batch":
        batch = str(line.get("batch") or "").strip().upper()
        if batch == "":
            fail("Enter a batch or lot code.", "batch")
        existing = batch_expiry(db, reward.id, batch)
        expires_at = line.get("expires_at")
        if existing and expires_at and expires_at != existing:
            fail(f"Batch {batch} already expires on {existing}.", "expires_at")
        return [{"qty": qty, "batch": batch, "expires_at": None if existing else expires_at}]
    if item.tracking == "serial":
        serials = normalize_serials(line.get("serials"))
        if len(serials) != qty:
            fail(f"Enter {qty} unique serial {plural('number', qty)} (one per unit).", "serials")
        in_stock = {serial for level in levels(db, reward.id).values() for serial in level["serials"]}
        clash = next((serial for serial in serials if serial in in_stock), None)
        if clash:
            fail(f"Serial {clash} is already in stock.", "serials")
        return [{"qty": qty, "serials": serials}]
    return [{"qty": qty}]


def _resolve_out(level: Level | None, item: RewardStockItem, line: dict[str, Any], mode: str) -> list[Part]:
    """What goes out. `mode` is "available" (unreserved, unexpired), "reserved" (a held unit) or "expired"."""
    if not level or level["on_hand"] <= 0:
        fail("There is no stock of this item at that location.")
    qty = line["quantity"]
    today = clock.today().isoformat()

    def unreserved() -> str:
        return f"Only {level['available']} unreserved {plural('unit', level['available'])} available here."

    if item.tracking == "batch":
        chosen = str(line.get("batch") or "").strip().upper()
        if chosen != "":
            batch = next((entry for entry in level["batches"] if entry["batch"] == chosen), None)
            if batch is None or batch["on_hand"] < qty:
                fail(f"Batch {chosen} has only {batch['on_hand'] if batch else 0} on hand here.", "batch")
            expired = bool(batch["expires_at"] and batch["expires_at"] < today)
            if mode == "expired" and not expired:
                fail(f"Batch {chosen} has not expired.", "batch")
            if mode == "reserved" and expired:
                fail(f"Batch {chosen} has expired.", "batch")
            if mode == "available" and not expired and qty > level["available"]:
                fail(unreserved())
            return [{"qty": qty, "batch": chosen}]
        if mode == "available" and qty > level["available"]:
            fail(unreserved())
        picks = _pick_batches(level["batches"], qty, mode == "expired", today)
        if picks is None:
            fail("Not enough expired stock here." if mode == "expired" else "Not enough unexpired stock here.")
        return picks

    if mode == "expired":
        fail("This item does not track expiry dates.")
    capacity = level["on_hand"] - level["expired"] if mode == "reserved" else level["available"]
    if qty > capacity:
        fail(f"Only {capacity} {plural('unit', capacity)} available here.")
    if item.tracking == "serial":
        serials = normalize_serials(line.get("serials"))
        if not serials and mode == "reserved":
            serials = level["serials"][:qty]
        if len(serials) != qty:
            fail(f"Choose {qty} serial {plural('number', qty)}.", "serials")
        missing = next((serial for serial in serials if serial not in level["serials"]), None)
        if missing:
            fail(f"Serial {missing} is not in stock here.", "serials")
        return [{"qty": qty, "serials": serials}]
    return [{"qty": qty}]


def _pick_batches(batches: list[dict[str, Any]], qty: int, expired_only: bool, today: str) -> list[Part] | None:
    """First expiry, first out."""
    picks: list[Part] = []
    remaining = qty
    for batch in batches:
        is_expired = bool(batch["expires_at"] and batch["expires_at"] < today)
        if is_expired != expired_only or batch["on_hand"] <= 0:
            continue
        take = min(batch["on_hand"], remaining)
        picks.append({"qty": take, "batch": batch["batch"]})
        remaining -= take
        if remaining == 0:
            return picks
    return None

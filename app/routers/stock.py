"""Reward stock: the ledger, stock rules, locations, suppliers, stocktakes and redemption fulfilment.

Any inventory page opens these routes. Receiving, transfers, returns and counting need clerk authority;
adjustments, write-offs, setup, posting stocktakes and cancelling redemptions need a stock manager; the
reservation time is for stock admins (Super Admin).
"""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from app import clock
from app.auth import Db, StockStaff, require_stock_authority, stock_authority
from app.http import (
    Input,
    attempt,
    find_or_404,
    from_day,
    ok,
    paginate,
    paginated,
    presence,
    refuse,
    until_day,
    update_route,
)
from app.models import (
    Redemption,
    RestockRequest,
    Reward,
    RewardStockItem,
    RewardStockMovement,
    RewardVariant,
    Setting,
    StockLocation,
    Stocktake,
    StocktakeLine,
    Supplier,
    User,
)
from app.services import redemptions, stock_ledger
from app.services.records import (
    log_activity,
    log_staff_change,
    put_setting,
    setting_int,
)
from app.validation import (
    Exists,
    In,
    RequiredIf,
    Unique,
    ValidationError,
    Validator,
    validate,
)

router = APIRouter(prefix="/backend", tags=["reward stock"])

StockManager = Annotated[User, stock_authority("manager")]
StockAdmin = Annotated[User, stock_authority("admin")]

# The stock authority each kind of movement needs.
AUTHORITY = {"receive": "clerk", "transfer": "clerk", "return": "clerk", "adjust": "manager", "writeoff": "manager"}


def exists_in(db: Session, model: type, **where: Any) -> Exists:
    def lookup(value: Any) -> bool:
        if not str(value).lstrip("-").isdigit():
            return False
        statement = select(model.id).where(model.id == int(value))
        for column, wanted in where.items():
            statement = statement.where(getattr(model, column) == wanted)
        return bool(db.scalar(statement))

    return Exists(lookup)


# ---------------------------------------------------------------- Snapshot and ledger


def snapshot(db: Session) -> dict[str, Any]:
    rewards = db.scalars(
        select(Reward)
        .options(selectinload(Reward.min_tier), selectinload(Reward.stock_item), selectinload(Reward.variants))
        .order_by(Reward.id)
    )
    return {
        "rewards": [
            {
                **reward.to_array(
                    "id", "name", "name_kh", "description", "category", "points_cost", "image", "image_url", "is_active"
                ),
                "min_tier": reward.min_tier.name if reward.min_tier else None,
                "stock_item": reward.stock_settings().to_api(),
                "variants": [variant.to_api() for variant in reward.variants],
            }
            for reward in rewards
        ],
        "locations": [
            location.to_array("id", "name", "name_kh", "type", "is_active")
            for location in db.scalars(select(StockLocation).order_by(StockLocation.id))
        ],
        "suppliers": [
            supplier.to_array("id", "name", "kind", "contact_name", "phone", "email", "is_active")
            for supplier in db.scalars(select(Supplier).order_by(Supplier.name))
        ],
        "movements": [
            movement.to_api()
            for movement in db.scalars(
                select(RewardStockMovement).options(selectinload(RewardStockMovement.actor)).order_by(RewardStockMovement.id.desc())
            )
        ],
        "stocktakes": [stocktake.to_api() for stocktake in db.scalars(stocktake_query().order_by(Stocktake.id.desc()))],
        "redemptions": [
            redemption.to_api()
            for redemption in db.scalars(
                select(Redemption)
                .options(selectinload(Redemption.reward), selectinload(Redemption.variant))
                .order_by(Redemption.id.desc())
            )
        ],
        "restock_requests": [
            request.to_api() for request in db.scalars(select(RestockRequest).order_by(RestockRequest.id.desc()))
        ],
        "reservation_hours": setting_int(db, Setting.RESERVATION_HOURS),
    }


@router.get("/stock")
@attempt("Could not load stock.")
def stock(db: Db, staff: StockStaff):
    """Everything the inventory pages work from. Overdue reservations are expired first."""
    redemptions.expire_overdue(db)
    db.commit()
    return ok(snapshot(db), "Stock retrieved successfully")


def filtered_movements(filters: dict[str, Any]) -> list[Any]:
    conditions = []
    if filters.get("from"):
        conditions.append(from_day(RewardStockMovement.created_at, filters["from"]))
    if filters.get("to"):
        conditions.append(until_day(RewardStockMovement.created_at, filters["to"]))
    for name in ("type", "location_id", "reward_id", "actor_id"):
        if filters.get(name):
            conditions.append(getattr(RewardStockMovement, name) == filters[name])
    return conditions


@router.get("/stock-movements")
@attempt("Could not load stock movements.")
def stock_movements(db: Db, staff: StockStaff, data: Input):
    filters = validate(
        data,
        {
            "from": ["nullable", "date_format:Y-m-d"],
            "to": ["nullable", "date_format:Y-m-d"],
            "type": ["nullable", In(RewardStockMovement.TYPES)],
            "location_id": ["nullable", "integer"],
            "reward_id": ["nullable", "integer"],
            "actor_id": ["nullable", "integer"],
        },
    )
    conditions = filtered_movements(filters)
    statement = (
        select(RewardStockMovement)
        .options(selectinload(RewardStockMovement.actor))
        .where(*conditions)
        .order_by(RewardStockMovement.id.desc())
    )
    rows, meta = paginate(db, statement, data)
    # The page shows the net on-hand change of every matching entry, not just this page.
    net = db.scalar(select(func.coalesce(func.sum(RewardStockMovement.on_hand_delta), 0)).where(*conditions))
    return paginated([row.to_api() for row in rows], meta, "Stock movements retrieved successfully", {"net_on_hand": int(net)})


@router.post("/stock-movements", status_code=201)
@attempt("Could not record the stock movement.")
def record_movement(db: Db, staff: StockStaff, data: Input):
    """Record a receipt, adjustment, transfer, write-off or return."""
    kind = data.get("type")
    reason = "required" if kind in ("adjust", "writeoff", "return") else "nullable"
    fields = validate(
        data,
        {
            "type": ["required", In(AUTHORITY)],
            "reward_id": ["required", "integer", exists_in(db, Reward)],
            "variant_id": ["nullable", "integer"],
            "location_id": ["required", "integer", exists_in(db, StockLocation)],
            "quantity": ["required", "integer", "min:1", "max:100000"],
            "batch": ["nullable", "string", "max:64"],
            "expires_at": ["nullable", "date_format:Y-m-d"],
            "serials": ["nullable", "array", "max:1000"],
            "serials.*": ["string", "max:64"],
            "reference": ["nullable", "string", "max:64"],
            "redemption_id": ["nullable", "integer", exists_in(db, Redemption)],
            "refund_points": ["sometimes", "boolean"],
            "reason": [reason, "string", "max:255"],
            "source": [RequiredIf(kind == "receive"), In(["PURCHASE", "DONATION"])],
            "supplier_id": ["nullable", "integer", exists_in(db, Supplier)],
            "unit_cost": [RequiredIf(kind == "receive"), "numeric", "min:0", "max:9999999"],
            "direction": [RequiredIf(kind == "adjust"), In(["in", "out"])],
            "to_location_id": [RequiredIf(kind == "transfer"), "integer", "different:location_id", exists_in(db, StockLocation)],
            "kind": [RequiredIf(kind == "writeoff"), In(["DAMAGE", "LOSS", "EXPIRY"])],
        },
        {
            "reason.required": "A reason is required for this movement.",
            "quantity.min": "Enter a whole quantity of at least 1.",
            "quantity.max": "Quantity is too large.",
            "to_location_id.different": "Choose a different destination.",
            "unit_cost.min": "Enter a unit cost of zero or more.",
        },
    )
    require_stock_authority(staff, AUTHORITY[fields["type"]])
    handlers = {
        "receive": stock_ledger.receive,
        "adjust": stock_ledger.adjust,
        "transfer": stock_ledger.transfer,
        "writeoff": stock_ledger.write_off,
        "return": redemptions.return_to_stock if fields.get("redemption_id") else stock_ledger.return_stock,
    }
    movements = handlers[fields["type"]](db, staff, fields)
    redemptions.settle(db)
    log_staff_change(db, staff, "STOCK_MOVEMENT", "CREATED")
    db.commit()
    return ok([movement.to_api() for movement in movements], "Stock movement recorded successfully", 201)


# ---------------------------------------------------------------- Redemption fulfilment


def redemption_shape(db: Session, redemption: Redemption) -> dict[str, Any]:
    db.refresh(redemption)
    return redemption.to_api()


@router.post("/redemptions/{redemption_id:int}/fulfill")
@attempt("Could not fulfil the redemption.")
def fulfill(redemption_id: int, db: Db, staff: StockStaff, data: Input):
    redemption = find_or_404(db, Redemption, redemption_id)
    fields = validate(
        data,
        {"method": ["required", In([Redemption.SHIPPED, Redemption.PICKED_UP])], "serial": ["nullable", "string", "max:255"]},
    )
    redemption = redemptions.fulfill(db, redemption, fields["method"], fields.get("serial"), staff)
    db.commit()
    return ok(redemption_shape(db, redemption), "Redemption fulfilled")


@router.post("/redemptions/{redemption_id:int}/extend")
@attempt("Could not extend the reservation.")
def extend(redemption_id: int, db: Db, staff: StockStaff, data: Input):
    redemption = find_or_404(db, Redemption, redemption_id)
    hours = validate(data, {"hours": ["required", "integer", "min:1", "max:168"]})["hours"]
    redemption = redemptions.extend(db, redemption, hours, staff)
    db.commit()
    return ok(redemption_shape(db, redemption), "Reservation extended")


@router.post("/redemptions/{redemption_id:int}/cancel")
@attempt("Could not cancel the redemption.")
def cancel(redemption_id: int, db: Db, staff: StockManager, data: Input):
    redemption = find_or_404(db, Redemption, redemption_id)
    reason = validate(data, {"reason": ["required", "string", "max:255"]})["reason"]
    redemption = redemptions.cancel(db, redemption, reason, staff)
    db.commit()
    return ok(redemption_shape(db, redemption), "Redemption cancelled")


# ---------------------------------------------------------------- Stocktakes


def stocktake_query():
    return select(Stocktake).options(
        selectinload(Stocktake.lines), selectinload(Stocktake.creator), selectinload(Stocktake.poster)
    )


def stocktake_shape(db: Session, stocktake_id: int) -> dict[str, Any]:
    db.expire_all()
    return db.scalars(stocktake_query().where(Stocktake.id == stocktake_id)).one().to_api()


@router.post("/stocktakes", status_code=201)
@attempt("Could not start the stocktake.")
def start_stocktake(db: Db, staff: StockStaff, data: Input):
    """Open a count at a location, with the expected quantity of every batch, serial or plain item there."""
    fields = validate(
        data,
        {
            "location_id": ["required", "integer", exists_in(db, StockLocation)],
            "category": ["nullable", "string", "max:255"],
            "note": ["nullable", "string", "max:1000"],
        },
    )
    location = db.get(StockLocation, fields["location_id"])
    if not location.is_active:
        return refuse("Choose an active location.")
    if db.scalar(select(Stocktake.id).where(Stocktake.location_id == location.id, Stocktake.status == Stocktake.OPEN)):
        return refuse(f"{location.name} already has an open stocktake.")

    levels = stock_ledger.levels(db)
    statement = select(Reward).options(selectinload(Reward.stock_item), selectinload(Reward.variants)).order_by(Reward.id)
    if fields.get("category"):
        statement = statement.where(Reward.category == fields["category"])
    lines: list[dict[str, Any]] = []
    for reward in db.scalars(statement):
        tracking = reward.stock_settings().tracking
        for variant_id in reward.variant_ids():
            level = levels.get(stock_ledger.key(reward.id, variant_id, location.id))
            base = {"reward_id": reward.id, "variant_id": variant_id}
            if tracking == "batch":
                lines += [{**base, "batch": batch["batch"], "expected": batch["on_hand"]} for batch in (level or {}).get("batches", [])]
            elif tracking == "serial":
                lines += [{**base, "serial": serial, "expected": 1} for serial in (level or {}).get("serials", [])]
            elif level or reward.is_active:
                lines.append({**base, "expected": level["on_hand"] if level else 0})
    if not lines:
        return refuse("Nothing to count at this location for that category.")

    stocktake = Stocktake(
        location_id=location.id,
        created_by=staff.id,
        category=fields.get("category") or None,
        status=Stocktake.OPEN,
        note=(fields.get("note") or "").strip() or None,
        lines=[StocktakeLine(**line) for line in lines],
    )
    db.add(stocktake)
    db.flush()
    log_staff_change(db, staff, "STOCKTAKE", "CREATED")
    db.commit()
    return ok(stocktake_shape(db, stocktake.id), "Stocktake started successfully", 201)


@router.patch("/stocktakes/{stocktake_id:int}/lines/{line_id:int}")
@attempt("Could not record the count.")
def count_line(stocktake_id: int, line_id: int, db: Db, staff: StockStaff, data: Input):
    stocktake = find_or_404(db, Stocktake, stocktake_id)
    line = db.scalars(
        select(StocktakeLine).where(StocktakeLine.id == line_id, StocktakeLine.stocktake_id == stocktake.id)
    ).first()
    if line is None:
        raise HTTPException(404, f"No query results for model [StocktakeLine] {line_id}")
    if stocktake.status != Stocktake.OPEN:
        return refuse("This stocktake is closed.")
    counted = validate(
        data,
        {"counted": ["present", "nullable", "integer", "min:0", "max:1" if line.serial else "max:100000"]},
        {"counted.max": "Serial lines are counted as 0 or 1." if line.serial else "Enter a whole count of zero or more."},
    )["counted"]
    line.counted = counted
    log_staff_change(db, staff, "STOCKTAKE", "COUNT", stocktake.id)
    db.commit()
    return ok(line.to_api(), "Count recorded successfully")


@router.post("/stocktakes/{stocktake_id:int}/post")
@attempt("Could not post the stocktake.")
def post_stocktake(stocktake_id: int, db: Db, staff: StockManager):
    """Write the difference between each count and the current level to the ledger."""
    stocktake = db.scalars(stocktake_query().where(Stocktake.id == stocktake_id)).first() or find_or_404(
        db, Stocktake, stocktake_id
    )
    if stocktake.status != Stocktake.OPEN:
        return refuse("Only open stocktakes can be posted.")
    if any(line.counted is None for line in stocktake.lines):
        return refuse("Count every line before posting.")

    levels = stock_ledger.levels(db)
    rewards = {
        reward.id: reward
        for reward in db.scalars(
            select(Reward)
            .options(selectinload(Reward.stock_item))
            .where(Reward.id.in_({line.reward_id for line in stocktake.lines}))
            .with_for_update()
        )
    }
    variances = []
    for line in stocktake.lines:
        level = levels.get(stock_ledger.key(line.reward_id, line.variant_id, stocktake.location_id)) or {}
        if line.serial:
            system = 1 if line.serial in level.get("serials", []) else 0
        elif line.batch:
            system = next((batch["on_hand"] for batch in level.get("batches", []) if batch["batch"] == line.batch), 0)
        else:
            system = level.get("on_hand", 0)
        variance = line.counted - system
        if (
            line.serial
            and variance > 0
            and any(other["reward_id"] == line.reward_id and line.serial in other["serials"] for other in levels.values())
        ):
            # Raising rolls back the lines already updated.
            raise ValidationError({"lines": [f"Serial {line.serial} is recorded at another location. Transfer it instead."]})
        line.system_at_post = system
        if variance:
            variances.append((line, variance))

    for line, variance in variances:
        stock_ledger.record(
            db,
            staff,
            rewards[line.reward_id].stock_settings(),
            {
                "type": "COUNT",
                "reward_id": line.reward_id,
                "variant_id": line.variant_id,
                "location_id": stocktake.location_id,
                "reference": stocktake.reference(),
                "reason": f"Stocktake {stocktake.reference()} variance",
            },
            [{"qty": abs(variance), "batch": line.batch, "serials": [line.serial] if line.serial else None}],
            1 if variance > 0 else -1,
        )
    stocktake.status = Stocktake.POSTED
    stocktake.posted_by = staff.id
    stocktake.posted_at = clock.now()
    db.flush()
    redemptions.settle(db)
    log_staff_change(db, staff, "STOCKTAKE", "POST", stocktake.id)
    db.commit()
    return ok(stocktake_shape(db, stocktake.id), "Stocktake posted successfully")


@router.post("/stocktakes/{stocktake_id:int}/cancel")
@attempt("Could not cancel the stocktake.")
def cancel_stocktake(stocktake_id: int, db: Db, staff: StockManager):
    stocktake = find_or_404(db, Stocktake, stocktake_id)
    if stocktake.status != Stocktake.OPEN:
        return refuse("Only open stocktakes can be cancelled.")
    stocktake.status = Stocktake.CANCELLED
    log_staff_change(db, staff, "STOCKTAKE", "CANCEL", stocktake.id)
    db.commit()
    return ok(stocktake_shape(db, stocktake.id), "Stocktake cancelled successfully")


# ---------------------------------------------------------------- Stock rules, locations and suppliers


def variant_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The variants sent, trimmed, with a SKU made from the item's SKU where one is missing."""
    sku = str(data.get("sku") or "").strip().upper()
    rows = []
    for index, variant in enumerate(data.get("variants") or []):
        variant = variant if isinstance(variant, dict) else {}
        rows.append(
            {
                "id": int(variant["id"]) if variant.get("id") is not None else None,
                "size": str(variant.get("size") or "").strip(),
                "color": str(variant.get("color") or "").strip(),
                "sku": str(variant.get("sku") or "").strip().upper() or f"{sku}-{index + 1}",
            }
        )
    return rows


def reward_with_stock_rules(db: Session, reward_id: int) -> Reward | None:
    return db.scalars(
        select(Reward).options(selectinload(Reward.stock_item), selectinload(Reward.variants)).where(Reward.id == reward_id)
    ).first()


@router.put("/stock-items/{reward_id:int}")
@attempt("Could not save the stock rules.")
def save_stock_item(reward_id: int, db: Db, staff: StockManager, data: Input):
    """Save a reward's stock rules and variants."""
    reward = reward_with_stock_rules(db, reward_id) or find_or_404(db, Reward, reward_id)
    if isinstance(data.get("sku"), str):
        data["sku"] = data["sku"].strip().upper()

    def sku_taken(value) -> bool:
        return bool(
            db.scalar(select(RewardStockItem.id).where(RewardStockItem.sku == value, RewardStockItem.reward_id != reward.id))
        )

    def variants_check(validator: Validator) -> None:
        if validator.errors:
            return
        rows = variant_rows(data)
        if any(row["size"] == "" and row["color"] == "" for row in rows):
            validator.add_error("variants", "Each variant needs a size or colour.")
            return
        skus = [row["sku"] for row in rows]
        taken = bool(skus) and db.scalar(
            select(RewardVariant.id).where(RewardVariant.sku.in_(skus), RewardVariant.reward_id != reward.id)
        )
        if len(set(skus)) != len(skus) or taken:
            validator.add_error("variants", "Variant SKUs must be unique.")

    settings = (
        Validator(
            data,
            {
                "sku": ["required", "string", "max:64", Unique(sku_taken)],
                "unit_cost": ["required", "numeric", "min:0", "max:9999999"],
                "supplier_id": ["nullable", "integer", exists_in(db, Supplier)],
                "tracking": ["required", In(RewardStockItem.TRACKING)],
                "low_stock_threshold": ["required", "integer", "min:0", "max:1000000"],
                "reorder_point": ["required", "integer", "min:0", "max:1000000"],
                "reorder_qty": ["required", "integer", "min:1", "max:1000000"],
                "out_of_stock": ["required", In(["hide", "disable"])],
                "allow_backorder": ["required", "boolean"],
                "variants": ["present", "array", "max:100"],
                "variants.*.id": ["nullable", "integer", exists_in(db, RewardVariant, reward_id=reward.id)],
                "variants.*.size": ["nullable", "string", "max:40"],
                "variants.*.color": ["nullable", "string", "max:40"],
                "variants.*.sku": ["nullable", "string", "max:64"],
            },
            {"sku.unique": "Another reward already uses that SKU.", "reorder_qty.min": "Reorder quantity must be at least 1."},
        )
        .after(variants_check)
        .validate()
    )
    settings.pop("variants", None)
    variants = variant_rows(data)
    levels = list(stock_ledger.levels(db, reward.id).values())

    def holds(variant_id: int | None) -> bool:
        return any(level["variant_id"] == variant_id and (level["on_hand"] or level["reserved"]) for level in levels)

    kept_ids = [row["id"] for row in variants if row["id"]]
    kept = kept_ids if variants else [None]
    dropped = next((vid for vid in reward.variant_ids() if vid not in kept and holds(vid)), False)
    if dropped is not False:
        label = "Standard" if dropped is None else next(v for v in reward.variants if v.id == dropped).label()
        return refuse(f"{label} still has stock. Move it to zero first.")
    if settings["tracking"] != reward.stock_settings().tracking and any(level["on_hand"] or level["reserved"] for level in levels):
        return refuse("Tracking can only change while the item has no stock on hand or reserved.")

    item = reward.stock_item
    if item is None:
        item = RewardStockItem(reward_id=reward.id)
        db.add(item)
    for name, value in settings.items():
        setattr(item, name, value)
    db.execute(delete(RewardVariant).where(RewardVariant.reward_id == reward.id, RewardVariant.id.not_in(kept_ids)))
    for row in variants:
        fields = {"size": row["size"], "color": row["color"], "sku": row["sku"]}
        if row["id"]:
            variant = db.get(RewardVariant, row["id"])
            for name, value in fields.items():
                setattr(variant, name, value)
        else:
            db.add(RewardVariant(reward_id=reward.id, **fields))
    db.flush()
    log_staff_change(db, staff, "STOCK_ITEM", "UPDATED", reward.name)
    db.commit()
    db.expire_all()
    reward = reward_with_stock_rules(db, reward_id)
    return ok(
        {
            "reward_id": reward.id,
            "stock_item": reward.stock_settings().to_api(),
            "variants": [variant.to_api() for variant in reward.variants],
        },
        "Stock rules saved successfully",
    )


def location_fields(method: str, data: dict[str, Any]) -> dict[str, Any]:
    return validate(
        data,
        {
            "name": [presence(method), "string", "max:255"],
            "name_kh": ["nullable", "string", "max:255"],
            "type": [presence(method), In(StockLocation.TYPES)],
            "is_active": ["sometimes", "boolean"],
        },
        {"name.required": "Enter a location name.", "type.in": "Choose a location type."},
    )


@router.post("/stock-locations", status_code=201)
@attempt("Could not create the location.")
def create_location(db: Db, staff: StockManager, data: Input):
    location = StockLocation(**location_fields("POST", data))
    db.add(location)
    db.flush()
    log_staff_change(db, staff, "STOCK_LOCATION", "CREATED", location.name)
    db.commit()
    db.refresh(location)
    return ok(location.to_array(), "Location created successfully", 201)


@update_route(router, "/stock-locations/{location_id:int}", ("PUT", "PATCH"))
@attempt("Could not update the location.")
def update_location(location_id: int, request: Request, db: Db, staff: StockManager, data: Input):
    location = find_or_404(db, StockLocation, location_id)
    fields = location_fields(request.method, data)
    if location.is_active and fields.get("is_active", True) is False:
        held = any(
            level["location_id"] == location.id and (level["on_hand"] or level["reserved"])
            for level in stock_ledger.levels(db).values()
        )
        if held:
            return refuse(f"Transfer or write off {location.name}'s stock before deactivating it.")
        if db.scalar(select(func.count()).where(StockLocation.is_active.is_(True))) < 2:
            return refuse("Keep at least one active location.")
    for name, value in fields.items():
        setattr(location, name, value)
    db.flush()
    log_staff_change(db, staff, "STOCK_LOCATION", "UPDATED", location.name)
    db.commit()
    return ok(location.to_array(), "Location updated successfully")


def supplier_fields(method: str, data: dict[str, Any]) -> dict[str, Any]:
    return validate(
        data,
        {
            "name": [presence(method), "string", "max:255"],
            "kind": [presence(method), In(Supplier.KINDS)],
            "contact_name": ["nullable", "string", "max:255"],
            "phone": ["nullable", "string", "max:40"],
            "email": ["nullable", "email", "max:255"],
            "is_active": ["sometimes", "boolean"],
        },
        {"name.required": "Enter a supplier or partner name.", "email.email": "Enter a valid email address."},
    )


@router.post("/suppliers", status_code=201)
@attempt("Could not add the supplier.")
def create_supplier(db: Db, staff: StockManager, data: Input):
    supplier = Supplier(**supplier_fields("POST", data))
    db.add(supplier)
    db.flush()
    log_staff_change(db, staff, "SUPPLIER", "CREATED", supplier.name)
    db.commit()
    db.refresh(supplier)
    return ok(supplier.to_array(), "Supplier created successfully", 201)


@update_route(router, "/suppliers/{supplier_id:int}", ("PUT", "PATCH"))
@attempt("Could not update the supplier.")
def update_supplier(supplier_id: int, request: Request, db: Db, staff: StockManager, data: Input):
    supplier = find_or_404(db, Supplier, supplier_id)
    for name, value in supplier_fields(request.method, data).items():
        setattr(supplier, name, value)
    db.flush()
    log_staff_change(db, staff, "SUPPLIER", "UPDATED", supplier.name)
    db.commit()
    return ok(supplier.to_array(), "Supplier updated successfully")


@router.put("/stock-settings")
@attempt("Could not save the stock settings.")
def stock_settings(db: Db, staff: StockAdmin, data: Input):
    hours = validate(data, {"reservation_hours": ["required", "integer", "min:1", "max:720"]})["reservation_hours"]
    put_setting(db, Setting.RESERVATION_HOURS, hours)
    log_activity(db, "STOCK_SETTINGS_UPDATED", f"Reservation timeout set to {hours}h", staff=staff)
    db.commit()
    return ok({"reservation_hours": hours}, "Stock settings saved successfully")


routers = [router]

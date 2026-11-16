"""Product stock, kept in the main warehouse's inventory with a movement for every change."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Inventory, Order, Product, ProductStockMovement, Warehouse
from app.validation import fail


def main_warehouse(db: Session) -> Warehouse:
    warehouse = db.scalars(select(Warehouse).where(Warehouse.name == Warehouse.MAIN)).first()
    if warehouse is None:
        warehouse = Warehouse(name=Warehouse.MAIN)
        db.add(warehouse)
        db.flush()
    return warehouse


def _inventory_for(db: Session, product: Product) -> Inventory:
    warehouse_id = main_warehouse(db).id
    statement = select(Inventory).where(Inventory.product_id == product.id, Inventory.warehouse_id == warehouse_id)
    inventory = db.scalars(statement.with_for_update()).first()
    if inventory is None:
        inventory = Inventory(product_id=product.id, warehouse_id=warehouse_id, qty_on_hand=0, qty_reserved=0)
        db.add(inventory)
        db.flush()
    return inventory


def _move(db: Session, inventory: Inventory, change: int, kind: str, order: Order | None = None) -> None:
    inventory.qty_on_hand += change
    db.add(
        ProductStockMovement(
            product_id=inventory.product_id,
            warehouse_id=inventory.warehouse_id,
            type=kind,
            quantity=change,
            reference_type="App\\Models\\Order" if order else None,
            reference_id=order.id if order else None,
        )
    )
    db.flush()


def set_stock(db: Session, product: Product, quantity: int) -> None:
    """Set the available stock, recording the difference as an adjustment."""
    inventory = _inventory_for(db, product)
    change = quantity - inventory.available()
    if change != 0:
        _move(db, inventory, change, ProductStockMovement.ADJUST)


def take(db: Session, product: Product, quantity: int, order: Order) -> None:
    inventory = _inventory_for(db, product)
    if quantity > inventory.available():
        fail("items", f"Only {inventory.available()} of {product.name} left in stock.")
    _move(db, inventory, -quantity, ProductStockMovement.SALE, order)


def put_back(db: Session, product: Product, quantity: int, order: Order) -> None:
    _move(db, _inventory_for(db, product), quantity, ProductStockMovement.RETURN, order)

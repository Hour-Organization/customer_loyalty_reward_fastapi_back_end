"""Test data builders, like the Laravel model factories. Every builder commits, so the API sees its rows."""

import base64
import itertools
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app import clock
from app.auth import create_token, hash_password
from app.models import (
    Badge,
    Campaign,
    Category,
    Customer,
    Product,
    Reward,
    RewardStockItem,
    RewardVariant,
    Role,
    StockLocation,
    Supplier,
    Tier,
    User,
)
from app.services import product_stock, stock_ledger

# A 1×1 PNG, small enough for every upload rule.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_counter = itertools.count(1)


def unique() -> int:
    return next(_counter)


def image(name: str = "photo.png", content: bytes = PNG, kind: str = "image/png") -> tuple[str, bytes, str]:
    return (name, content, kind)


class Factory:
    def __init__(self, db: Session):
        self.db = db
        self._password = hash_password("password")

    def save(self, *rows: Any) -> Any:
        self.db.add_all(rows)
        self.db.commit()
        for row in rows:
            self.db.refresh(row)
        return rows[0] if len(rows) == 1 else rows

    def headers(self, owner: User | Customer) -> dict[str, str]:
        token = create_token(self.db, owner, "tests")
        self.db.commit()
        return {"Authorization": f"Bearer {token}"}

    # ------------------------------------------------------------ People

    def role(self, pages: list[str] | None = None, authority: str | None = "clerk", **fields: Any) -> Role:
        n = unique()
        role = Role(code=fields.pop("code", f"role_{n}"), label=fields.pop("label", f"Role {n}"), stock_authority=authority, **fields)
        if pages is not None:
            role.sync_pages(pages)
        return self.save(role)

    def super_role(self) -> Role:
        from sqlalchemy import select

        existing = self.db.scalars(select(Role).where(Role.code == Role.SUPER_ADMIN)).first()
        return existing or self.role(code=Role.SUPER_ADMIN, label="Super Admin", authority=None)

    def staff(self, pages: list[str] | None = None, authority: str = "clerk", **fields: Any) -> User:
        """A Super Admin, or a staff user whose role opens only `pages`."""
        role = self.super_role() if pages is None else self.role(pages, authority)
        n = unique()
        fields.setdefault("name", f"Staff {n}")
        fields.setdefault("email", f"staff{n}@example.com")
        return self.save(User(role_id=role.id, password=self._password, is_active=fields.pop("is_active", True), **fields))

    def tier(self, **fields: Any) -> Tier:
        n = unique()
        defaults = {"name": f"Tier {n}", "min_points": 0, "max_points": None, "multiplier": Decimal(1), "color": "#94a3b8"}
        return self.save(Tier(**{**defaults, **fields}))

    def customer(self, tier: Tier | None = None, points: int = 0, **fields: Any) -> Customer:
        n = unique()
        tier = tier or self.tier()
        defaults = {
            "name": f"Customer {n}",
            "email": f"customer{n}@example.com",
            "phone": "012345678",
            "password": self._password,
            "referral_code": f"CODE{n:04d}",
            "points_balance": points,
            "is_active": True,
        }
        return self.save(Customer(tier_id=tier.id, **{**defaults, **fields}))

    # ------------------------------------------------------------ Catalog

    def category(self, **fields: Any) -> Category:
        n = unique()
        return self.save(Category(**{"name": f"Category {n}", "is_active": True, **fields}))

    def product(self, category: Category | None = None, stock: int = 0, **fields: Any) -> Product:
        n = unique()
        category = category or self.category()
        price = Decimal(fields.pop("price", 10))
        defaults = {"name": f"Product {n}", "sku": f"PRD-T{n:04d}", "normal_points": int(price), "bonus_multiplier": Decimal(1)}
        product = self.save(Product(category_id=category.id, price=price, **{**defaults, **fields}))
        if stock:
            product_stock.set_stock(self.db, product, stock)
            self.db.commit()
        return product

    # ------------------------------------------------------------ Loyalty

    def campaign(self, **fields: Any) -> Campaign:
        n = unique()
        defaults = {
            "name": f"Campaign {n}",
            "multiplier": Decimal(2),
            "start_date": clock.today() - timedelta(days=1),
            "end_date": clock.today() + timedelta(days=7),
            "is_active": True,
            "show_on_home": False,
            "sort_order": 0,
        }
        return self.save(Campaign(**{**defaults, **fields}))

    def badge(self, **fields: Any) -> Badge:
        n = unique()
        defaults = {"code": f"BADGE_{n}", "name": f"Badge {n}", "icon": "medal", "color": "#f59e0b", "metric": "orders", "threshold": 1}
        return self.save(Badge(**{**defaults, **fields}))

    def reward(self, tier: Tier | None = None, **fields: Any) -> Reward:
        n = unique()
        tier = tier or self.tier()
        defaults = {"name": f"Reward {n}", "category": "Discount", "points_cost": 200, "is_active": True}
        return self.save(Reward(min_tier_id=tier.id, **{**defaults, **fields}))

    # ------------------------------------------------------------ Reward stock

    def location(self, **fields: Any) -> StockLocation:
        n = unique()
        return self.save(StockLocation(**{"name": f"Warehouse {n}", "type": "warehouse", "is_active": True, **fields}))

    def supplier(self, **fields: Any) -> Supplier:
        n = unique()
        return self.save(Supplier(**{"name": f"Supplier {n}", "kind": "supplier", "is_active": True, **fields}))

    def stock_item(self, reward: Reward, **fields: Any) -> RewardStockItem:
        n = unique()
        defaults = {
            "sku": f"RW-T{n:04d}",
            "unit_cost": Decimal("2.5"),
            "tracking": "none",
            "low_stock_threshold": 5,
            "reorder_point": 10,
            "reorder_qty": 25,
            "out_of_stock": "disable",
            "allow_backorder": False,
        }
        return self.save(RewardStockItem(reward_id=reward.id, **{**defaults, **fields}))

    def variant(self, reward: Reward, **fields: Any) -> RewardVariant:
        n = unique()
        return self.save(RewardVariant(reward_id=reward.id, **{"size": "Standard", "color": f"Colour {n}", "sku": f"VAR-{n:04d}", **fields}))

    def receive(self, staff: User, reward: Reward, quantity: int, location: StockLocation, **fields: Any) -> None:
        """Put stock straight into the ledger."""
        stock_ledger.receive(
            self.db,
            staff,
            {
                "reward_id": reward.id,
                "variant_id": None,
                "location_id": location.id,
                "quantity": quantity,
                "source": "PURCHASE",
                "unit_cost": Decimal(1),
                **fields,
            },
        )
        self.db.commit()

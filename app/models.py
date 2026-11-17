"""Database tables, following the same schema as the Laravel API (the "Assignment Ecommerce" ERD).

Commerce: categories, products, product_images, carts, orders, payments, shipments, addresses.
Postal areas (not in the Laravel API): provinces, districts, communes.
Product stock: warehouses, inventories, stock_movements, suppliers, purchase_orders.
Loyalty & rewards: tiers, tier_histories, loyalty_transactions, campaigns, badges, referrals, rewards
with their own stock ledger (reward_variants, reward_stock_items, stock_locations, reward_stock_movements,
stocktakes), redemptions and restock_requests.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    inspect,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app import clock
from app.serial import day, dec, file_url, is_external, iso, num, stamp


class Base(DeclarativeBase):
    # Columns left out of to_array(), like Eloquent's $hidden.
    __hidden__: tuple[str, ...] = ()

    id: Mapped[int] = mapped_column(primary_key=True)

    def to_array(self, *only: str, hide: tuple[str, ...] = ()) -> dict[str, Any]:
        """Every column formatted the way Eloquent's toArray() did, or just `only`."""
        out: dict[str, Any] = {}
        for column in self.__table__.columns:
            name = column.key
            if (only and name not in only) or name in self.__hidden__ or name in hide:
                continue
            value = getattr(self, name)
            if isinstance(column.type, Numeric) and value is not None:
                value = dec(value, column.type.scale or 2)
            elif isinstance(column.type, DateTime):
                value = stamp(value)
            elif isinstance(column.type, Date):
                value = day(value)
            out[name] = value
        if only:
            return {name: out[name] for name in only if name in out}
        return out


class Timestamps:
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=clock.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=clock.now, onupdate=clock.now)


class SoftDeletes:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)


def fk(target: str, ondelete: str | None = None, **kwargs) -> Any:
    return mapped_column(ForeignKey(target, ondelete=ondelete), **kwargs)


# ---------------------------------------------------------------- Staff, roles and tokens


class Role(Timestamps, SoftDeletes, Base):
    __tablename__ = "roles"

    SUPER_ADMIN = "super_admin"
    PAGES = [
        "dashboard", "analytics", "customers", "products", "categories", "orders",
        "points", "tiers", "campaigns", "rewards", "badges", "referrals",
        "stock-alerts", "stock", "stock-movements", "fulfillment", "stocktake", "stock-reports", "suppliers",
        "logs", "exchange-rate", "users", "roles", "profile",
    ]  # fmt: skip
    INVENTORY_PAGES = ["stock-alerts", "stock", "stock-movements", "fulfillment", "stocktake", "stock-reports", "suppliers"]
    STOCK_AUTHORITIES = ["clerk", "manager", "admin"]
    # Pages only Super Admin may have.
    RESERVED_PAGES = ["exchange-rate", "users", "roles"]
    # Every role can open its own profile.
    PROFILE_PAGE = "profile"

    code: Mapped[str] = mapped_column(String(255), unique=True)
    label: Mapped[str] = mapped_column(String(255))
    stock_authority: Mapped[str | None] = mapped_column(String(255))

    permissions: Mapped[list["RolePermission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan", passive_deletes=True, order_by="RolePermission.id"
    )

    def is_super_admin(self) -> bool:
        return self.code == self.SUPER_ADMIN

    def pages(self) -> list[str]:
        if self.is_super_admin():
            return list(self.PAGES)
        stored = {permission.page for permission in self.permissions}
        return [page for page in self.PAGES if page in stored]

    def sync_pages(self, pages: list[str]) -> None:
        """Store the pages, always adding the profile page and never the Super Admin ones."""
        wanted = [page for page in dict.fromkeys([*pages, self.PROFILE_PAGE]) if page not in self.RESERVED_PAGES]
        self.permissions = [RolePermission(page=page) for page in wanted]

    def to_api(self, users_count: int | None = None) -> dict[str, Any]:
        out = {
            "id": self.id,
            "code": self.code,
            "label": self.label,
            "stock_authority": self.stock_authority,
            "pages": self.pages(),
        }
        if users_count is not None:
            out["users_count"] = users_count
        return out


class RolePermission(Timestamps, Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[int] = fk("roles.id", "CASCADE")
    page: Mapped[str] = mapped_column(String(255))

    role: Mapped[Role] = relationship(back_populates="permissions")


class User(Timestamps, Base):
    """A back-office staff user."""

    __tablename__ = "users"
    __hidden__ = ("password", "remember_token")

    role_id: Mapped[int | None] = fk("roles.id", "SET NULL", nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    remember_token: Mapped[str | None] = mapped_column(String(100))

    role: Mapped[Role | None] = relationship()

    def is_super_admin(self) -> bool:
        return bool(self.role and self.role.is_super_admin())

    def can_access_page(self, page: str) -> bool:
        return page in (self.role.pages() if self.role else [])

    def stock_authority(self) -> str | None:
        """Super Admin is a stock admin; a role with an inventory page acts at its own level, a clerk by default."""
        if self.is_super_admin():
            return "admin"
        pages = self.role.pages() if self.role else []
        if not set(Role.INVENTORY_PAGES) & set(pages):
            return None
        return self.role.stock_authority or "clerk"

    def has_stock_authority(self, level: str) -> bool:
        authority = self.stock_authority()
        return authority is not None and Role.STOCK_AUTHORITIES.index(authority) >= Role.STOCK_AUTHORITIES.index(level)

    def to_api(self) -> dict[str, Any]:
        role = self.role
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "is_active": self.is_active,
            "role": (
                {"id": role.id, "code": role.code, "label": role.label, "stock_authority": role.stock_authority}
                if role
                else None
            ),
            "pages": role.pages() if role else [],
        }


class PersonalAccessToken(Timestamps, Base):
    """API tokens in Laravel Sanctum's format: the client holds "<id>|<secret>", the table its SHA-256."""

    __tablename__ = "personal_access_tokens"

    tokenable_type: Mapped[str] = mapped_column(String(255), index=True)
    tokenable_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(Text)
    token: Mapped[str] = mapped_column(String(64), unique=True)
    abilities: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)


class Setting(Timestamps, Base):
    __tablename__ = "settings"

    EXCHANGE_RATE = "exchange_rate"
    RESERVATION_HOURS = "reservation_hours"
    DEFAULTS = {EXCHANGE_RATE: 4000, RESERVATION_HOURS: 72}

    key: Mapped[str] = mapped_column(String(255), unique=True)
    value: Mapped[str | None] = mapped_column(Text)


class ActivityLog(Timestamps, Base):
    """One entry of the activity log. Entries are only ever added."""

    __tablename__ = "activity_logs"

    customer_id: Mapped[int | None] = fk("customers.id", "SET NULL", nullable=True)
    user_id: Mapped[int | None] = fk("users.id", "SET NULL", nullable=True)
    event: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)

    customer: Mapped["Customer | None"] = relationship()
    user: Mapped[User | None] = relationship()

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "user_id": self.user_id,
            "actor_name": (self.user.name if self.user else None) or (self.customer.name if self.customer else None) or "System",
            "event": self.event,
            "description": self.description,
            "created_at": iso(self.created_at),
        }


# ---------------------------------------------------------------- Loyalty


class Tier(Timestamps, Base):
    __tablename__ = "tiers"

    name: Mapped[str] = mapped_column(String(255), unique=True)
    min_points: Mapped[int] = mapped_column(Integer, default=0)
    max_points: Mapped[int | None] = mapped_column(Integer)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal(1))
    color: Mapped[str | None] = mapped_column(String(255))


class Campaign(Timestamps, Base):
    __tablename__ = "campaigns"

    BANNER_FIELDS = (
        "id", "name", "name_kh", "banner_title", "banner_subtitle", "button_label", "link_url",
        "image_url", "start_date", "end_date", "multiplier",
    )  # fmt: skip

    name: Mapped[str] = mapped_column(String(255))
    name_kh: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    image: Mapped[str | None] = mapped_column(String(255))
    banner_title: Mapped[str | None] = mapped_column(String(255))
    banner_subtitle: Mapped[str | None] = mapped_column(String(255))
    button_label: Mapped[str | None] = mapped_column(String(255))
    link_url: Mapped[str | None] = mapped_column(String(2048))
    multiplier: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal(1))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    show_on_home: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    def has_uploaded_image(self) -> bool:
        return self.image is not None and not is_external(self.image)

    def to_array(self, *only: str, hide: tuple[str, ...] = ()) -> dict[str, Any]:
        out = super().to_array(*only, hide=hide)
        if not only or "image_url" in only:
            out["image_url"] = file_url(self.image)
        return out

    def to_banner(self) -> dict[str, Any]:
        data = self.to_array()
        return {name: data[name] for name in self.BANNER_FIELDS}


class Customer(Timestamps, Base):
    """A shopper and loyalty member."""

    __tablename__ = "customers"
    __hidden__ = ("password",)

    tier_id: Mapped[int] = fk("tiers.id")
    tier_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str] = mapped_column(String(255))
    name_kh: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    phone: Mapped[str | None] = mapped_column(String(255))
    password: Mapped[str] = mapped_column(String(255))
    province: Mapped[str | None] = mapped_column(String(255))
    avatar: Mapped[str | None] = mapped_column(String(255))
    referral_code: Mapped[str] = mapped_column(String(255), unique=True)
    points_balance: Mapped[int] = mapped_column(Integer, default=0)
    qualifying_points: Mapped[int] = mapped_column(Integer, default=0)
    total_spent: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal(0))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    tier: Mapped[Tier] = relationship()
    badges: Mapped[list["Badge"]] = relationship(secondary="customer_badges", viewonly=True, order_by="Badge.id")


class Address(Timestamps, Base):
    __tablename__ = "addresses"

    customer_id: Mapped[int] = fk("customers.id", "CASCADE")
    street: Mapped[str] = mapped_column(String(255))
    city: Mapped[str] = mapped_column(String(255))
    country: Mapped[str] = mapped_column(String(255))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


# Cambodia Post's postal areas (app/data/cambodia_postal_codes.csv, loaded by `python -m app.cli import-postal-codes`).
# Codes are six digits: PP0000 a province, PPDD00 a district, PPDDCC a commune.


class Province(Timestamps, Base):
    __tablename__ = "provinces"

    code: Mapped[str] = mapped_column(String(6), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    name_kh: Mapped[str] = mapped_column(String(255))

    districts: Mapped[list["District"]] = relationship(back_populates="province", order_by="District.code")


class District(Timestamps, Base):
    __tablename__ = "districts"

    province_id: Mapped[int] = fk("provinces.id", "CASCADE", index=True)
    code: Mapped[str] = mapped_column(String(6), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    name_kh: Mapped[str] = mapped_column(String(255))

    province: Mapped[Province] = relationship(back_populates="districts")
    communes: Mapped[list["Commune"]] = relationship(back_populates="district", order_by="Commune.code")


class Commune(Timestamps, Base):
    __tablename__ = "communes"

    district_id: Mapped[int] = fk("districts.id", "CASCADE", index=True)
    code: Mapped[str] = mapped_column(String(6), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    name_kh: Mapped[str] = mapped_column(String(255))

    district: Mapped[District] = relationship(back_populates="communes")


class Referral(Timestamps, Base):
    __tablename__ = "referrals"

    referrer_id: Mapped[int] = fk("customers.id", "CASCADE")
    # Each customer is referred at most once.
    referred_id: Mapped[int] = fk("customers.id", "CASCADE", unique=True)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False)

    referrer: Mapped[Customer] = relationship(foreign_keys=[referrer_id])
    referred: Mapped[Customer] = relationship(foreign_keys=[referred_id])

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "referrer_id": self.referrer_id,
            "referrer_name": self.referrer.name if self.referrer else None,
            "referred_id": self.referred_id,
            "referred_name": self.referred.name if self.referred else None,
            "completed": self.is_completed,
            "created_at": day(self.created_at),
        }


class Badge(Timestamps, Base):
    __tablename__ = "badges"

    METRICS = ["orders", "spend", "referrals", "qualifyingPoints", "membershipDays"]
    ICONS = ["medal", "star", "trophy", "gift", "cart", "heart", "gem", "users", "rocket"]

    code: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    name_kh: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(String(255))
    color: Mapped[str | None] = mapped_column(String(255))
    metric: Mapped[str] = mapped_column(String(255))
    threshold: Mapped[int] = mapped_column(Integer, default=0)


class CustomerBadge(Timestamps, Base):
    __tablename__ = "customer_badges"

    customer_id: Mapped[int] = fk("customers.id", "CASCADE")
    badge_id: Mapped[int] = fk("badges.id", "CASCADE")
    awarded_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)


class TierHistory(Timestamps, Base):
    __tablename__ = "tier_histories"

    customer_id: Mapped[int] = fk("customers.id", "CASCADE")
    from_tier_id: Mapped[int] = fk("tiers.id")
    to_tier_id: Mapped[int] = fk("tiers.id")
    reason: Mapped[str | None] = mapped_column(String(255))

    from_tier: Mapped[Tier] = relationship(foreign_keys=[from_tier_id])
    to_tier: Mapped[Tier] = relationship(foreign_keys=[to_tier_id])

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "from": self.from_tier.name if self.from_tier else None,
            "to": self.to_tier.name if self.to_tier else None,
            "reason": self.reason,
            "created_at": iso(self.created_at),
        }


class LoyaltyTransaction(Timestamps, Base):
    """One row of the points ledger, with the balance before and after it."""

    __tablename__ = "loyalty_transactions"

    TYPE_EARN = "EARN"
    TYPE_SPEND = "SPEND"
    TYPE_ADJUST = "ADJUST"
    TYPE_REFERRAL = "REFERRAL"
    TYPE_REFUND = "REFUND"
    TYPES = [TYPE_EARN, TYPE_SPEND, TYPE_ADJUST, TYPE_REFERRAL, TYPE_REFUND]

    customer_id: Mapped[int] = fk("customers.id", "RESTRICT")
    order_id: Mapped[int | None] = fk("orders.id", "SET NULL", nullable=True)
    created_by: Mapped[int | None] = fk("users.id", "SET NULL", nullable=True)
    type: Mapped[str] = mapped_column(String(255))
    points: Mapped[int] = mapped_column(Integer)
    balance_before: Mapped[int] = mapped_column(Integer)
    balance_after: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(String(255))

    customer: Mapped[Customer] = relationship()
    creator: Mapped[User | None] = relationship()

    def with_people(self) -> dict[str, Any]:
        """The row with its customer and the staff user who made it, as the points pages show it."""
        return {
            **self.to_array(),
            "customer": self.customer.to_array("id", "name", "email") if self.customer else None,
            "creator": self.creator.to_array("id", "name") if self.creator else None,
        }


# ---------------------------------------------------------------- Catalog


class Category(Timestamps, SoftDeletes, Base):
    __tablename__ = "categories"

    parent_id: Mapped[int | None] = fk("categories.id", "SET NULL", nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    name_kh: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Product(Timestamps, SoftDeletes, Base):
    __tablename__ = "products"

    MAX_IMAGES = 8

    category_id: Mapped[int] = fk("categories.id")
    name: Mapped[str] = mapped_column(String(255))
    name_kh: Mapped[str | None] = mapped_column(String(255))
    sku: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    cost_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal(0))
    reorder_level: Mapped[int] = mapped_column(Integer, default=0)
    normal_points: Mapped[int] = mapped_column(Integer, default=0)
    bonus_multiplier: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal(1))
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False)

    category: Mapped[Category] = relationship()
    images: Mapped[list["ProductImage"]] = relationship(
        order_by=lambda: [ProductImage.sort_order, ProductImage.id], cascade="all, delete-orphan", passive_deletes=True
    )
    inventories: Mapped[list["Inventory"]] = relationship(passive_deletes=True)

    def stock(self) -> int:
        return sum(inventory.qty_on_hand - inventory.qty_reserved for inventory in self.inventories)

    def to_api(self, hide: tuple[str, ...] = ()) -> dict[str, Any]:
        """The product with its category, images and available stock."""
        category = self.category
        return {
            **self.to_array(hide=hide),
            "stock": self.stock(),
            "category": category.to_array("id", "name", "name_kh", "is_active") if category else None,
            "images": [image.to_api() for image in self.images],
        }


class ProductImage(Timestamps, Base):
    __tablename__ = "product_images"

    product_id: Mapped[int] = fk("products.id", "CASCADE")
    path: Mapped[str] = mapped_column(String(255))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    def to_api(self) -> dict[str, Any]:
        return {"id": self.id, "sort_order": self.sort_order, "url": file_url(self.path)}


class Warehouse(Timestamps, Base):
    __tablename__ = "warehouses"

    MAIN = "Main Warehouse"

    name: Mapped[str] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Supplier(Timestamps, Base):
    __tablename__ = "suppliers"

    KINDS = ["supplier", "partner"]

    name: Mapped[str] = mapped_column(String(255))
    contact_name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    kind: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Inventory(Timestamps, Base):
    __tablename__ = "inventories"

    product_id: Mapped[int] = fk("products.id", "CASCADE")
    warehouse_id: Mapped[int] = fk("warehouses.id", "CASCADE")
    qty_on_hand: Mapped[int] = mapped_column(Integer, default=0)
    qty_reserved: Mapped[int] = mapped_column(Integer, default=0)

    def available(self) -> int:
        return self.qty_on_hand - self.qty_reserved


class ProductStockMovement(Timestamps, Base):
    """A change to a product's on-hand stock."""

    __tablename__ = "stock_movements"

    SALE = "SALE"
    RETURN = "RETURN"
    ADJUST = "ADJUST"

    product_id: Mapped[int] = fk("products.id")
    warehouse_id: Mapped[int] = fk("warehouses.id")
    type: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[int] = mapped_column(Integer)
    reference_type: Mapped[str | None] = mapped_column(String(255))
    reference_id: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)


class PurchaseOrder(Timestamps, Base):
    __tablename__ = "purchase_orders"

    supplier_id: Mapped[int] = fk("suppliers.id")
    warehouse_id: Mapped[int] = fk("warehouses.id")
    status: Mapped[str] = mapped_column(String(255))
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime)
    received_at: Mapped[datetime | None] = mapped_column(DateTime)


class PurchaseOrderItem(Timestamps, Base):
    __tablename__ = "purchase_order_items"

    purchase_order_id: Mapped[int] = fk("purchase_orders.id", "CASCADE")
    product_id: Mapped[int] = fk("products.id")
    quantity: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class Cart(Timestamps, Base):
    __tablename__ = "carts"

    customer_id: Mapped[int] = fk("customers.id", "CASCADE")


class CartItem(Timestamps, Base):
    __tablename__ = "cart_items"

    cart_id: Mapped[int] = fk("carts.id", "CASCADE")
    product_id: Mapped[int] = fk("products.id", "CASCADE")
    quantity: Mapped[int] = mapped_column(Integer, default=1)


# ---------------------------------------------------------------- Orders


class Order(Timestamps, Base):
    __tablename__ = "orders"

    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    PROCESSING = "PROCESSING"
    SHIPPED = "SHIPPED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    # The statuses each status can move to.
    TRANSITIONS = {
        PENDING: [CONFIRMED, CANCELLED],
        CONFIRMED: [PROCESSING, CANCELLED],
        PROCESSING: [SHIPPED, CANCELLED],
        SHIPPED: [COMPLETED],
        COMPLETED: [],
        CANCELLED: [],
    }

    customer_id: Mapped[int] = fk("customers.id")
    address_id: Mapped[int] = fk("addresses.id")
    status: Mapped[str] = mapped_column(String(255))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    ordered_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    points_earned: Mapped[int] = mapped_column(Integer, default=0)
    delivery_phone: Mapped[str | None] = mapped_column(String(255))

    customer: Mapped[Customer] = relationship()
    address: Mapped[Address] = relationship()
    items: Mapped[list["OrderItem"]] = relationship(order_by="OrderItem.id", passive_deletes=True)
    payment: Mapped["Payment | None"] = relationship(uselist=False, passive_deletes=True)
    shipment: Mapped["Shipment | None"] = relationship(uselist=False, passive_deletes=True)

    def reference(self) -> str:
        return f"ORD-{self.id:06d}"


class OrderItem(Timestamps, Base):
    __tablename__ = "order_items"

    order_id: Mapped[int] = fk("orders.id", "CASCADE")
    product_id: Mapped[int] = fk("products.id")
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    base_points: Mapped[int] = mapped_column(Integer, default=0)
    bonus_multiplier: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal(1))


class Payment(Timestamps, Base):
    __tablename__ = "payments"

    CASH_ON_DELIVERY = "CASH_ON_DELIVERY"
    KHQR = "KHQR"
    METHODS = (CASH_ON_DELIVERY, KHQR)

    PENDING = "PENDING"
    PAID = "PAID"
    CANCELLED = "CANCELLED"
    # Online payments only: the provider refused it, the QR ran out unpaid, or a paid order was cancelled.
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    REFUND_DUE = "REFUND_DUE"

    order_id: Mapped[int] = fk("orders.id", "CASCADE")
    method: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Online payments only. `provider_ref` is what the provider knows the payment by (the KHQR's MD5 for
    # Bakong), `qr_payload` the KHQR string the customer scans.
    provider: Mapped[str | None] = mapped_column(String(255))
    provider_ref: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD", server_default="USD")
    qr_payload: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    def is_online(self) -> bool:
        return self.method != self.CASH_ON_DELIVERY

    def awaiting_online(self) -> bool:
        return self.is_online() and self.status == self.PENDING


class Shipment(Timestamps, Base):
    __tablename__ = "shipments"

    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"

    order_id: Mapped[int] = fk("orders.id", "CASCADE")
    carrier: Mapped[str | None] = mapped_column(String(255))
    tracking_no: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(255))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime)


# ---------------------------------------------------------------- Rewards and their stock


class Reward(Timestamps, Base):
    __tablename__ = "rewards"

    min_tier_id: Mapped[int] = fk("tiers.id")
    name: Mapped[str] = mapped_column(String(255))
    name_kh: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(255))
    points_cost: Mapped[int] = mapped_column(Integer)
    # Unused: stock comes from the ledger. Kept so the schema matches the ERD.
    stock: Mapped[int] = mapped_column(Integer, default=0)
    image: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[date | None] = mapped_column(Date)

    min_tier: Mapped[Tier] = relationship()
    stock_item: Mapped["RewardStockItem | None"] = relationship(uselist=False, passive_deletes=True)
    variants: Mapped[list["RewardVariant"]] = relationship(order_by="RewardVariant.id", passive_deletes=True)

    def stock_settings(self) -> "RewardStockItem":
        """The stock rules, or the defaults when none are saved yet."""
        return self.stock_item or RewardStockItem.defaults_for(self)

    def variant_ids(self) -> list[int | None]:
        """The variants a redemption or movement can name; [None] when the reward has none."""
        return [variant.id for variant in self.variants] or [None]

    def has_uploaded_image(self) -> bool:
        return self.image is not None and not is_external(self.image)

    def to_array(self, *only: str, hide: tuple[str, ...] = ()) -> dict[str, Any]:
        out = super().to_array(*only, hide=hide)
        if not only or "image_url" in only:
            out["image_url"] = file_url(self.image)
        return out


class RewardVariant(Timestamps, Base):
    __tablename__ = "reward_variants"

    reward_id: Mapped[int] = fk("rewards.id", "CASCADE")
    size: Mapped[str | None] = mapped_column(String(255))
    color: Mapped[str | None] = mapped_column(String(255))
    sku: Mapped[str] = mapped_column(String(255), unique=True)

    def label(self) -> str:
        return " / ".join(part for part in (self.size, self.color) if part) or self.sku

    def to_api(self) -> dict[str, Any]:
        return {"id": self.id, "size": self.size, "color": self.color, "sku": self.sku}


class RewardStockItem(Timestamps, Base):
    """A reward's stock rules (1:1)."""

    __tablename__ = "reward_stock_items"

    TRACKING = ["none", "batch", "serial"]

    reward_id: Mapped[int] = fk("rewards.id", "CASCADE", unique=True)
    supplier_id: Mapped[int | None] = fk("suppliers.id", "SET NULL", nullable=True)
    sku: Mapped[str] = mapped_column(String(255), unique=True)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    tracking: Mapped[str] = mapped_column(String(255))
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=0)
    reorder_point: Mapped[int] = mapped_column(Integer, default=0)
    reorder_qty: Mapped[int] = mapped_column(Integer, default=0)
    out_of_stock: Mapped[str] = mapped_column(String(255))
    allow_backorder: Mapped[bool] = mapped_column(Boolean, default=False)

    @classmethod
    def defaults_for(cls, reward: Reward) -> "RewardStockItem":
        """Unsaved default rules for a reward that has none."""
        return cls(
            reward_id=reward.id,
            sku=f"RW-{reward.id}",
            unit_cost=Decimal(0),
            tracking="none",
            low_stock_threshold=5,
            reorder_point=10,
            reorder_qty=25,
            out_of_stock="disable",
            allow_backorder=False,
        )

    def to_api(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "unit_cost": num(self.unit_cost),
            "supplier_id": self.supplier_id,
            "tracking": self.tracking,
            "low_stock_threshold": int(self.low_stock_threshold or 0),
            "reorder_point": int(self.reorder_point or 0),
            "reorder_qty": int(self.reorder_qty or 0),
            "out_of_stock": self.out_of_stock,
            "allow_backorder": bool(self.allow_backorder),
        }


class StockLocation(Timestamps, Base):
    __tablename__ = "stock_locations"

    TYPES = ["warehouse", "store", "pickup"]

    name: Mapped[str] = mapped_column(String(255))
    name_kh: Mapped[str | None] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class RewardStockMovement(Timestamps, Base):
    """One entry of the reward stock ledger. Levels are the sum of the entries."""

    __tablename__ = "reward_stock_movements"

    TYPES = [
        "RECEIVE", "RESERVE", "RELEASE", "FULFILL", "RETURN", "ADJUST",
        "TRANSFER_OUT", "TRANSFER_IN", "DAMAGE", "LOSS", "EXPIRY", "COUNT",
    ]  # fmt: skip

    reward_id: Mapped[int] = fk("rewards.id", "RESTRICT")
    variant_id: Mapped[int | None] = fk("reward_variants.id", "SET NULL", nullable=True)
    location_id: Mapped[int] = fk("stock_locations.id", "RESTRICT")
    supplier_id: Mapped[int | None] = fk("suppliers.id", "SET NULL", nullable=True)
    actor_id: Mapped[int | None] = fk("users.id", nullable=True)
    type: Mapped[str] = mapped_column(String(255))
    on_hand_delta: Mapped[int] = mapped_column(Integer, default=0)
    reserved_delta: Mapped[int] = mapped_column(Integer, default=0)
    batch: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[date | None] = mapped_column(Date)
    serials: Mapped[list[str] | None] = mapped_column(JSON)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    source: Mapped[str | None] = mapped_column(String(255))
    reference: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(String(255))

    actor: Mapped[User | None] = relationship()

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": iso(self.created_at),
            "type": self.type,
            "reward_id": self.reward_id,
            "variant_id": self.variant_id,
            "location_id": self.location_id,
            "on_hand_delta": int(self.on_hand_delta),
            "reserved_delta": int(self.reserved_delta),
            "batch": self.batch,
            "expires_at": day(self.expires_at),
            "serials": self.serials,
            "unit_cost": num(self.unit_cost),
            "supplier_id": self.supplier_id,
            "source": self.source,
            "reference": self.reference,
            "reason": self.reason,
            "actor_id": self.actor_id,
            "actor_name": self.actor.name if self.actor else None,
        }


class Redemption(Timestamps, Base):
    """Points spent on a reward, and where its unit is in fulfilment."""

    __tablename__ = "redemptions"

    RESERVED = "RESERVED"
    BACKORDERED = "BACKORDERED"
    FULFILLED = "FULFILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    RETURNED = "RETURNED"
    # Still waiting to be handed over.
    OPEN = [RESERVED, BACKORDERED]
    # Fulfilment methods; NOT_FULFILLED is stored until the unit is handed over.
    NOT_FULFILLED = "PENDING"
    SHIPPED = "SHIPPED"
    PICKED_UP = "PICKED_UP"

    customer_id: Mapped[int] = fk("customers.id", "RESTRICT")
    reward_id: Mapped[int] = fk("rewards.id", "RESTRICT")
    variant_id: Mapped[int | None] = fk("reward_variants.id", "SET NULL", nullable=True)
    location_id: Mapped[int | None] = fk("stock_locations.id", "SET NULL", nullable=True)
    points: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(255))
    fulfillment: Mapped[str] = mapped_column(String(255))
    reserved_until: Mapped[datetime | None] = mapped_column(DateTime)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime)
    batch: Mapped[str | None] = mapped_column(String(255))
    serial: Mapped[str | None] = mapped_column(String(255))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    close_reason: Mapped[str | None] = mapped_column(String(255))

    customer: Mapped[Customer] = relationship()
    reward: Mapped[Reward] = relationship()
    variant: Mapped[RewardVariant | None] = relationship()

    def reference(self) -> str:
        return f"RDM-{self.id:06d}"

    def reward_name(self) -> str:
        name = self.reward.name if self.reward else "Reward"
        return f"{name} ({self.variant.label()})" if self.variant else name

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "reward_id": self.reward_id,
            "reward_name": self.reward_name(),
            "variant_id": self.variant_id,
            "location_id": self.location_id,
            "points": self.points,
            "status": self.status,
            "fulfillment": None if self.fulfillment == self.NOT_FULFILLED else self.fulfillment,
            "created_at": iso(self.created_at),
            "reserved_until": iso(self.reserved_until),
            "fulfilled_at": iso(self.fulfilled_at),
            "batch": self.batch,
            "serial": self.serial,
            "closed_at": iso(self.closed_at),
            "close_reason": self.close_reason,
        }


class RestockRequest(Timestamps, Base):
    """A customer asking to hear when a sold-out reward is back."""

    __tablename__ = "restock_requests"

    customer_id: Mapped[int] = fk("customers.id", "CASCADE")
    reward_id: Mapped[int] = fk("rewards.id", "CASCADE")
    variant_id: Mapped[int | None] = fk("reward_variants.id", "CASCADE", nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)

    customer: Mapped[Customer] = relationship()
    reward: Mapped[Reward] = relationship()

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "reward_id": self.reward_id,
            "variant_id": self.variant_id,
            "created_at": iso(self.created_at),
            "notified_at": iso(self.notified_at),
        }


class Stocktake(Timestamps, Base):
    __tablename__ = "stocktakes"

    OPEN = "OPEN"
    POSTED = "POSTED"
    CANCELLED = "CANCELLED"

    location_id: Mapped[int] = fk("stock_locations.id")
    created_by: Mapped[int] = fk("users.id")
    posted_by: Mapped[int | None] = fk("users.id", "SET NULL", nullable=True)
    category: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(255))
    note: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)

    lines: Mapped[list["StocktakeLine"]] = relationship(
        order_by="StocktakeLine.id", cascade="all, delete-orphan", passive_deletes=True
    )
    creator: Mapped[User] = relationship(foreign_keys=[created_by])
    poster: Mapped[User | None] = relationship(foreign_keys=[posted_by])

    def reference(self) -> str:
        return f"ST-{self.id:06d}"

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "reference": self.reference(),
            "location_id": self.location_id,
            "category": self.category,
            "status": self.status,
            "note": self.note,
            "created_at": iso(self.created_at),
            "created_by": self.creator.name if self.creator else None,
            "posted_at": iso(self.posted_at),
            "posted_by": self.poster.name if self.poster else None,
            "lines": [line.to_api() for line in self.lines],
        }


class StocktakeLine(Timestamps, Base):
    __tablename__ = "stocktake_lines"

    stocktake_id: Mapped[int] = fk("stocktakes.id", "CASCADE")
    reward_id: Mapped[int] = fk("rewards.id")
    variant_id: Mapped[int | None] = fk("reward_variants.id", "SET NULL", nullable=True)
    batch: Mapped[str | None] = mapped_column(String(255))
    serial: Mapped[str | None] = mapped_column(String(255))
    expected: Mapped[int] = mapped_column(Integer, default=0)
    counted: Mapped[int | None] = mapped_column(Integer)
    system_at_post: Mapped[int | None] = mapped_column(Integer)

    def to_api(self) -> dict[str, Any]:
        return self.to_array("id", "reward_id", "variant_id", "batch", "serial", "expected", "counted", "system_at_post")


def is_new(instance: Base) -> bool:
    return inspect(instance).transient or inspect(instance).pending

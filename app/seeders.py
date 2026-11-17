"""Demo data, the same as the Laravel DatabaseSeeder. Safe to run more than once."""

import calendar
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.auth import hash_password
from app.models import (
    Badge,
    Campaign,
    Category,
    Customer,
    Inventory,
    LoyaltyTransaction,
    Order,
    Payment,
    Product,
    ProductImage,
    Referral,
    Reward,
    RewardStockItem,
    RewardStockMovement,
    RewardVariant,
    Role,
    StockLocation,
    Supplier,
    Tier,
    User,
)
from app.services import orders, product_stock
from app.services.loyalty import record_points
from app.services.postal_codes import import_postal_codes

STOCK_PAGES = ["stock-alerts", "stock", "stock-movements", "fulfillment", "stocktake", "stock-reports", "suppliers"]

ROLES = [
    {"code": Role.SUPER_ADMIN, "label": "Super Admin", "stock_authority": None, "pages": []},
    {"code": "marketing_manager", "label": "Marketing Manager", "stock_authority": "clerk",
     "pages": ["dashboard", "analytics", "products", "categories", "campaigns", "rewards"]},
    {"code": "loyalty_manager", "label": "Loyalty Program Manager", "stock_authority": "clerk",
     "pages": ["dashboard", "customers", "points", "tiers", "rewards", "badges", "referrals", "logs", "analytics"]},
    {"code": "stock_manager", "label": "Stock Manager", "stock_authority": "manager",
     "pages": ["dashboard", "rewards", *STOCK_PAGES]},
    {"code": "stock_clerk", "label": "Stock Clerk", "stock_authority": "clerk",
     "pages": ["stock-alerts", "stock", "stock-movements", "fulfillment", "stocktake"]},
]  # fmt: skip

TIERS = [
    {"name": "Bronze", "min_points": 0, "max_points": 999, "multiplier": Decimal("1.00"), "color": "#CD7F32"},
    {"name": "Silver", "min_points": 1000, "max_points": 4999, "multiplier": Decimal("1.25"), "color": "#C0C0C0"},
    {"name": "Gold", "min_points": 5000, "max_points": 9999, "multiplier": Decimal("1.50"), "color": "#FFD700"},
    {"name": "Platinum", "min_points": 10000, "max_points": None, "multiplier": Decimal("2.00"), "color": "#E5E4E2"},
]

BADGES = [
    {"code": "FIRST_PURCHASE", "name": "First Purchase", "name_kh": "ការទិញលើកដំបូង", "description": "Complete your first order", "icon": "cart", "color": "#3b82f6", "metric": "orders", "threshold": 1},
    {"code": "LOYAL_CUSTOMER", "name": "Loyal Customer", "name_kh": "អតិថិជនស្មោះ", "description": "Active for 6+ months", "icon": "heart", "color": "#f59e0b", "metric": "membershipDays", "threshold": 180},
    {"code": "BIG_SPENDER", "name": "Big Spender", "name_kh": "ចំណាយច្រើន", "description": "Spend over $500 total", "icon": "gem", "color": "#8b5cf6", "metric": "spend", "threshold": 500},
    {"code": "FIVE_ORDERS", "name": "5 Orders", "name_kh": "ការបញ្ជាទិញ ៥", "description": "Complete 5 orders", "icon": "star", "color": "#10b981", "metric": "orders", "threshold": 5},
    {"code": "TEN_ORDERS", "name": "10 Orders", "name_kh": "ការបញ្ជាទិញ ១០", "description": "Complete 10 orders", "icon": "trophy", "color": "#f59e0b", "metric": "orders", "threshold": 10},
    {"code": "REFERRAL_CHAMPION", "name": "Referral Champion", "name_kh": "ជើងឯកណែនាំ", "description": "Successfully refer 5 friends", "icon": "users", "color": "#ec4899", "metric": "referrals", "threshold": 5},
    {"code": "POINT_COLLECTOR", "name": "Point Collector", "name_kh": "អ្នករក Point", "description": "Earn 1,000+ loyalty points", "icon": "star", "color": "#f59e0b", "metric": "qualifyingPoints", "threshold": 1000},
]  # fmt: skip

VOUCHER_IMAGE = "https://images.unsplash.com/photo-1607082349566-187342175e2f?w=300&h=200&fit=crop&auto=format"


def unsplash(photo: str, size: str = "w=300&h=200") -> str:
    return f"https://images.unsplash.com/{photo}?{size}&fit=crop&auto=format"


REWARDS = [
    {"name": "$2 Discount Voucher", "name_kh": "គូប៉ុង $2", "description": "Get $2 off your next order. Valid for 30 days.", "points_cost": 200, "tier": "Silver", "category": "Discount", "image": VOUCHER_IMAGE},
    {"name": "$5 Discount Voucher", "name_kh": "គូប៉ុង $5", "description": "Get $5 off your next order. Valid for 30 days.", "points_cost": 500, "tier": "Gold", "category": "Discount", "image": VOUCHER_IMAGE},
    {"name": "Free Kampot Coffee", "name_kh": "កាហ្វេកំពតឥតគិតថ្លៃ", "description": "Redeem a free bag of Kampot Pepper Coffee. Delivered with your next order.", "points_cost": 400, "tier": "Silver", "category": "Free Product", "image": unsplash("photo-1514432324607-a09d9b4aefdd")},
    {"name": "Free Shipping Voucher", "name_kh": "ដឹកជញ្ជូនឥតគិតថ្លៃ", "description": "Free shipping on your next order, any amount. Valid 14 days.", "points_cost": 150, "tier": "Silver", "category": "Shipping", "image": unsplash("photo-1566576912321-d58ddd7a6088")},
    {"name": "Khmer Snack Box", "name_kh": "ប្រអប់អាហារ", "description": "A premium selection of the best Cambodian snacks. Surprise box!", "points_cost": 800, "tier": "Gold", "category": "Free Product", "image": unsplash("photo-1621939514649-280e2ee25f60")},
    {"name": "$15 Platinum Voucher", "name_kh": "គូប៉ុង Platinum $15", "description": "Exclusive $15 voucher for Platinum members only.", "points_cost": 1200, "tier": "Platinum", "category": "Discount", "image": VOUCHER_IMAGE},
    {"name": "Silk Krama Gift", "name_kh": "ក្រមាសូត្រជាអំណោយ", "description": "A beautiful traditional Silk Krama — the iconic Cambodian garment. Platinum exclusive.", "points_cost": 2000, "tier": "Platinum", "category": "Free Product", "image": unsplash("photo-1606760227091-3dd870d97f1d")},
    {"name": "Coffee Tasting Kit", "name_kh": "ឧបករណ៍សាកល្បងកាហ្វេ", "description": "5 varieties of Cambodian specialty coffee in one curated tasting kit.", "points_cost": 600, "tier": "Gold", "category": "Free Product", "image": unsplash("photo-1611854779393-1b2da9d400fe")},
]  # fmt: skip

CAMPAIGNS = [
    {"name": "Double Points Week", "name_kh": "សប្តាហ៍ពិន្ទុទ្វេដង", "description": "Every completed order earns twice the loyalty points.",
     "image": unsplash("photo-1607083206869-4c7672e72a8a", "w=1200&h=400"), "banner_title": "Double points on every order",
     "banner_subtitle": "Shop this week and earn 2× loyalty points on everything.", "button_label": "Shop now", "link_url": "/products",
     "multiplier": Decimal(2), "sort_order": 1},
    {"name": "Kampot Coffee Festival", "name_kh": "ពិធីបុណ្យកាហ្វេកំពត", "description": "Spotlight on Kampot coffee and pepper.",
     "image": unsplash("photo-1447933601403-0c6688de566e", "w=1200&h=400"), "banner_title": "Kampot Coffee Festival",
     "banner_subtitle": "Discover single-origin Cambodian coffee, roasted fresh.", "button_label": "Explore coffee", "link_url": "/products",
     "multiplier": Decimal(1), "sort_order": 2},
    {"name": "Rewards Month", "name_kh": "ខែរង្វាន់", "description": "Promote the rewards catalog.",
     "image": unsplash("photo-1513201099705-a9746e1e201f", "w=1200&h=400"), "banner_title": "Turn your points into rewards",
     "banner_subtitle": "Vouchers, free products and exclusive gifts are waiting.", "button_label": "See rewards", "link_url": "/rewards",
     "multiplier": Decimal(1), "sort_order": 3},
]  # fmt: skip

# The Laravel seeder made ten random customers; these are fixed so the demo is the same every time.
DEMO_CUSTOMERS = [
    ("Sokha Chan", "sokha.chan@example.com", "Phnom Penh"),
    ("Dara Kim", "dara.kim@example.com", "Siem Reap"),
    ("Sreymom Heng", "sreymom.heng@example.com", "Battambang"),
    ("Vuthy Lim", "vuthy.lim@example.com", "Kampot"),
    ("Chenda Ouk", "chenda.ouk@example.com", "Takeo"),
    ("Piseth Sok", "piseth.sok@example.com", "Kandal"),
    ("Bopha Meas", "bopha.meas@example.com", "Kampong Cham"),
    ("Rithy Nhem", "rithy.nhem@example.com", "Phnom Penh"),
    ("Kanha Prak", "kanha.prak@example.com", "Sihanoukville"),
    ("Visal Touch", "visal.touch@example.com", "Kep"),
]

LEDGER_HISTORIES = [
    [(40, "EARN", 320, "Order #1001 completed"), (28, "REFERRAL", 100, "Referred a friend"), (21, "EARN", 280, "Order #1014 completed"),
     (14, "SPEND", -200, "Redeemed $2 Discount Voucher"), (6, "EARN", 150, "Order #1032 completed")],
    [(35, "EARN", 900, "Order #1006 completed"), (18, "SPEND", -500, "Redeemed $5 Discount Voucher"),
     (16, "REFUND", 500, "Refund: $5 Discount Voucher cancelled"), (3, "EARN", 420, "Order #1041 completed")],
    [(30, "EARN", 150, "Order #1009 completed"), (12, "SPEND", -150, "Redeemed Free Shipping Voucher"), (2, "EARN", 75, "Order #1045 completed")],
]  # fmt: skip

CATEGORIES = [
    {"name": "Coffee & Tea", "name_kh": "កាហ្វេ និងតែ", "description": "Local coffee beans, teas, and brewing goods."},
    {"name": "Snacks", "name_kh": "អាហារសម្រន់", "description": "Dried fruit, chips, and treats."},
    {"name": "Food & Grocery", "name_kh": "អាហារ និងគ្រឿងទេស", "description": "Pantry staples and Khmer specialties."},
    {"name": "Clothing", "name_kh": "សម្លៀកបំពាក់", "description": "Krama, silk, and apparel."},
    {"name": "Beauty", "name_kh": "សម្ផស្ស", "description": "Skincare and natural cosmetics."},
    {"name": "Beverages", "name_kh": "ភេសជ្ជៈ", "description": "Ready-to-drink juices and drinks."},
    {"name": "Electronics", "name_kh": "គ្រឿងអេឡិចត្រូនិច", "description": "Gadgets and accessories for devices."},
    {"name": "Accessories", "name_kh": "គ្រឿងតុបតែង", "description": "Handicrafts, home decor, and gifts."},
]

PRODUCTS = [
    ("PRD-0001", "Coffee & Tea", "Kampot Pepper Coffee", "កាហ្វេម្រេចកំពត", 12, "2", True, "photo-1514432324607-a09d9b4aefdd"),
    ("PRD-0002", "Snacks", "Khmer Snack Variety Box", "ប្រអប់អាហារសម្ល", 18, "1.5", True, "photo-1621939514649-280e2ee25f60"),
    ("PRD-0003", "Clothing", "Silk Krama Scarf", "ក្រមាសូត្រ", 35, "1", True, "photo-1606760227091-3dd870d97f1d"),
    ("PRD-0004", "Food & Grocery", "Jasmine Rice (5kg)", "អង្ករ​ម្លិះ (5គីឡូ)", 8, "1", False, "photo-1586201375761-83865001e31c"),
    ("PRD-0005", "Beauty", "Coconut Body Lotion", "លីកូនខ្លាញ់ដូង", 14, "2", True, "photo-1556228578-0d85b1a4d571"),
    ("PRD-0006", "Beverages", "Angkor Craft Beer 6-Pack", "ស្រាបៀរអង្គរ (6ដប)", 16, "1", False, "photo-1608270586620-248524c67de9"),
    ("PRD-0007", "Electronics", "Wireless Earbuds", "កាស wireless", 45, "1.5", True, "photo-1590658268037-6bf12165a8df"),
    ("PRD-0008", "Snacks", "Dried Mango Strips", "ស្វាយស្ងួត", 6, "3", False, "photo-1601493700631-2851384deff5"),
    ("PRD-0009", "Beauty", "Lemongrass Essential Oil", "ប្រេងស្ករស", 22, "2", False, "photo-1608571423902-eed4a5ad8108"),
    ("PRD-0010", "Coffee & Tea", "Cambodian Coffee Drip Bag Box", "ថង់ទម្លាក់កាហ្វេ", 20, "2", False, "photo-1611854779393-1b2da9d400fe"),
    ("PRD-0011", "Accessories", "Phone Stand (Bamboo)", "ជំហររ​ទូរស័ព្ទ", 9, "1", False, "photo-1586816879360-004f4d142167"),
    ("PRD-0012", "Accessories", "Traditional Khmer Pottery", "ភាជន៍ប្រឡោះ", 28, "1.5", False, "photo-1565193566173-7a0ee3dbe261"),
]

# Storefront copy for the demo products, keyed by name. Seeding fills a description only when a product has none.
PRODUCT_DESCRIPTIONS = {
    "Kampot Pepper Coffee": "Premium Cambodian coffee blended with authentic Kampot pepper. Bold, aromatic, and uniquely local.",
    "Khmer Snack Variety Box": "A curated box of traditional Cambodian snacks: dried mango, banana chips, coconut candy, and more.",
    "Silk Krama Scarf": "Traditional Cambodian krama woven from pure silk. Wear it as a scarf, headband, or sarong.",
    "Jasmine Rice (5kg)": "Phka Malis jasmine rice, Cambodia's award-winning fragrant white rice. Grown in Takeo province.",
    "Coconut Body Lotion": "Natural body lotion made with Cambodian coconut oil and lemongrass. Deeply moisturising.",
    "Angkor Craft Beer 6-Pack": "Cambodia's iconic Angkor beer. Crisp and refreshing, perfect for a warm Phnom Penh evening.",
    "Wireless Earbuds": "Compact wireless earbuds with 20-hour battery life. Perfect for commuting through Phnom Penh.",
    "Dried Mango Strips": "Sun-dried Cambodian mango strips. Naturally sweet with no added sugar. Sourced from Kampong Speu.",
    "Lemongrass Essential Oil": "Pure lemongrass oil extracted on Cambodian farms. Great for aromatherapy and skin care.",
    "Cambodian Coffee Drip Bag Box": "Single-serve drip coffee bags from the Mondulkiri highlands. Rich, dark roast with chocolate notes.",
    "Phone Stand (Bamboo)": "Handcrafted bamboo phone stand made by local artisans in Siem Reap. Eco-friendly and stylish.",
    "Traditional Khmer Pottery": "Hand-thrown clay pottery in traditional Khmer designs. A unique home decor piece and cultural gift.",
}

LOCATIONS = [
    {"name": "Phnom Penh Central Warehouse", "name_kh": "ឃ្លាំងកណ្ដាលភ្នំពេញ", "type": "warehouse"},
    {"name": "Siem Reap Store", "name_kh": "ហាងសៀមរាប", "type": "store"},
    {"name": "Battambang Pickup Point", "name_kh": "ចំណុចទទួលបាត់ដំបង", "type": "pickup"},
]

SUPPLIERS = {
    "print": {"name": "Mekong Print & Voucher", "kind": "supplier", "contact_name": "Rotha Prak", "phone": "+855 23 880 120", "email": "orders@mekongprint.example"},
    "kampot": {"name": "Kampot Pepper Coffee Co.", "kind": "supplier", "contact_name": "Sreypov Nhem", "phone": "+855 33 932 410", "email": "sales@kampotcoffee.example"},
    "snack": {"name": "Phsar Thmei Snack Traders", "kind": "supplier", "contact_name": "Heng Lay", "phone": "+855 12 448 902", "email": "heng@snacktraders.example"},
    "krama": {"name": "Takeo Silk Weavers Cooperative", "kind": "partner", "contact_name": "Chenda Ouk", "phone": "+855 32 210 755", "email": "hello@takeosilk.example"},
}  # fmt: skip

VOUCHER_STOCK = {"supplier": "print", "unit_cost": Decimal("0.1"), "tracking": "batch", "low_stock_threshold": 50, "reorder_point": 120, "reorder_qty": 500}

# Stock rules, and opening balances as (location index, quantity, batch, expires in days).
REWARD_STOCK: dict[str, dict[str, Any]] = {
    "$2 Discount Voucher": {"stock": {**VOUCHER_STOCK, "sku": "VCH-002"}, "opening": [(0, 799, "VCH-2026Q3", 78), (1, 200, "VCH-2026Q3", 78)]},
    "$5 Discount Voucher": {"stock": {**VOUCHER_STOCK, "sku": "VCH-005"}, "opening": [(0, 400, "VCH-2026Q3", 78), (1, 100, "VCH-2026Q3", 78)]},
    "Free Kampot Coffee": {"stock": {"sku": "FP-KPC-250", "supplier": "kampot", "unit_cost": Decimal("4.5"), "tracking": "batch", "low_stock_threshold": 8, "reorder_point": 15, "reorder_qty": 40},
                           "opening": [(0, 30, "KPC-0726", 18), (0, 35, "KPC-0926", 190)]},
    "Free Shipping Voucher": {"stock": {**VOUCHER_STOCK, "sku": "VCH-SHIP"}, "opening": [(0, 799, "VCH-2026Q3", 78), (1, 200, "VCH-2026Q3", 78)]},
    "Khmer Snack Box": {"stock": {"sku": "FP-SNK-BOX", "supplier": "snack", "unit_cost": Decimal(6), "tracking": "batch", "low_stock_threshold": 5, "reorder_point": 10, "reorder_qty": 30, "out_of_stock": "hide"},
                        "opening": [(1, 4, "SNK-08", -5), (0, 26, "SNK-09", 55)]},
    "$15 Platinum Voucher": {"stock": {**VOUCHER_STOCK, "sku": "VCH-015", "low_stock_threshold": 10, "reorder_point": 25, "reorder_qty": 100},
                             "opening": [(0, 80, "VCH-2026Q3", 78), (1, 20, "VCH-2026Q3", 78)]},
    "Silk Krama Gift": {"stock": {"sku": "GF-KRAMA", "supplier": "krama", "unit_cost": Decimal(18), "tracking": "serial", "low_stock_threshold": 2, "reorder_point": 4, "reorder_qty": 10, "allow_backorder": True},
                        "variants": [("Standard", "Red", "GF-KRAMA-RED", "R"), ("Standard", "Indigo", "GF-KRAMA-IND", "I"), ("Standard", "Gold", "GF-KRAMA-GLD", "G")]},
    "Coffee Tasting Kit": {"stock": {"sku": "GF-TASTE", "supplier": "kampot", "unit_cost": Decimal("9.5"), "tracking": "none", "low_stock_threshold": 3, "reorder_point": 6, "reorder_qty": 12},
                           "variants": [("Small", "", "GF-TASTE-S", 12), ("Large", "", "GF-TASTE-L", 8)]},
}  # fmt: skip


def first(db: Session, model: type, include_deleted: bool = False, **where: Any) -> Any:
    statement = select(model).filter_by(**where)
    if include_deleted:
        statement = statement.execution_options(include_deleted=True)
    return db.scalars(statement).first()


def upsert(db: Session, model: type, match: dict[str, Any], values: dict[str, Any], include_deleted: bool = False) -> Any:
    """updateOrCreate: find by `match`, then set `values`."""
    row = first(db, model, include_deleted, **match)
    if row is None:
        row = model(**match)
        db.add(row)
    for name, value in values.items():
        setattr(row, name, value)
    db.flush()
    return row


def seed_roles(db: Session) -> None:
    for data in ROLES:
        if first(db, Role, include_deleted=True, code=data["code"]) is None:
            role = Role(code=data["code"], label=data["label"], stock_authority=data["stock_authority"])
            # Only new roles get the defaults, so re-seeding keeps permissions edited since.
            if data["code"] != Role.SUPER_ADMIN:
                role.sync_pages(data["pages"])
            db.add(role)
    db.flush()


def seed_staff(db: Session) -> User:
    user = first(db, User, email="test@example.com")
    if user is None:
        user = User(name="Test User", email="test@example.com", password=hash_password("password"), email_verified_at=clock.now())
        db.add(user)
    # The demo staff user is Super Admin.
    if user.role_id is None:
        user.role_id = db.scalar(select(Role.id).where(Role.code == Role.SUPER_ADMIN))
    db.flush()
    return user


def seed_loyalty(db: Session) -> None:
    for tier in TIERS:
        upsert(db, Tier, {"name": tier["name"]}, tier)
    for badge in BADGES:
        upsert(db, Badge, {"code": badge["code"]}, badge)
    tiers = dict(db.execute(select(Tier.name, Tier.id)).all())
    entry = db.scalar(select(Tier.id).order_by(Tier.min_points))
    for reward in REWARDS:
        values = {key: value for key, value in reward.items() if key != "tier"}
        # A reward whose tier no longer exists falls back to the entry tier.
        upsert(db, Reward, {"name": reward["name"]}, {**values, "min_tier_id": tiers.get(reward["tier"], entry)})

    today = clock.today()
    start = today.replace(day=1)
    month, year = today.month + 2, today.year
    if month > 12:
        month, year = month - 12, year + 1
    end = date(year, month, calendar.monthrange(year, month)[1])
    for campaign in CAMPAIGNS:
        upsert(db, Campaign, {"name": campaign["name"]}, {**campaign, "start_date": start, "end_date": end, "show_on_home": True})


def seed_customers(db: Session) -> None:
    tiers = list(db.scalars(select(Tier).order_by(Tier.min_points)))
    password = hash_password("password")
    upsert(
        db,
        Customer,
        {"email": "customer@example.com"},
        {"tier_id": tiers[0].id, "name": "Test Customer", "phone": "012345678", "password": password, "referral_code": "TESTCUST"},
    )
    for index, (name, email, province) in enumerate(DEMO_CUSTOMERS):
        if first(db, Customer, email=email) is None:
            db.add(
                Customer(
                    tier_id=tiers[index % len(tiers)].id,
                    name=name,
                    email=email,
                    phone=f"0{12 + index}{345678 + index:06d}",
                    password=password,
                    province=province,
                    referral_code=f"DEMO{index + 1:04d}",
                )
            )
    db.flush()

    if db.scalar(select(LoyaltyTransaction.id)):
        return
    demo = first(db, Customer, email="customer@example.com")
    others = db.scalars(
        select(Customer)
        .where(Customer.email.like("%@example.%"), Customer.email != "customer@example.com")
        .order_by(Customer.id)
        .limit(len(LEDGER_HISTORIES) - 1)
    )
    for customer, history in zip([demo, *others], LEDGER_HISTORIES):
        for days_ago, kind, points, reason in history:
            row = record_points(db, customer, kind, points, reason)
            row.created_at = row.updated_at = clock.now() - timedelta(days=days_ago)
    db.flush()


def seed_catalog(db: Session) -> None:
    for category in CATEGORIES:
        upsert(db, Category, {"name": category["name"]}, category, include_deleted=True)
    categories = dict(db.execute(select(Category.name, Category.id).execution_options(include_deleted=True)).all())
    for sku, category, name, name_kh, price, bonus, featured, photo in PRODUCTS:
        product = upsert(
            db,
            Product,
            {"sku": sku},
            {
                "category_id": categories[category],
                "name": name,
                "name_kh": name_kh,
                "price": Decimal(price),
                "bonus_multiplier": Decimal(bonus),
                "is_featured": featured,
                "normal_points": price,
            },
            include_deleted=True,
        )
        if not product.description:
            product.description = PRODUCT_DESCRIPTIONS.get(name)
        # Demo covers are external URLs; staff can replace them with uploads.
        if first(db, ProductImage, product_id=product.id, sort_order=0) is None:
            db.add(ProductImage(product_id=product.id, sort_order=0, path=unsplash(photo, "w=800&h=600")))
    db.flush()

    warehouse = product_stock.main_warehouse(db)
    for product_id in list(db.scalars(select(Product.id).order_by(Product.id))):
        if first(db, Inventory, product_id=product_id, warehouse_id=warehouse.id) is None:
            db.add(Inventory(product_id=product_id, warehouse_id=warehouse.id, qty_on_hand=20 + (product_id * 7) % 60, qty_reserved=0))
    db.flush()


def seed_reward_stock(db: Session, actor: User | None) -> None:
    locations = []
    for data in LOCATIONS:
        location = first(db, StockLocation, name=data["name"])
        if location is None:
            location = StockLocation(**data)
            db.add(location)
            db.flush()
        locations.append(location)
    suppliers = {}
    for key, data in SUPPLIERS.items():
        supplier = first(db, Supplier, name=data["name"])
        if supplier is None:
            supplier = Supplier(**data)
            db.add(supplier)
            db.flush()
        suppliers[key] = supplier

    seed_ledger = actor is not None and not db.scalar(select(RewardStockMovement.id))
    tiers = dict(db.execute(select(Tier.name, Tier.id)).all())
    for reward_data in REWARDS:
        plan = REWARD_STOCK[reward_data["name"]]
        reward = first(db, Reward, name=reward_data["name"])
        if reward is None:
            reward = Reward(
                name=reward_data["name"], points_cost=reward_data["points_cost"], min_tier_id=tiers[reward_data["tier"]], is_active=True
            )
            db.add(reward)
            db.flush()
        stock = plan["stock"]
        item = first(db, RewardStockItem, reward_id=reward.id)
        if item is None:
            item = RewardStockItem(
                reward_id=reward.id,
                **{key: value for key, value in stock.items() if key not in ("supplier", "out_of_stock", "allow_backorder")},
                supplier_id=suppliers[stock["supplier"]].id,
                out_of_stock=stock.get("out_of_stock", "disable"),
                allow_backorder=stock.get("allow_backorder", False),
            )
            db.add(item)
            db.flush()
        variants = []
        for size, color, sku, opening in plan.get("variants", []):
            variant = first(db, RewardVariant, sku=sku)
            if variant is None:
                variant = RewardVariant(reward_id=reward.id, size=size, color=color, sku=sku)
                db.add(variant)
                db.flush()
            variants.append((variant, opening))
        if not seed_ledger:
            continue

        def receive(**fields: Any) -> None:
            db.add(
                RewardStockMovement(
                    reward_id=reward.id,
                    actor_id=actor.id,
                    supplier_id=item.supplier_id,
                    type="RECEIVE",
                    unit_cost=item.unit_cost,
                    source="DONATION" if item.tracking == "serial" else "PURCHASE",
                    reason="Opening balance",
                    reserved_delta=0,
                    **fields,
                )
            )

        for location, qty, batch, expires_in in plan.get("opening", []):
            receive(location_id=locations[location].id, on_hand_delta=qty, batch=batch, expires_at=clock.today() + timedelta(days=expires_in))
        for variant, opening in variants:
            # Serial-tracked variants get three numbered units each; others get their opening quantity.
            if isinstance(opening, str):
                receive(location_id=locations[0].id, variant_id=variant.id, on_hand_delta=3, serials=[f"KR-{opening}-00{n}" for n in (1, 2, 3)])
            else:
                receive(location_id=locations[0].id, variant_id=variant.id, on_hand_delta=opening)
        db.flush()


def seed_referrals(db: Session) -> None:
    referrer = first(db, Customer, email="customer@example.com")
    if referrer is None or db.scalar(select(Referral.id)):
        return
    for customer_id in list(db.scalars(select(Customer.id).where(Customer.id != referrer.id).order_by(Customer.id).limit(3))):
        db.add(Referral(referrer_id=referrer.id, referred_id=customer_id))
    db.flush()


def seed_orders(db: Session) -> None:
    customers = list(db.scalars(select(Customer).order_by(Customer.id).limit(4)))
    products = list(db.scalars(select(Product).order_by(Product.id).limit(6)))
    if db.scalar(select(Order.id)) or not customers or not products:
        return
    statuses = [Order.PENDING, Order.CONFIRMED, Order.PROCESSING]
    for index, product in enumerate(products):
        customer = customers[index % len(customers)]
        order = orders.place(
            db, customer, [{"product_id": product.id, "quantity": index % 3 + 1}], f"{index} Street 240, Daun Penh", "012345678"
        )
        order.status = statuses[index % 3]
        if order.payment:
            order.payment.status = Payment.PENDING
    db.flush()


def seed(db: Session) -> None:
    seed_roles(db)
    staff = seed_staff(db)
    seed_loyalty(db)
    seed_customers(db)
    seed_catalog(db)
    seed_reward_stock(db, staff)
    seed_referrals(db)
    seed_orders(db)
    import_postal_codes(db)
    db.commit()

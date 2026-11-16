"""Catalog setup: categories and products."""

from typing import Annotated, Any

from fastapi import APIRouter, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app import clock, storage
from app.auth import Db, pages, random_string
from app.http import Input, attempt, done, find_or_404, method_of, ok, page_of, presence, refuse, uploads
from app.models import Category, Product, ProductImage, User
from app.services import product_stock
from app.services.records import log_staff_change
from app.shapes import counts
from app.validation import Exists, NotIn, Unique, Validator, to_bool, validate

router = APIRouter(prefix="/backend", tags=["catalog"])

CategoriesStaff = Annotated[User, pages("categories")]
ProductsStaff = Annotated[User, pages("products")]


def plural(word: str, count: int) -> str:
    return word if count == 1 else f"{word}s"


# ---------------------------------------------------------------- Categories


def category_api(db: Session, category: Category, products_count: int | None = None) -> dict[str, Any]:
    if products_count is None:
        products_count = counts(db, Product.category_id, [category.id]).get(category.id, 0)
    parent = db.get(Category, category.parent_id) if category.parent_id else None
    if parent is not None and parent.deleted_at is not None:
        parent = None
    return {
        **category.to_array(),
        "products_count": products_count,
        "parent": parent.to_array("id", "name") if parent else None,
    }


def category_rules(db: Session, method: str, category: Category | None) -> dict[str, list[Any]]:
    def not_own_descendant(attribute, value, fail):
        if category is None:
            return
        # Walk up from the proposed parent; reaching this category means the parent sits beneath it.
        visited: set[int] = set()
        ancestor = db.scalar(select(Category.parent_id).where(Category.id == int(value)))
        while ancestor and ancestor not in visited:
            if ancestor == category.id:
                fail("A category cannot be moved under one of its own sub-categories.")
                return
            visited.add(ancestor)
            ancestor = db.scalar(select(Category.parent_id).where(Category.id == ancestor))

    def unique_name(attribute, value, fail):
        statement = select(Category.id).where(func.lower(Category.name) == str(value).lower(), Category.deleted_at.is_(None))
        if category is not None:
            statement = statement.where(Category.id != category.id)
        if db.scalar(statement):
            fail(f"A category named {value} already exists.")

    exists = Exists(
        lambda value: bool(db.scalar(select(Category.id).where(Category.id == int(value), Category.deleted_at.is_(None))))
    )
    return {
        "parent_id": ["nullable", "integer", exists, NotIn([category.id] if category else []), not_own_descendant],
        "name": [presence(method), "string", "max:40", unique_name],
        "name_kh": ["nullable", "string", "max:255"],
        "description": ["nullable", "string", "max:1000"],
        "is_active": ["sometimes", "boolean"],
    }


CATEGORY_MESSAGES = {"parent_id.not_in": "A category cannot be its own parent."}


@router.get("/categories")
@attempt("Could not load categories.")
def list_categories(db: Db, staff: CategoriesStaff, data: Input):
    filters = validate(data, {"search": ["nullable", "string", "max:255"], "is_active": ["nullable", "boolean"]})
    statement = select(Category).order_by(Category.name, Category.id)
    if filters.get("search"):
        search = filters["search"]
        statement = statement.where(
            or_(func.lower(Category.name).like(f"%{search.lower()}%"), Category.name_kh.like(f"%{search}%"))
        )
    if filters.get("is_active") is not None:
        statement = statement.where(Category.is_active.is_(to_bool(data["is_active"])))

    def shape(category: Category) -> dict[str, Any]:
        return category_api(db, category)

    return page_of(db, statement, data, "Categories retrieved successfully", shape)


@router.post("/categories", status_code=201)
@attempt("Could not create the category.")
def create_category(db: Db, staff: CategoriesStaff, data: Input):
    fields = validate(data, category_rules(db, "POST", None), CATEGORY_MESSAGES)
    category = Category(**fields)
    db.add(category)
    db.flush()
    log_staff_change(db, staff, "CATEGORY", "CREATED", category.name)
    db.commit()
    return ok(category_api(db, category), "Category created successfully", 201)


@router.get("/categories/{category_id:int}")
@attempt("Could not load the category.")
def show_category(category_id: int, db: Db, staff: CategoriesStaff):
    return ok(category_api(db, find_or_404(db, Category, category_id)), "Category retrieved successfully")


@router.api_route("/categories/{category_id:int}", methods=["PUT", "PATCH", "POST"])
@attempt("Could not update the category.")
def update_category(category_id: int, request: Request, db: Db, staff: CategoriesStaff, data: Input):
    category = find_or_404(db, Category, category_id)
    fields = validate(data, category_rules(db, method_of(request, data), category), CATEGORY_MESSAGES)
    for name, value in fields.items():
        setattr(category, name, value)
    db.flush()
    log_staff_change(db, staff, "CATEGORY", "UPDATED", category.name)
    db.commit()
    return ok(category_api(db, category), "Category updated successfully")


@router.delete("/categories/{category_id:int}")
@attempt("Could not delete the category.")
def delete_category(category_id: int, db: Db, staff: CategoriesStaff):
    category = find_or_404(db, Category, category_id)
    children = db.scalar(
        select(func.count()).where(Category.parent_id == category.id, Category.deleted_at.is_(None))
    )
    if children:
        return refuse(f"Move the {children} {plural('sub-category', children)} in {category.name} to another parent first.")
    products = counts(db, Product.category_id, [category.id]).get(category.id, 0)
    if products:
        return refuse(f"Move the {products} {plural('product', products)} in {category.name} to another category first.")
    category.deleted_at = clock.now()
    log_staff_change(db, staff, "CATEGORY", "DELETED", category.name)
    db.commit()
    return done("Category deleted successfully")


# ---------------------------------------------------------------- Products

PRODUCT_COLUMNS = (
    "category_id", "name", "name_kh", "sku", "description", "price", "cost_price",
    "reorder_level", "normal_points", "bonus_multiplier", "is_featured",
)  # fmt: skip

PRODUCT_MESSAGES = {
    "images.max": f"Upload at most {Product.MAX_IMAGES} images at a time.",
    "images.*.image": "Each file must be an image.",
    "images.*.mimes": "Images must be JPG, PNG or WebP.",
    "images.*.max": "Each image must be 4 MB or smaller.",
    "remove_image_ids.*.exists": "That image does not belong to this product.",
}


def product_query():
    return select(Product).options(
        selectinload(Product.category), selectinload(Product.images), selectinload(Product.inventories)
    )


def load_product(db: Session, product_id: int) -> Product:
    product = db.scalars(product_query().where(Product.id == product_id)).first()
    if product is None:
        find_or_404(db, Product, product_id)
    return product


def product_validator(db: Session, method: str, product: Product | None, data: dict[str, Any]) -> Validator:
    if "sku" in data and data["sku"] is None:
        del data["sku"]

    def assignable(attribute, value, fail):
        category = db.get(Category, int(value))
        if category and not category.is_active and (product is None or product.category_id != category.id):
            fail(f"The {category.name} category is inactive and cannot be assigned to products.")

    def sku_taken(value) -> bool:
        statement = select(Product.id).where(Product.sku == value).execution_options(include_deleted=True)
        if product is not None:
            statement = statement.where(Product.id != product.id)
        return bool(db.scalar(statement))

    owner_id = product.id if product else 0
    rules = {
        "category_id": [
            presence(method), "integer",
            Exists(lambda value: bool(db.scalar(select(Category.id).where(Category.id == int(value), Category.deleted_at.is_(None))))),
            assignable,
        ],  # fmt: skip
        "name": [presence(method), "string", "max:255"],
        "name_kh": ["nullable", "string", "max:255"],
        "sku": ["sometimes", "string", "max:64", Unique(sku_taken)],
        "description": ["nullable", "string", "max:5000"],
        "images": ["sometimes", "array", f"max:{Product.MAX_IMAGES}"],
        "images.*": ["image", "mimes:jpg,jpeg,png,webp", "max:4096"],
        "remove_image_ids": ["sometimes", "array"],
        "remove_image_ids.*": [
            "integer",
            Exists(
                lambda value: bool(
                    db.scalar(select(ProductImage.id).where(ProductImage.id == int(value), ProductImage.product_id == owner_id))
                )
            ),
        ],
        "price": [presence(method), "numeric", "gt:0", "max:9999999999.99"],
        "cost_price": ["sometimes", "numeric", "min:0", "max:9999999999.99"],
        "reorder_level": ["sometimes", "integer", "min:0"],
        "normal_points": ["sometimes", "integer", "min:0"],
        "bonus_multiplier": ["sometimes", "numeric", "min:1", "max:999.99"],
        "is_featured": ["sometimes", "boolean"],
        "stock": ["sometimes", "integer", "min:0", "max:1000000"],
    }

    def image_limit(validator: Validator) -> None:
        removed = [int(value) for value in data.get("remove_image_ids") or [] if str(value).isdigit()]
        kept = 0
        if product is not None:
            kept = sum(1 for image in product.images if image.id not in removed)
        total = kept + len(uploads(data, "images"))
        if total > Product.MAX_IMAGES:
            validator.add_error("images", f"A product can have at most {Product.MAX_IMAGES} images; this would make {total}.")

    return Validator(data, rules, PRODUCT_MESSAGES).after(image_limit)


def add_images(db: Session, product: Product, files: list) -> None:
    if not files:
        return
    highest = db.scalar(select(func.max(ProductImage.sort_order)).where(ProductImage.product_id == product.id))
    position = 0 if highest is None else highest + 1
    for upload in files:
        db.add(ProductImage(product_id=product.id, path=storage.store(upload, f"products/{product.id}"), sort_order=position))
        position += 1
    db.flush()


def remove_images(db: Session, product: Product, image_ids: list[int]) -> None:
    if not image_ids:
        return
    for image in db.scalars(select(ProductImage).where(ProductImage.product_id == product.id, ProductImage.id.in_(image_ids))):
        storage.delete(image.path)
        db.delete(image)
    db.flush()


def product_response(db: Session, product_id: int) -> dict[str, Any]:
    db.expire_all()
    return load_product(db, product_id).to_api()


@router.get("/products")
@attempt("Could not load products.")
def list_products(db: Db, staff: ProductsStaff, data: Input):
    filters = validate(
        data,
        {
            "category_id": ["nullable", "integer"],
            "search": ["nullable", "string", "max:255"],
            "is_featured": ["nullable", "boolean"],
        },
    )
    statement = product_query().order_by(Product.id.desc())
    if filters.get("category_id"):
        statement = statement.where(Product.category_id == filters["category_id"])
    if filters.get("search"):
        search = filters["search"]
        term = f"%{search.lower()}%"
        statement = statement.where(
            or_(func.lower(Product.name).like(term), func.lower(Product.sku).like(term), Product.name_kh.like(f"%{search}%"))
        )
    if filters.get("is_featured") is not None:
        statement = statement.where(Product.is_featured.is_(to_bool(data["is_featured"])))
    return page_of(db, statement, data, "Products retrieved successfully", lambda product: product.to_api())


@router.post("/products", status_code=201)
@attempt("Could not create the product.")
def create_product(db: Db, staff: ProductsStaff, data: Input):
    fields = product_validator(db, "POST", None, data).validate()
    product = Product(
        sku=fields.get("sku") or f"PRD-{random_string(8).upper()}",
        **{name: value for name, value in fields.items() if name in PRODUCT_COLUMNS and name != "sku"},
    )
    db.add(product)
    db.flush()
    add_images(db, product, uploads(fields, "images"))
    product_stock.set_stock(db, product, fields.get("stock") or 0)
    log_staff_change(db, staff, "PRODUCT", "CREATED", product.name)
    db.commit()
    return ok(product_response(db, product.id), "Product created successfully", 201)


@router.get("/products/{product_id:int}")
@attempt("Could not load the product.")
def show_product(product_id: int, db: Db, staff: ProductsStaff):
    return ok(load_product(db, product_id).to_api(), "Product retrieved successfully")


@router.api_route("/products/{product_id:int}", methods=["PUT", "PATCH", "POST"])
@attempt("Could not update the product.")
def update_product(product_id: int, request: Request, db: Db, staff: ProductsStaff, data: Input):
    product = load_product(db, product_id)
    fields = product_validator(db, method_of(request, data), product, data).validate()
    for name, value in fields.items():
        if name in PRODUCT_COLUMNS:
            setattr(product, name, value)
    db.flush()
    remove_images(db, product, [int(value) for value in fields.get("remove_image_ids") or []])
    add_images(db, product, uploads(fields, "images"))
    if fields.get("stock") is not None:
        product_stock.set_stock(db, product, fields["stock"])
    log_staff_change(db, staff, "PRODUCT", "UPDATED", product.name)
    db.commit()
    return ok(product_response(db, product.id), "Product updated successfully")


@router.delete("/products/{product_id:int}")
@attempt("Could not delete the product.")
def delete_product(product_id: int, db: Db, staff: ProductsStaff):
    product = find_or_404(db, Product, product_id)
    product.deleted_at = clock.now()
    log_staff_change(db, staff, "PRODUCT", "DELETED", product.name)
    db.commit()
    return done("Product deleted successfully")

routers = [router]

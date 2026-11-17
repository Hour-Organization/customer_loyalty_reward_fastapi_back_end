from sqlalchemy import func, select

from app.models import (
    ActivityLog,
    Category,
    Inventory,
    Product,
    ProductImage,
    ProductStockMovement,
)
from tests.factories import image


def staff_headers(make, pages=None):
    return make.headers(make.staff(pages))


# ---------------------------------------------------------------- Categories


def test_categories_are_listed_with_product_counts_and_parent(client, make):
    parent = make.category(name="Food")
    child = make.category(name="Snacks", parent_id=parent.id)
    make.product(category=child)
    make.product(category=child)
    body = client.get("/api/backend/categories", headers=staff_headers(make)).json()
    assert body["meta"] == {"current_page": 1, "per_page": 30, "total": 2, "last_page": 1}
    snacks = next(row for row in body["data"] if row["name"] == "Snacks")
    assert snacks["products_count"] == 2
    assert snacks["parent"] == {"id": parent.id, "name": "Food"}
    assert snacks["created_at"].endswith("Z")


def test_page_size_defaults_to_30_and_is_capped_at_100(client, make):
    for _ in range(31):
        make.category()
    headers = staff_headers(make)
    body = client.get("/api/backend/categories?page=2", headers=headers).json()
    assert len(body["data"]) == 1 and body["meta"]["last_page"] == 2
    response = client.get("/api/backend/categories?per_page=101", headers=headers)
    assert response.json()["errors"]["per_page"] == ["The per page field must not be greater than 100."]


def test_category_names_are_required_short_and_unique_ignoring_case(client, make):
    make.category(name="Snacks")
    headers = staff_headers(make)
    assert client.post("/api/backend/categories", headers=headers, json={}).json()["errors"]["name"] == [
        "The name field is required."
    ]
    long = client.post("/api/backend/categories", headers=headers, json={"name": "x" * 41})
    assert long.json()["errors"]["name"] == ["The name field must not be greater than 40 characters."]
    taken = client.post("/api/backend/categories", headers=headers, json={"name": "SNACKS"})
    assert taken.json()["errors"]["name"] == ["A category named SNACKS already exists."]


def test_category_is_created_and_logged(client, make, db):
    response = client.post("/api/backend/categories", headers=staff_headers(make), json={"name": " Tea ", "name_kh": ""})
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["name"] == "Tea" and data["name_kh"] is None and data["is_active"] is True
    assert data["products_count"] == 0 and data["parent"] is None
    log = db.scalars(select(ActivityLog)).one()
    assert (log.event, log.description) == ("CATEGORY_CREATED", "Category created: Tea")


def test_category_can_keep_its_own_name_and_toggle_status(client, make):
    category = make.category(name="Snacks")
    headers = staff_headers(make)
    response = client.patch(f"/api/backend/categories/{category.id}", headers=headers, json={"name": "snacks", "is_active": False})
    assert response.status_code == 200
    assert response.json()["data"]["is_active"] is False


def test_category_cannot_be_its_own_parent_or_move_under_its_children(client, make):
    parent = make.category()
    child = make.category(parent_id=parent.id)
    grandchild = make.category(parent_id=child.id)
    headers = staff_headers(make)
    own = client.patch(f"/api/backend/categories/{parent.id}", headers=headers, json={"parent_id": parent.id})
    assert own.json()["errors"]["parent_id"] == ["A category cannot be its own parent."]
    under = client.patch(f"/api/backend/categories/{parent.id}", headers=headers, json={"parent_id": grandchild.id})
    assert under.json()["errors"]["parent_id"] == ["A category cannot be moved under one of its own sub-categories."]
    top = client.patch(f"/api/backend/categories/{child.id}", headers=headers, json={"parent_id": None})
    assert top.json()["data"]["parent_id"] is None


def test_unexpected_failures_return_a_json_error_and_save_nothing(client, make, db, monkeypatch):
    import app.routers.catalog as catalog

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(catalog, "log_staff_change", broken)
    response = client.post("/api/backend/categories", headers=staff_headers(make), json={"name": "Tea"})
    assert response.status_code == 500
    assert response.json() == {"success": False, "message": "Could not create the category."}
    assert db.scalar(select(func.count(Category.id))) == 0


def test_categories_with_children_or_products_cannot_be_deleted(client, make):
    parent = make.category(name="Food")
    make.category(parent_id=parent.id)
    stocked = make.category(name="Drinks")
    make.product(category=stocked)
    headers = staff_headers(make)
    response = client.delete(f"/api/backend/categories/{parent.id}", headers=headers)
    assert response.status_code == 422
    assert response.json()["message"] == "Move the 1 sub-category in Food to another parent first."
    response = client.delete(f"/api/backend/categories/{stocked.id}", headers=headers)
    assert response.json()["message"] == "Move the 1 product in Drinks to another category first."


def test_deleted_categories_are_hidden_and_their_name_can_be_reused(client, make, db):
    category = make.category(name="Old")
    headers = staff_headers(make)
    assert client.delete(f"/api/backend/categories/{category.id}", headers=headers).status_code == 200
    assert client.get(f"/api/backend/categories/{category.id}", headers=headers).status_code == 404
    assert client.get("/api/backend/categories", headers=headers).json()["meta"]["total"] == 0
    db.expire_all()
    assert db.scalars(select(Category).execution_options(include_deleted=True)).one().deleted_at is not None
    assert client.post("/api/backend/categories", headers=headers, json={"name": "Old"}).status_code == 201


def test_staff_need_the_categories_page_and_customers_cannot_manage_them(client, make):
    assert client.get("/api/backend/categories", headers=staff_headers(make, ["orders"])).status_code == 403
    assert client.get("/api/backend/categories", headers=make.headers(make.customer())).status_code == 401


# ---------------------------------------------------------------- Products


def test_products_are_listed_with_category_images_stock_and_filters(client, make):
    coffee = make.category(name="Coffee")
    make.product(category=coffee, name="Kampot Coffee", stock=5, is_featured=True)
    make.product(name="Rice")
    headers = staff_headers(make)
    rows = client.get(f"/api/backend/products?category_id={coffee.id}", headers=headers).json()["data"]
    assert [row["name"] for row in rows] == ["Kampot Coffee"]
    row = rows[0]
    assert row["stock"] == 5 and row["price"] == "10.00" and row["bonus_multiplier"] == "1.00"
    assert row["category"]["name"] == "Coffee" and row["images"] == []
    assert "inventories" not in row
    assert len(client.get("/api/backend/products?search=kampot", headers=headers).json()["data"]) == 1
    assert len(client.get("/api/backend/products?is_featured=0", headers=headers).json()["data"]) == 1


def test_product_is_created_with_a_generated_sku_defaults_and_stock(client, make, db):
    category = make.category()
    response = client.post(
        "/api/backend/products", headers=staff_headers(make), json={"category_id": category.id, "name": "Tea", "price": 4.5, "stock": 12}
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["sku"].startswith("PRD-") and len(data["sku"]) == 12
    assert data["price"] == "4.50" and data["normal_points"] == 0 and data["bonus_multiplier"] == "1.00"
    assert data["stock"] == 12
    movement = db.scalars(select(ProductStockMovement)).one()
    assert (movement.type, movement.quantity) == ("ADJUST", 12)


def test_product_values_are_validated(client, make):
    response = client.post("/api/backend/products", headers=staff_headers(make), json={"price": 0, "bonus_multiplier": 0.5})
    errors = response.json()["errors"]
    assert errors["category_id"] == ["The category id field is required."]
    assert errors["price"] == ["The price field must be greater than 0."]
    assert errors["bonus_multiplier"] == ["The bonus multiplier field must be at least 1."]


def test_product_is_created_with_uploaded_images_in_order(client, make):
    category = make.category()
    response = client.post(
        "/api/backend/products",
        headers=staff_headers(make),
        data={"category_id": str(category.id), "name": "Krama", "price": "35", "is_featured": "1"},
        files=[("images[]", image("a.png")), ("images[]", image("b.png"))],
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["is_featured"] is True
    assert [img["sort_order"] for img in data["images"]] == [0, 1]
    assert all(img["url"].startswith("http://testserver/storage/products/") for img in data["images"])


def test_update_removes_and_adds_images_with_a_spoofed_patch(client, make, db):
    product = make.product(name="Krama")
    db.add_all([ProductImage(product_id=product.id, path="https://example.com/a.jpg", sort_order=0),
                ProductImage(product_id=product.id, path="https://example.com/b.jpg", sort_order=1)])
    db.commit()
    first = db.scalars(select(ProductImage).order_by(ProductImage.id)).first()
    response = client.post(
        f"/api/backend/products/{product.id}",
        headers=staff_headers(make),
        data={"_method": "PATCH", "remove_image_ids[]": str(first.id), "name": "Silk Krama"},
        files=[("images[]", image())],
    )
    assert response.status_code == 200, response.json()
    data = response.json()["data"]
    assert data["name"] == "Silk Krama"
    assert [img["sort_order"] for img in data["images"]] == [1, 2]
    assert data["images"][0]["url"] == "https://example.com/b.jpg"


def test_a_post_without_a_spoofed_method_is_not_an_update(client, make):
    product = make.product()
    response = client.post(f"/api/backend/products/{product.id}", headers=staff_headers(make), data={"name": "X"})
    assert response.status_code == 405


def test_images_must_be_small_jpg_png_or_webp_and_at_most_eight(client, make, db):
    product = make.product()
    headers = staff_headers(make)
    gif = client.post(
        f"/api/backend/products/{product.id}", headers=headers, data={"_method": "PATCH"},
        files=[("images[]", ("a.gif", b"GIF89a" + b"\0" * 20, "image/gif"))],
    )
    assert gif.json()["errors"]["images.0"] == ["Images must be JPG, PNG or WebP."]
    db.add_all([ProductImage(product_id=product.id, path=f"https://example.com/{n}.jpg", sort_order=n) for n in range(8)])
    db.commit()
    full = client.post(f"/api/backend/products/{product.id}", headers=headers, data={"_method": "PATCH"}, files=[("images[]", image())])
    assert full.json()["errors"]["images"] == ["A product can have at most 8 images; this would make 9."]


def test_another_products_image_cannot_be_removed(client, make, db):
    product, other = make.product(), make.product()
    db.add(ProductImage(product_id=other.id, path="https://example.com/a.jpg", sort_order=0))
    db.commit()
    image_id = db.scalars(select(ProductImage.id)).one()
    response = client.patch(f"/api/backend/products/{product.id}", headers=staff_headers(make), json={"remove_image_ids": [image_id]})
    assert response.json()["errors"]["remove_image_ids.0"] == ["That image does not belong to this product."]


def test_skus_are_unique_even_against_deleted_products(client, make):
    deleted = make.product(sku="PRD-OLD")
    headers = staff_headers(make)
    client.delete(f"/api/backend/products/{deleted.id}", headers=headers)
    category = make.category()
    response = client.post(
        "/api/backend/products", headers=headers, json={"category_id": category.id, "name": "New", "price": 1, "sku": "PRD-OLD"}
    )
    assert response.json()["errors"]["sku"] == ["The sku has already been taken."]


def test_inactive_categories_cannot_take_new_products(client, make):
    inactive = make.category(name="Retired", is_active=False)
    headers = staff_headers(make)
    response = client.post("/api/backend/products", headers=headers, json={"category_id": inactive.id, "name": "A", "price": 1})
    assert response.json()["errors"]["category_id"] == ["The Retired category is inactive and cannot be assigned to products."]
    staying = make.product(category=inactive)
    response = client.patch(f"/api/backend/products/{staying.id}", headers=headers, json={"category_id": inactive.id, "is_featured": True})
    assert response.status_code == 200 and response.json()["data"]["is_featured"] is True


def test_deleted_products_are_hidden_but_keep_their_stock_history(client, make, db):
    product = make.product(stock=3)
    headers = staff_headers(make)
    assert client.delete(f"/api/backend/products/{product.id}", headers=headers).json()["message"] == "Product deleted successfully"
    assert client.get(f"/api/backend/products/{product.id}", headers=headers).status_code == 404
    assert db.scalar(select(func.count(Inventory.id))) == 1


def test_deleted_categories_cannot_be_assigned(client, make):
    category = make.category()
    headers = staff_headers(make)
    client.delete(f"/api/backend/categories/{category.id}", headers=headers)
    response = client.post("/api/backend/products", headers=headers, json={"category_id": category.id, "name": "A", "price": 1})
    assert response.json()["errors"]["category_id"] == ["The selected category id is invalid."]


def test_products_need_the_products_page(client, make):
    assert client.get("/api/backend/products", headers=staff_headers(make, ["categories"])).status_code == 403
    assert db_count_products(make) == 0


def db_count_products(make) -> int:
    return make.db.scalar(select(func.count(Product.id)))

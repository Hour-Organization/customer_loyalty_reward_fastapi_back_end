from sqlalchemy import func, select

from app.models import PersonalAccessToken, Role

# ---------------------------------------------------------------- Roles


def test_roles_are_listed_with_pages_and_user_counts(client, make):
    admin = make.staff()
    make.staff(["orders"])
    rows = client.get("/api/backend/roles", headers=make.headers(admin)).json()["data"]
    super_admin = next(row for row in rows if row["code"] == Role.SUPER_ADMIN)
    assert super_admin["pages"] == Role.PAGES and super_admin["users_count"] == 1
    other = next(row for row in rows if row["code"] != Role.SUPER_ADMIN)
    assert other["pages"] == ["orders", "profile"]


def test_role_is_created_with_dashboard_access_and_a_code(client, make):
    h = make.headers(make.staff())
    data = client.post("/api/backend/roles", headers=h, json={"label": "  Marketing   Manager "}).json()["data"]
    assert data["label"] == "Marketing Manager" and data["code"] == "marketing_manager"
    assert data["pages"] == ["dashboard", "profile"] and data["stock_authority"] == "clerk" and data["users_count"] == 0
    taken = client.post("/api/backend/roles", headers=h, json={"label": "marketing manager"})
    assert taken.json()["errors"]["label"] == ["A role with that name already exists."]


def test_role_pages_and_stock_authority_can_be_updated(client, make):
    role = make.role(["dashboard"])
    data = client.patch(
        f"/api/backend/roles/{role.id}", headers=make.headers(make.staff()), json={"pages": ["stock", "orders"], "stock_authority": "manager"}
    ).json()["data"]
    assert data["pages"] == ["orders", "stock", "profile"] and data["stock_authority"] == "manager"


def test_roles_cannot_get_reserved_pages_or_only_my_profile(client, make):
    h = make.headers(make.staff())
    reserved = client.post("/api/backend/roles", headers=h, json={"label": "Sneaky", "pages": ["users"]})
    assert reserved.json()["errors"]["pages.0"] == [
        "Exchange Rate, User Management, and Role Permissions are reserved for Super Admin."
    ]
    profile = client.post("/api/backend/roles", headers=h, json={"label": "Empty", "pages": ["profile"]})
    assert profile.json()["errors"]["pages"] == ["Select at least one section for this role."]


def test_super_admin_role_cannot_be_changed_or_deleted(client, make):
    admin = make.staff()
    h = make.headers(admin)
    message = "Super Admin always has full access and cannot be changed."
    assert client.patch(f"/api/backend/roles/{admin.role_id}", headers=h, json={"label": "X"}).json()["message"] == message
    assert client.delete(f"/api/backend/roles/{admin.role_id}", headers=h).json()["message"] == message


def test_role_with_users_cannot_be_deleted_and_a_recreated_role_gets_a_new_code(client, make):
    h = make.headers(make.staff())
    used = make.staff(["orders"]).role
    response = client.delete(f"/api/backend/roles/{used.id}", headers=h)
    assert response.json()["message"] == f"Move the 1 staff user with the {used.label} role to another role first."
    created = client.post("/api/backend/roles", headers=h, json={"label": "Packers"}).json()["data"]
    assert client.delete(f"/api/backend/roles/{created['id']}", headers=h).status_code == 200
    again = client.post("/api/backend/roles", headers=h, json={"label": "Packers"}).json()["data"]
    assert again["code"] == "packers_2"
    gone = client.post(
        "/api/backend/users", headers=h, json={"name": "A", "email": "a@example.com", "password": "secret123", "role_id": created["id"]}
    )
    assert gone.json()["errors"]["role_id"] == ["The selected role id is invalid."]


def test_only_super_admin_manages_roles_and_users(client, make):
    h = make.headers(make.staff(["dashboard"]))
    assert client.get("/api/backend/roles", headers=h).status_code == 403
    assert client.get("/api/backend/users", headers=h).status_code == 403


# ---------------------------------------------------------------- Users


def test_staff_users_are_listed_with_role_and_pages(client, make):
    make.staff(["orders"], name="Bopha")
    body = client.get("/api/backend/users?per_page=1", headers=make.headers(make.staff(name="Aaron"))).json()
    assert body["meta"]["total"] == 2 and body["meta"]["last_page"] == 2
    assert body["data"][0]["name"] == "Aaron" and body["data"][0]["role"]["code"] == Role.SUPER_ADMIN


def test_staff_user_can_be_created_and_log_in(client, make):
    role = make.role(["orders"])
    response = client.post(
        "/api/backend/users",
        headers=make.headers(make.staff()),
        json={"name": "Clerk", "email": "Clerk@Example.com", "password": "secret123", "role_id": role.id},
    )
    assert response.status_code == 201 and response.json()["data"]["email"] == "clerk@example.com"
    assert client.post("/api/backend/login", json={"email": "clerk@example.com", "password": "secret123"}).status_code == 200


def test_staff_user_fields_are_validated(client, make):
    existing = make.staff()
    errors = client.post(
        "/api/backend/users", headers=make.headers(existing), json={"email": existing.email, "password": "short"}
    ).json()["errors"]
    assert errors["email"] == ["A staff user already has that email address."]
    assert errors["password"] == ["The password field must be at least 8 characters."]
    assert errors["role_id"] == ["The role id field is required."]


def test_update_keeps_the_password_when_left_blank(client, make):
    user = make.staff(["orders"])
    response = client.patch(f"/api/backend/users/{user.id}", headers=make.headers(make.staff()), json={"name": "Renamed", "password": ""})
    assert response.status_code == 200
    assert client.post("/api/backend/login", json={"email": user.email, "password": "password"}).status_code == 200


def test_disabling_a_user_revokes_their_tokens(client, make, db):
    user = make.staff(["orders"])
    user_headers = make.headers(user)
    client.patch(f"/api/backend/users/{user.id}", headers=make.headers(make.staff()), json={"is_active": False})
    assert client.get("/api/backend/profile", headers=user_headers).status_code == 401
    assert db.scalar(select(func.count(PersonalAccessToken.id)).where(PersonalAccessToken.tokenable_id == user.id)) == 0


def test_admins_cannot_lock_themselves_out_and_one_super_admin_stays(client, make):
    admin = make.staff()
    h = make.headers(admin)
    other_role = make.role(["orders"])
    assert client.patch(f"/api/backend/users/{admin.id}", headers=h, json={"is_active": False}).json()["message"] == (
        "You cannot disable your own account."
    )
    assert client.patch(f"/api/backend/users/{admin.id}", headers=h, json={"role_id": other_role.id}).json()["message"] == (
        "You cannot remove your own Super Admin role."
    )
    second = make.staff()
    assert client.patch(f"/api/backend/users/{second.id}", headers=h, json={"is_active": False}).status_code == 200
    # The only other active Super Admin is the actor, so demoting them is the last one going.
    assert client.patch(f"/api/backend/users/{second.id}", headers=h, json={"is_active": True}).status_code == 200

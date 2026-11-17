from datetime import timedelta

from sqlalchemy import func, select

from app import clock
from app.models import Customer, PersonalAccessToken, Referral
from tests.factories import image


def staff_login(client, email, password="password"):
    return client.post("/api/backend/login", json={"email": email, "password": password, "device_name": "tests"})


# ---------------------------------------------------------------- Staff


def test_staff_can_log_in_with_valid_credentials(client, make):
    staff = make.staff()
    response = staff_login(client, staff.email)
    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Login successful"
    assert body["token_type"] == "Bearer"
    assert "|" in body["token"]
    assert body["user"] == {"id": staff.id, "name": staff.name, "email": staff.email, "is_active": True}


def test_login_fails_with_wrong_password(client, make):
    staff = make.staff()
    response = staff_login(client, staff.email, "wrong-password")
    assert response.status_code == 422
    assert response.json()["errors"] == {"email": ["The provided credentials are incorrect."]}


def test_login_requires_email_and_password(client):
    response = client.post("/api/backend/login", json={})
    assert response.status_code == 422
    assert response.json() == {
        "message": "The email field is required. (and 1 more error)",
        "errors": {"email": ["The email field is required."], "password": ["The password field is required."]},
    }


def test_unauthenticated_requests_get_a_json_401(client):
    response = client.get("/api/backend/profile")
    assert response.status_code == 401
    assert response.json() == {"message": "Unauthenticated."}


def test_token_stays_valid_while_it_keeps_being_used(client, make):
    headers = make.headers(make.staff())
    for _ in range(3):
        clock.travel(timedelta(days=6))
        assert client.get("/api/backend/profile", headers=headers).status_code == 200


def test_token_expires_after_the_idle_timeout(client, make):
    headers = make.headers(make.staff())
    clock.travel(timedelta(days=7, minutes=1))
    assert client.get("/api/backend/profile", headers=headers).status_code == 401


def test_staff_can_log_out(client, make, db):
    headers = make.headers(make.staff())
    assert client.post("/api/backend/logout", headers=headers).json() == {"message": "Logged out"}
    assert db.scalar(select(func.count(PersonalAccessToken.id))) == 0
    assert client.get("/api/backend/profile", headers=headers).status_code == 401


def test_disabled_staff_cannot_log_in(client, make):
    staff = make.staff(is_active=False)
    response = staff_login(client, staff.email)
    assert response.json()["errors"]["email"] == ["This staff account has been disabled."]


def test_profile_returns_the_role_and_its_pages(client, make):
    staff = make.staff(["customers", "orders"])
    data = client.get("/api/backend/profile", headers=make.headers(staff)).json()["data"]
    assert data["role"]["code"] == staff.role.code
    assert data["pages"] == ["customers", "orders", "profile"]


def test_staff_can_update_their_own_profile(client, make):
    staff = make.staff()
    other = make.staff()
    headers = make.headers(staff)
    taken = client.patch("/api/backend/profile", headers=headers, json={"name": "New", "email": other.email})
    assert taken.json()["errors"]["email"] == ["The email has already been taken."]
    response = client.patch("/api/backend/profile", headers=headers, json={"name": "New Name", "email": "new@example.com"})
    assert response.json()["data"]["name"] == "New Name"
    assert response.json()["data"]["email"] == "new@example.com"


# ---------------------------------------------------------------- Customers


def test_customer_can_register_into_the_entry_tier(client, make, db):
    make.tier(name="Gold", min_points=5000)
    bronze = make.tier(name="Bronze", min_points=0)
    response = client.post(
        "/api/register",
        json={"name": " Dara ", "email": "Dara@Example.com ", "password": "secret123", "phone": "", "device_name": "phone"},
    )
    assert response.status_code == 201
    customer = response.json()["customer"]
    assert customer["name"] == "Dara"
    assert customer["email"] == "dara@example.com"
    assert customer["phone"] is None
    assert customer["points_balance"] == 0
    assert customer["tier"] == {"id": bronze.id, "name": "Bronze"}
    assert len(customer["referral_code"]) == 8
    assert client.get("/api/profile", headers={"Authorization": f"Bearer {response.json()['token']}"}).status_code == 200


def test_customer_can_register_with_an_avatar_and_a_referral_code(client, make, db):
    referrer = make.customer(referral_code="FRIEND01")
    response = client.post(
        "/api/register",
        data={"name": "Sokha", "email": "sokha@example.com", "password": "secret123", "referral_code": "friend01"},
        files={"avatar": image()},
    )
    assert response.status_code == 201
    avatar = response.json()["customer"]["avatar"]
    assert avatar.startswith("http://testserver/storage/customers/avatars/")
    assert client.get(avatar.removeprefix("http://testserver")).status_code == 200
    referral = db.scalars(select(Referral)).one()
    assert referral.referrer_id == referrer.id


def test_registration_is_validated(client, make):
    make.customer(email="taken@example.com")
    response = client.post(
        "/api/register",
        data={"email": "taken@example.com", "password": "short", "referral_code": "NOPE"},
        files={"avatar": ("notes.txt", b"hello", "text/plain")},
    )
    errors = response.json()["errors"]
    assert errors["name"] == ["The name field is required."]
    assert errors["email"] == ["An account already exists with that email address."]
    assert errors["password"] == ["The password field must be at least 8 characters."]
    assert errors["referral_code"] == ["That referral code is not valid."]
    assert errors["avatar"][0] == "The avatar field must be an image."


def test_registration_is_unavailable_without_tiers(client):
    response = client.post("/api/register", json={"name": "A", "email": "a@example.com", "password": "secret123"})
    assert response.status_code == 503
    assert response.json()["message"] == "Registration is unavailable until loyalty tiers are set up."


def test_customer_login_and_deactivated_accounts(client, make):
    customer = make.customer()
    response = client.post("/api/login", json={"email": customer.email, "password": "password"})
    assert response.json()["customer"] == {"id": customer.id, "name": customer.name, "email": customer.email, "phone": "012345678"}
    inactive = make.customer(is_active=False)
    response = client.post("/api/login", json={"email": inactive.email, "password": "password"})
    assert response.json()["errors"]["email"] == ["This account has been deactivated."]


def test_staff_credentials_cannot_log_in_as_a_customer(client, make):
    staff = make.staff()
    response = client.post("/api/login", json={"email": staff.email, "password": "password"})
    assert response.status_code == 422


def test_tokens_only_open_their_own_side(client, make):
    customer_headers = make.headers(make.customer())
    staff_headers = make.headers(make.staff())
    assert client.get("/api/backend/profile", headers=customer_headers).status_code == 401
    assert client.get("/api/profile", headers=staff_headers).status_code == 401


def test_customer_can_log_out(client, make):
    headers = make.headers(make.customer())
    assert client.post("/api/logout", headers=headers).status_code == 200
    assert client.get("/api/profile", headers=headers).status_code == 401


def test_customer_profile_has_their_points(client, make):
    customer = make.customer(points=120)
    data = client.get("/api/profile", headers=make.headers(customer)).json()["data"]
    assert data["points_balance"] == 120
    assert set(data) == {"id", "name", "name_kh", "email", "phone", "province", "referral_code", "points_balance", "qualifying_points"}


def test_customers_are_unique_by_email(db, make):
    make.customer(email="one@example.com")
    assert db.scalar(select(func.count(Customer.id))) == 1

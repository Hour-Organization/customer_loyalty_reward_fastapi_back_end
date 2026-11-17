import os
import tempfile

# Settings are read once, so the test environment is set before the app is imported.
os.environ.update(
    APP_DEBUG="false",
    DATABASE_URL="sqlite://",
    BCRYPT_ROUNDS="4",
    EXPIRE_REDEMPTIONS_EVERY="0",
    PAYMENT_PROVIDER="fake",
    APP_URL="http://testserver",
    STORAGE_DIR=tempfile.mkdtemp(prefix="khmershop-storage-"),
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import clock  # noqa: E402
from app.database import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.services.payments import FakeKhqr  # noqa: E402
from tests.factories import Factory  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_database():
    Base.metadata.create_all(engine)
    yield
    clock.reset()
    FakeKhqr.settled.clear()
    Base.metadata.drop_all(engine)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def make(db) -> Factory:
    return Factory(db)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)

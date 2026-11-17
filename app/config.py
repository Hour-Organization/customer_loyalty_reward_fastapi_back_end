from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
UNITS = {"s": 1, "m": 60, "h": 3600}


class Settings(BaseSettings):
    """Settings read from the environment or `.env`."""

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    app_name: str = "KhmerShop API"
    app_debug: bool = False
    # Public root of this server, used for uploaded file URLs when there is no request to take it from.
    app_url: str = "http://localhost:8000"

    database_url: str = f"sqlite:///{BASE_DIR / 'database.sqlite'}"

    # Uploaded images are stored here and served at /storage.
    storage_dir: Path = BASE_DIR / "storage" / "public"

    # Tokens idle for longer than this many minutes stop working (0 turns the check off).
    token_idle_timeout: int = 60 * 24 * 7
    bcrypt_rounds: int = 12

    # Comma-separated origins allowed to call the API from a browser.
    cors_origins: str = "*"

    # Expire overdue redemptions in the background every this many seconds (0 turns it off).
    # A unit suffix is allowed: "90s", "5m", "1h".
    expire_redemptions_every: int = 300

    @field_validator("expire_redemptions_every", mode="before")
    @classmethod
    def _seconds(cls, value: object) -> object:
        if isinstance(value, str) and value.strip()[-1:].lower() in UNITS:
            text = value.strip()
            return int(text[:-1]) * UNITS[text[-1].lower()]
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

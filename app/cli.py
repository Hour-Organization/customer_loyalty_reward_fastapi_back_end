"""Command-line tasks: `python -m app.cli <command>`.

    migrate              apply the database migrations
    seed                 add the demo data (safe to run more than once)
    fresh                drop every table, migrate and seed
    setup                migrate, and seed the demo data if the database has none yet (docker compose runs this);
                         also loads the postal areas if there are none
    import-postal-codes  load Cambodia's provinces, districts and communes from app/data/cambodia_postal_codes.csv
                         (safe to run more than once: adds new areas and renames changed ones)
    expire-redemptions   refund reservations whose time ran out (the scheduler runs this every 5 minutes)
    expire-payments      cancel orders whose KHQR was left unpaid, and mark late KHQR payments for a refund
                         (the scheduler runs this every 5 minutes)
    check-bakong         check the Bakong settings: the token works and the shop's account exists
"""

import argparse
import sys

from alembic import command
from alembic.config import Config

from app.config import BASE_DIR


def alembic_config() -> Config:
    return Config(str(BASE_DIR / "alembic.ini"))


def migrate() -> None:
    command.upgrade(alembic_config(), "head")
    print("Migrated.")


def seed() -> None:
    from app.database import SessionLocal
    from app.seeders import seed as run

    with SessionLocal() as db:
        run(db)
    print("Seeded the demo data. Staff: test@example.com / password. Customer: customer@example.com / password.")


def fresh() -> None:
    from sqlalchemy import text

    from app.database import engine
    from app.models import Base

    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    migrate()
    seed()


def setup() -> None:
    from sqlalchemy import func, select

    from app.database import SessionLocal
    from app.models import Province, Tier

    migrate()
    with SessionLocal() as db:
        empty = not db.scalar(select(func.count(Tier.id)))
        no_postal_areas = not db.scalar(select(func.count(Province.id)))
    if empty:
        seed()
    else:
        print("Data found; skipped seeding.")
        if no_postal_areas:
            import_postal_codes()


def import_postal_codes() -> None:
    from app.database import SessionLocal
    from app.services.postal_codes import PostalCodeError
    from app.services.postal_codes import import_postal_codes as run

    with SessionLocal() as db:
        try:
            result = run(db)
        except PostalCodeError as error:
            sys.exit(f"Could not import the postal codes: {error}")
        db.commit()
    print(
        f"Postal areas: {result.provinces} provinces, {result.districts} districts, {result.communes} communes "
        f"({result.added} added, {result.updated} updated)."
    )


def expire_redemptions() -> None:
    from app.database import SessionLocal
    from app.services.redemptions import expire_overdue

    with SessionLocal() as db:
        expired = expire_overdue(db)
        db.commit()
    print(f"{expired} reservations expired.")


def expire_payments() -> None:
    from app.database import SessionLocal
    from app.services.payments import catch_late, expire_overdue

    with SessionLocal() as db:
        expired = expire_overdue(db)
        late = catch_late(db)
        db.commit()
    print(f"{expired} KHQR payments expired. {late} late KHQR payments found (refund due).")


def check_bakong() -> None:
    from app.config import get_settings
    from app.services.payments import BakongKhqr, ProviderUnavailable

    provider = BakongKhqr()
    problem = provider.ready()
    if problem:
        sys.exit(f"Not ready: {problem}")
    account = get_settings().bakong_account_id.strip()
    try:
        body = provider.call("/v1/check_bakong_account", {"accountId": account})
    except ProviderUnavailable as error:
        sys.exit(f"Could not use the Bakong Open API: {error}")
    if body.get("responseCode") != 0:
        sys.exit(f"Bakong does not know the account {account}: {body.get('responseMessage')}")
    print(f"Bakong is ready: the token works and {account} exists.")


COMMANDS = {
    "migrate": migrate,
    "seed": seed,
    "fresh": fresh,
    "setup": setup,
    "import-postal-codes": import_postal_codes,
    "expire-redemptions": expire_redemptions,
    "expire-payments": expire_payments,
    "check-bakong": check_bakong,
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=COMMANDS)
    COMMANDS[parser.parse_args(argv).command]()


if __name__ == "__main__":
    main(sys.argv[1:])

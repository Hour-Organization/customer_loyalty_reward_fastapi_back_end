"""Command-line tasks: `python -m app.cli <command>`.

    migrate              apply the database migrations
    seed                 add the demo data (safe to run more than once)
    fresh                drop every table, migrate and seed
    expire-redemptions   refund reservations whose time ran out (the scheduler runs this every 5 minutes)
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


def expire_redemptions() -> None:
    from app.database import SessionLocal
    from app.services.redemptions import expire_overdue

    with SessionLocal() as db:
        expired = expire_overdue(db)
        db.commit()
    print(f"{expired} reservations expired.")


COMMANDS = {"migrate": migrate, "seed": seed, "fresh": fresh, "expire-redemptions": expire_redemptions}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=COMMANDS)
    COMMANDS[parser.parse_args(argv).command]()


if __name__ == "__main__":
    main(sys.argv[1:])

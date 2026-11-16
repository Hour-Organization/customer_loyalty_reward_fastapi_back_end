"""The activity log and system settings."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ActivityLog, Customer, Setting, User


def log_activity(
    db: Session,
    event: str,
    description: str,
    customer: Customer | None = None,
    staff: User | None = None,
) -> ActivityLog:
    """Add an entry to the activity log."""
    entry = ActivityLog(
        customer_id=customer.id if customer else None,
        user_id=staff.id if staff else None,
        event=event,
        description=description,
    )
    db.add(entry)
    db.flush()
    return entry


def log_staff_change(db: Session, staff: User, subject: str, action: str, name: object | None = None) -> None:
    """Log a staff change the way the Laravel RecordStaffActivity middleware did: CATEGORY_UPDATED, "Category updated: Snacks"."""
    event = f"{subject}_{action}"
    label = event.replace("_", " ").lower().capitalize()
    log_activity(db, event, f"{label}: {name}" if name not in (None, "") else label, staff=staff)


def setting_int(db: Session, key: str) -> int:
    value = db.scalar(select(Setting.value).where(Setting.key == key))
    return int(value) if value is not None else int(Setting.DEFAULTS[key])


def put_setting(db: Session, key: str, value: int | str) -> None:
    setting = db.scalars(select(Setting).where(Setting.key == key)).first()
    if setting is None:
        db.add(Setting(key=key, value=str(value)))
    else:
        setting.value = str(value)
    db.flush()

"""Load Cambodia Post's postal areas into the provinces, districts and communes tables.

Each CSV row names a province, a district and a commune with their six-digit codes. A handful of rows in the
published file are shifted one level (a district listed as a commune, Stung Treng listed as a district of
Sihanoukville), so the level and parent of every area come from its code, not from its column:
PP0000 is a province, PPDD00 a district of PP0000, PPDDCC a commune of PPDD00.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.models import Commune, District, Province

POSTAL_CODES_CSV = BASE_DIR / "app" / "data" / "cambodia_postal_codes.csv"
COLUMNS = (
    ("Provincial_postal_code", "Provincial_postalarea_en", "Provincial_postalarea_kh"),
    ("District_postal_code", "District_postalarea_en", "District_postalarea_kh"),
    ("Commune_postal_code", "Commune_postalarea_en", "Commune_postalarea_kh"),
)
CODE = re.compile(r"\d{6}")


class PostalCodeError(ValueError):
    pass


@dataclass
class Imported:
    provinces: int
    districts: int
    communes: int
    added: int
    updated: int


def read_areas(path: Path) -> dict[str, tuple[str, str]]:
    """Every area in the file as {code: (name, name_kh)}."""
    areas: dict[str, tuple[str, str]] = {}
    with open(path, encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        missing = {name for columns in COLUMNS for name in columns} - set(reader.fieldnames or ())
        if missing:
            raise PostalCodeError(f"{path.name} is missing the columns {', '.join(sorted(missing))}.")
        for line, row in enumerate(reader, start=2):
            for code_column, name_column, name_kh_column in COLUMNS:
                code = (row[code_column] or "").strip()
                name, name_kh = (row[name_column] or "").strip(), (row[name_kh_column] or "").strip()
                if not CODE.fullmatch(code):
                    raise PostalCodeError(f"Line {line}: {code_column} {code!r} is not a six-digit code.")
                if not name or not name_kh:
                    raise PostalCodeError(f"Line {line}: area {code} has no English or Khmer name.")
                if areas.setdefault(code, (name, name_kh)) != (name, name_kh):
                    raise PostalCodeError(f"Line {line}: area {code} is named both {areas[code]} and {(name, name_kh)}.")
    return areas


def split_levels(areas: dict[str, tuple[str, str]]) -> tuple[dict, dict, dict]:
    provinces = {code: names for code, names in areas.items() if code.endswith("0000")}
    districts = {code: names for code, names in areas.items() if code.endswith("00") and code not in provinces}
    communes = {code: names for code, names in areas.items() if not code.endswith("00")}
    for code in districts:
        if province_of(code) not in provinces:
            raise PostalCodeError(f"District {code} has no province {province_of(code)}.")
    for code in communes:
        if district_of(code) not in districts:
            raise PostalCodeError(f"Commune {code} has no district {district_of(code)}.")
    return provinces, districts, communes


def province_of(code: str) -> str:
    return code[:2] + "0000"


def district_of(code: str) -> str:
    return code[:4] + "00"


def import_postal_codes(db: Session, path: Path = POSTAL_CODES_CSV) -> Imported:
    """Add the areas that are new and rename the ones whose names changed. Safe to run more than once.

    Areas no longer in the file are left alone, since addresses may point at them. The caller commits.
    """
    provinces, districts, communes = split_levels(read_areas(path))
    counts = {"added": 0, "updated": 0}

    def sync(model: type, wanted: dict[str, tuple[str, str]], parent_column: str | None = None, parent_ids=None, parent_of=None):
        """Upsert `wanted` by code and return {code: id} for every row of `model`."""
        existing = {row.code: row for row in db.scalars(select(model))}
        for code, (name, name_kh) in sorted(wanted.items()):
            values = {"name": name, "name_kh": name_kh}
            if parent_column:
                values[parent_column] = parent_ids[parent_of(code)]
            row = existing.get(code)
            if row is None:
                row = existing[code] = model(code=code)
                db.add(row)
                counts["added"] += 1
            elif any(getattr(row, column) != value for column, value in values.items()):
                counts["updated"] += 1
            for column, value in values.items():
                setattr(row, column, value)
        db.flush()
        return {code: row.id for code, row in existing.items()}

    province_ids = sync(Province, provinces)
    district_ids = sync(District, districts, "province_id", province_ids, province_of)
    sync(Commune, communes, "district_id", district_ids, district_of)
    return Imported(len(provinces), len(districts), len(communes), counts["added"], counts["updated"])

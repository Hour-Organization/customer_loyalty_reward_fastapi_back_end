from sqlalchemy import func, select

from app.models import Commune, District, Province
from app.services.postal_codes import PostalCodeError, import_postal_codes

HEADER = (
    "Provincial_postalarea_kh,Provincial_postalarea_en,Provincial_postal_code,"
    "District_postalarea_kh,District_postalarea_en,District_postal_code,"
    "Commune_postalarea_kh,Commune_postalarea_en,Commune_postal_code\n"
)


def count(db, model) -> int:
    return db.scalar(select(func.count(model.id)))


def test_imports_the_bundled_file(db):
    result = import_postal_codes(db)
    db.commit()

    assert (result.provinces, result.districts, result.communes) == (25, 210, 1652)
    assert (count(db, Province), count(db, District), count(db, Commune)) == (25, 210, 1652)
    commune = db.scalars(select(Commune).filter_by(code="010201")).one()
    assert (commune.name, commune.name_kh) == ("Banteay Neang", "បន្ទាយនាង")
    assert commune.district.code == "010200"
    assert commune.district.province.name == "Banteay Meanchey"


def test_places_shifted_rows_by_their_codes(db):
    import_postal_codes(db)

    # The file lists Stung Treng as a district of Sihanoukville and Kampong Leaeng as a commune.
    stung_treng = db.scalars(select(Province).filter_by(code="190000")).one()
    assert stung_treng.name == "Stung Treng"
    assert [d.code for d in stung_treng.districts] == ["190100", "190200", "190300", "190400", "190500", "190600"]
    kampong_leaeng = db.scalars(select(District).filter_by(code="040400")).one()
    assert kampong_leaeng.province.code == "040000"
    assert len(kampong_leaeng.communes) == 9
    assert db.scalars(select(Commune).filter_by(code="040401")).one().district_id == kampong_leaeng.id


def test_running_again_adds_nothing_and_applies_renames(db, tmp_path):
    import_postal_codes(db)
    again = import_postal_codes(db)
    assert (again.added, again.updated) == (0, 0)

    renamed = tmp_path / "renamed.csv"
    renamed.write_text(HEADER + "ខេត្ត កែប,Kep,230000,ក្រុង កែប,Kep City,230200,សង្កាត់ កែប,Sangkat Kep,230201\n", encoding="utf-8")
    result = import_postal_codes(db, renamed)
    assert (result.added, result.updated) == (0, 2)
    assert db.scalars(select(Commune).filter_by(code="230201")).one().name == "Sangkat Kep"
    assert count(db, Commune) == 1652


def test_rejects_a_bad_file(db, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(HEADER + "ខេត្ត កែប,Kep,230000,ក្រុង កែប,Kaeb,230200,សង្កាត់ កែប,Sangkat Kaeb,2302\n", encoding="utf-8")
    try:
        import_postal_codes(db, bad)
    except PostalCodeError as error:
        assert "six-digit" in str(error)
    else:
        raise AssertionError("expected a PostalCodeError")
    orphan = tmp_path / "orphan.csv"
    orphan.write_text(HEADER + "ខេត្ត កែប,Kep,230000,ក្រុង កែប,Kaeb,230200,សង្កាត់ កែប,Sangkat Kaeb,230301\n", encoding="utf-8")
    try:
        import_postal_codes(db, orphan)
    except PostalCodeError as error:
        assert "has no district 230300" in str(error)
    else:
        raise AssertionError("expected a PostalCodeError")

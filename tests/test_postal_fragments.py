"""Derived postal fragments: separate research signal; original Pincode untouched."""

from src.models import PersonRecord
from src.normalize import normalize_text
from src.postal import derive_postal_fragments
from src.query_planner import FAMILY_POSTAL, build_query_plan


def _record(**overrides):
    data = dict(record_id="P", name="MITHOO MURLI RAMCHANDANI")
    data.update(overrides)
    return PersonRecord(**data)


def test_us_zip_is_derived_from_address_line_not_house_number():
    record = _record(
        address1="12044 FALCON CREST WAY NORTHRIDGE",
        address2="CALIFORNIA 91326 U.S.A",
        country="United States",
    )
    fragments = derive_postal_fragments(record)
    assert [item.value for item in fragments] == ["91326"]
    item = fragments[0]
    assert item.source_field == "address2"
    assert item.source_text == "CALIFORNIA 91326 U.S.A"
    assert item.country_hint == "US"
    assert item.location == "CALIFORNIA"
    assert record.pincode == ""


def test_singapore_legacy_postal_district_is_derived():
    record = _record(
        name="INDER RAJ VIG",
        address1="55 CAIRNHILL ROAD 27 06 CAIRNHILL PLAZA",
        address2="SINGAPORE 0922",
        country="Singapore",
        pincode="0",
    )
    fragments = derive_postal_fragments(record)
    assert [item.value for item in fragments] == ["0922"]
    assert fragments[0].source_field == "address2"
    assert record.pincode == "0"


def test_unit_and_house_numbers_are_not_postal_codes():
    for address in ("FLAT 1203 TOWER B", "12044 FALCON CREST WAY", "PLOT 4521 SECTOR 9"):
        assert derive_postal_fragments(_record(address1=address)) == []


def test_original_pincode_field_is_kept_distinct():
    record = _record(address2="BANGALORE 560 038", country="India", pincode="560038")
    fragments = derive_postal_fragments(record)
    assert fragments[0].value == "560038"
    assert fragments[0].same_as_pincode is True
    assert record.pincode == "560038"
    assert record.address2 == "BANGALORE 560 038"
    assert "derived from address2" in fragments[0].describe()


def test_postal_query_strategies_are_planned_and_deduplicated():
    record = _record(
        address1="12044 FALCON CREST WAY NORTHRIDGE",
        address2="CALIFORNIA 91326 U.S.A",
        country="United States",
    )
    plans = build_query_plan(record)
    postal = {plan.strategy: plan.query for plan in plans if plan.family == FAMILY_POSTAL}
    assert postal["Q_POSTAL_NAME"] == "MITHOO MURLI RAMCHANDANI 91326"
    assert postal["Q_POSTAL_ADDRESS_FRAGMENT"] == "FALCON CREST WAY NORTHRIDGE 91326"
    assert postal["Q_POSTAL_NAME_LOCATION"] == "MITHOO MURLI RAMCHANDANI CALIFORNIA 91326"
    keys = [normalize_text(plan.query) for plan in plans]
    assert len(keys) == len(set(keys))


def test_postal_name_query_matching_pincode_query_is_not_repeated():
    record = _record(address2="BANGALORE 560038", country="India", pincode="560038")
    plans = build_query_plan(record)
    queries = [plan.query for plan in plans]
    assert queries.count("MITHOO MURLI RAMCHANDANI 560038") == 1
    assert "Q_POSTAL_NAME" not in {plan.strategy for plan in plans}

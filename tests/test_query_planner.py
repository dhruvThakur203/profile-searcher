from src.models import PersonRecord
from src.query_planner import build_query_plan


def test_query_plan_contains_core_strategies():
    p = PersonRecord(
        record_id="1",
        name="Dhruv Singh Thakur",
        address1="123 ABC Road",
        address2="Singapore 123456",
        country="Singapore",
        scrip="Example Ltd",
    )

    strategies = [x.strategy for x in build_query_plan(p)]

    assert "Q1_FULL_NAME_FULL_ADDRESS" in strategies
    assert "Q2_NAME_ADDRESS1" in strategies
    assert "Q3_NAME_ADDRESS2" in strategies
    assert "Q4_NAME_COUNTRY" in strategies
    assert "Q5_NAME_SCRIP" in strategies

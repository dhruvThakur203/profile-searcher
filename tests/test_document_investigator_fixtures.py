from pathlib import Path

from src.scraper.document_investigator import (
    EntityType,
    ShareholderTarget,
    SourceDocument,
    SourceMatchLevel,
    investigate,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "document_investigator"


def mithoo() -> ShareholderTarget:
    return ShareholderTarget(
        name="MITHOO MURLIRAMCHANDANI",
        address1="12044 FALCON CREST WAY",
        address2="NORTHRIDGE",
        record_id="mithoo",
    )


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def test_fixture_shareholder_table_is_strong():
    result = investigate(
        mithoo(),
        SourceDocument(
            source_url="fixture://shareholder_table.txt",
            text=_load("shareholder_table.txt"),
            title="table",
        ),
    )
    assert result.level == SourceMatchLevel.STRONG_MATCH
    types = set(result.accepted_groups[0].entity_types_found)
    assert EntityType.NAME in types
    assert EntityType.ADDRESS1 in types
    assert EntityType.ADDRESS2 in types


def test_fixture_multiline_record_is_strong():
    result = investigate(
        mithoo(),
        SourceDocument(
            source_url="fixture://multiline_record.txt",
            text=_load("multiline_record.txt"),
        ),
    )
    assert result.level == SourceMatchLevel.STRONG_MATCH


def test_fixture_unrelated_same_page_keeps_record_1_only():
    result = investigate(
        mithoo(),
        SourceDocument(
            source_url="fixture://unrelated_same_page.txt",
            text=_load("unrelated_same_page.txt"),
            page_texts=[_load("unrelated_same_page.txt")],
        ),
    )
    assert result.level == SourceMatchLevel.STRONG_MATCH
    accepted = result.accepted_groups[0]
    assert "ANOTHER PERSON" not in accepted.text
    assert "999 OTHER ROAD" not in accepted.text
    address2 = [
        item for item in accepted.occurrences if item.entity_type == EntityType.ADDRESS2
    ]
    assert len(address2) == 1
    assert "NORTHRIDGE" in address2[0].matched_text.upper()


def test_fixture_repeated_address_does_not_false_group():
    result = investigate(
        mithoo(),
        SourceDocument(
            source_url="fixture://repeated_address.txt",
            text=_load("repeated_address.txt"),
        ),
    )
    assert result.level == SourceMatchLevel.STRONG_MATCH
    accepted = result.accepted_groups[0]
    address2 = [
        item for item in accepted.occurrences if item.entity_type == EntityType.ADDRESS2
    ]
    assert len(address2) == 1
    assert "12044 FALCON CREST WAY" in accepted.text.upper()


def test_fixture_contacts_are_shareholder_only():
    result = investigate(
        mithoo(),
        SourceDocument(
            source_url="fixture://contact_record.txt",
            text=_load("contact_record.txt"),
        ),
    )
    assert result.level == SourceMatchLevel.STRONG_MATCH
    values = {item.value for item in result.contacts}
    assert "mithoo@example.com" in values
    assert any("555" in item.value for item in result.contacts)
    assert "company@example.com" not in values
    assert all("99999" not in item.value for item in result.contacts)


def test_fixture_pdf_like_extracted_text_finds_correct_record():
    result = investigate(
        mithoo(),
        SourceDocument(
            source_url="fixture://pdf_like_extracted_text.txt",
            text=_load("pdf_like_extracted_text.txt"),
            document_type="pdf",
        ),
    )
    assert result.level == SourceMatchLevel.STRONG_MATCH
    accepted = result.accepted_groups[0]
    assert "MITHOO M RAMCHANDANI" in accepted.text
    assert "12044 FALCON CREST WAY" in accepted.text
    assert "SOME PRIOR HOLDER" not in accepted.text
    assert "LATER HOLDER" not in accepted.text

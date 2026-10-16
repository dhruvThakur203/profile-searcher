from src.scraper.document_investigator import (
    NameEquivalence,
    ShareholderTarget,
    SourceDocument,
    SourceMatchLevel,
    investigate,
    investigate_many,
)
from src.scraper.document_investigator.evidence import name_equivalence
from src.scraper.document_investigator.text_search import entity_queries, search_entities


def mithoo_target() -> ShareholderTarget:
    return ShareholderTarget(
        name="MITHOO MURLIRAMCHANDANI",
        address1="12044 FALCON CREST WAY",
        address2="NORTHRIDGE",
        record_id="mithoo",
    )


def test_entity_queries_are_independent_and_not_combined():
    target = mithoo_target()
    queries = entity_queries(target)
    assert [entity.value for entity, _ in queries] == ["NAME", "ADDRESS1", "ADDRESS2"]
    assert [value for _, value in queries] == [
        "MITHOO MURLIRAMCHANDANI",
        "12044 FALCON CREST WAY",
        "NORTHRIDGE",
    ]
    assert not any("FALCON" in value and "MITHOO" in value for _, value in queries)


def test_a_same_row_table_is_strong():
    text = (
        "Name | Address1 | Address2\n"
        "MITHOO M RAMCHANDANI | 12044 FALCON CREST WAY | NORTHRIDGE\n"
        "OTHER PERSON | 1 MAIN STREET | ELSEWHERE\n"
    )
    result = investigate(
        mithoo_target(),
        SourceDocument(source_url="https://example.com/a.pdf", text=text, title="Table"),
    )
    assert result.level == SourceMatchLevel.STRONG_MATCH
    assert any(group.entity_types_found for group in result.accepted_groups)


def test_b_multiline_record_is_strong():
    text = """
Shareholder Register

Name:
MITHOO M RAMCHANDANI

Address:
12044 FALCON CREST WAY

City:
NORTHRIDGE

Next Holder:
SOMEONE ELSE
"""
    result = investigate(
        mithoo_target(),
        SourceDocument(source_url="https://example.com/b.pdf", text=text),
    )
    assert result.level == SourceMatchLevel.STRONG_MATCH


def test_c_column_style_layout_is_strong():
    text = (
        "MITHOO M RAMCHANDANI        12044 FALCON CREST WAY\n"
        "                             NORTHRIDGE\n"
    )
    result = investigate(
        mithoo_target(),
        SourceDocument(source_url="https://example.com/c.pdf", text=text),
    )
    assert result.level == SourceMatchLevel.STRONG_MATCH


def test_d_unrelated_same_page_person_is_not_automatic_match():
    text = """
Page 22

MITHOO M RAMCHANDANI
Folio 999
No address here

ANOTHER PERSON
12044 FALCON CREST WAY
NORTHRIDGE
California
"""
    # Separate blocks and large gap: address belongs to another person.
    result = investigate(
        mithoo_target(),
        SourceDocument(
            source_url="https://example.com/d.pdf",
            text=text,
            page_texts=[text],
        ),
    )
    # Name-only / address-only groups must not produce STRONG_MATCH.
    assert result.level != SourceMatchLevel.STRONG_MATCH


def test_e_repeated_city_only_near_record_counts():
    filler = "\n".join(f"Line {index} mentions NORTHRIDGE in passing" for index in range(50))
    text = (
        filler
        + "\n\n"
        + "MITHOO M RAMCHANDANI\n12044 FALCON CREST WAY\nNORTHRIDGE\n"
        + "\n\n"
        + filler
    )
    result = investigate(
        mithoo_target(),
        SourceDocument(source_url="https://example.com/e.pdf", text=text),
    )
    assert result.level == SourceMatchLevel.STRONG_MATCH
    northridge_hits = [
        item
        for item in result.occurrences
        if item.entity_type.value == "ADDRESS2"
    ]
    assert len(northridge_hits) >= 50
    # Accepted group should contain only one ADDRESS2 occurrence.
    accepted = result.accepted_groups[0]
    address2 = [item for item in accepted.occurrences if item.entity_type.value == "ADDRESS2"]
    assert len(address2) == 1


def test_f_name_formatting_variation_is_potential_or_compatible():
    text = "Holder MITHOO M RAMCHANDANI lives elsewhere with no address fields."
    occurrences = search_entities(mithoo_target(), text)
    name_hits = [item for item in occurrences if item.entity_type.value == "NAME"]
    assert name_hits
    assert name_hits[0].occurrence_kind.value in {"COMPATIBLE", "POTENTIAL"}
    assert name_equivalence(
        "MITHOO MURLIRAMCHANDANI", "MITHOO M RAMCHANDANI"
    ) == NameEquivalence.COMPATIBLE


def test_g_partial_name_is_not_exact_identity():
    assert (
        name_equivalence("DHRUV SINGH THAKUR", "DHRUV THAKUR")
        == NameEquivalence.PARTIAL
    )
    text = "DHRUV THAKUR\n123 ABC ROAD\nSINGAPORE\n"
    result = investigate(
        ShareholderTarget(
            name="DHRUV SINGH THAKUR",
            address1="123 ABC ROAD",
            address2="SINGAPORE",
        ),
        SourceDocument(source_url="https://example.com/g.pdf", text=text),
    )
    assert result.level != SourceMatchLevel.STRONG_MATCH
    assert all(
        item.name_equivalence != NameEquivalence.EXACT for item in result.evaluations
    )


def test_h_contacts_associated_with_accepted_record():
    text = """
MITHOO M RAMCHANDANI
12044 FALCON CREST WAY
NORTHRIDGE
Phone: +1 555 1234567
Email: mithoo@example.com
"""
    result = investigate(
        mithoo_target(),
        SourceDocument(source_url="https://example.com/h.pdf", text=text),
    )
    assert result.level == SourceMatchLevel.STRONG_MATCH
    values = {(item.type, item.value) for item in result.contacts}
    assert ("email", "mithoo@example.com") in values
    assert any(item.type == "phone" and "555" in item.value for item in result.contacts)
    assert all(item.group_id for item in result.contacts)


def test_i_unrelated_company_phone_is_not_associated():
    text = """
Company Contact:
Phone: +91 9876543210

Shareholder record:

MITHOO M RAMCHANDANI
12044 FALCON CREST WAY
NORTHRIDGE
"""
    result = investigate(
        mithoo_target(),
        SourceDocument(source_url="https://example.com/i.pdf", text=text),
    )
    assert result.level == SourceMatchLevel.STRONG_MATCH
    assert all("9876543210" not in item.value for item in result.contacts)


def test_investigate_many_keeps_all_accepted_sources():
    strong = (
        "MITHOO M RAMCHANDANI | 12044 FALCON CREST WAY | NORTHRIDGE\n"
    )
    weak = "Random document about NORTHRIDGE weather only.\n"
    results = investigate_many(
        mithoo_target(),
        [
            SourceDocument(source_url="https://example.com/milkfood.pdf", text=strong),
            SourceDocument(source_url="https://example.com/cie.pdf", text=strong),
            SourceDocument(source_url="https://example.com/other.pdf", text=weak),
        ],
    )
    assert [item.source_url for item in results if item.level == SourceMatchLevel.STRONG_MATCH] == [
        "https://example.com/milkfood.pdf",
        "https://example.com/cie.pdf",
    ]


def test_page_numbers_are_preserved_when_provided():
    pages = [
        "Cover page",
        "MITHOO M RAMCHANDANI\n12044 FALCON CREST WAY\nNORTHRIDGE\n",
        "Index",
    ]
    result = investigate(
        mithoo_target(),
        SourceDocument(
            source_url="https://example.com/paged.pdf",
            text="",
            page_texts=pages,
            document_type="pdf",
        ),
    )
    assert result.level == SourceMatchLevel.STRONG_MATCH
    assert result.accepted_groups[0].page_number == 2

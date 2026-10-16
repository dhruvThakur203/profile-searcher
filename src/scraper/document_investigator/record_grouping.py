from __future__ import annotations

import re
from dataclasses import dataclass

from ...normalize import normalize_name
from .models import EntityOccurrence, EntityType, EvidenceGroup, OccurrenceKind
from .text_search import is_compatible_name


@dataclass(frozen=True)
class GroupingConfig:
    """Deterministic same-record grouping. Same page alone is never enough."""

    record_radius: int = 350
    max_group_span: int = 900
    exact_name_max_group_span: int = 1400
    block_gap_limit: int = 120
    exact_name_block_gap_limit: int = 320
    prefer_blank_line_blocks: bool = True


_FIELD_LABEL = re.compile(
    r"(?i)^\s*(name|address|addr|city|state|pincode|pin|zip|folio|shareholder)\b"
)
_NAME_LINE = re.compile(r"\b([A-Z][A-Za-z.'-]{1,})(?:\s+[A-Z][A-Za-z.']{0,}){1,4}\b")
_UI_CHROME = {
    "VIEW REPORT",
    "AT A GLANCE",
    "PRIMARY PHONE",
    "PRIMARY EMAIL",
    "CURRENT CITY",
    "CONTACT INFORMATION",
    "PHONE NUMBERS",
    "EMAIL ADDRESSES",
    "RESIDENCE DATA",
    "RELATIONSHIP DATA",
    "SEARCH SMARTER KNOW MORE",
    "AS SEEN IN",
    "ADDITIONAL INFORMATION",
    "PEOPLE SEARCH",
    "REVERSE PHONE LOOKUP",
    "PUBLIC RECORDS",
    "BACKGROUND CHECK",
    "SIGN UP",
    "LOG IN",
    "LOGIN",
}
_CITY_STATE_LINE = re.compile(
    r"^[A-Za-z .'-]+,\s*[A-Z]{2}(?:\s+\d{4,6})?$"
)


def group_occurrences(
    occurrences: list[EntityOccurrence],
    text: str,
    config: GroupingConfig | None = None,
) -> list[EvidenceGroup]:
    settings = config or GroupingConfig()
    if not occurrences:
        return []

    blocks = _blank_line_blocks(text) if settings.prefer_blank_line_blocks else []
    seeds = _seed_occurrences(occurrences)
    groups: list[EvidenceGroup] = []
    used_signatures: set[tuple] = set()

    for seed in seeds:
        allowed = _allowed_span(seed, text, blocks, settings)
        members = _collect_members(seed, occurrences, text, blocks, settings, allowed)
        if not members:
            continue
        start = min(allowed[0], min(item.character_start for item in members))
        end = max(allowed[1], max(item.character_end for item in members))
        max_span = (
            settings.exact_name_max_group_span
            if seed.occurrence_kind == OccurrenceKind.EXACT
            else settings.max_group_span
        )
        if end - start > max_span:
            end = min(len(text), start + max_span)
        signature = tuple(
            sorted(
                (item.entity_type.value, item.character_start, item.character_end)
                for item in members
            )
        )
        if signature in used_signatures:
            continue
        used_signatures.add(signature)
        entity_types = sorted(
            {item.entity_type for item in members},
            key=lambda value: value.value,
        )
        pages = {item.page_number for item in members if item.page_number is not None}
        page_number = next(iter(pages)) if len(pages) == 1 else seed.page_number
        groups.append(
            EvidenceGroup(
                group_id=f"group-{len(groups) + 1}",
                text=text[start:end],
                occurrences=sorted(members, key=lambda item: item.character_start),
                entity_types_found=entity_types,
                start_position=start,
                end_position=end,
                page_number=page_number,
            )
        )
    return groups


def _seed_occurrences(occurrences: list[EntityOccurrence]) -> list[EntityOccurrence]:
    names = [
        item
        for item in occurrences
        if item.entity_type == EntityType.NAME
        and item.occurrence_kind
        in {OccurrenceKind.EXACT, OccurrenceKind.COMPATIBLE, OccurrenceKind.POTENTIAL}
    ]
    rank = {
        OccurrenceKind.EXACT: 0,
        OccurrenceKind.COMPATIBLE: 1,
        OccurrenceKind.POTENTIAL: 2,
    }
    names.sort(key=lambda item: (rank.get(item.occurrence_kind, 9), item.character_start))
    if names:
        return names
    return sorted(
        [item for item in occurrences if item.entity_type == EntityType.ADDRESS1],
        key=lambda item: item.character_start,
    )


def _collect_members(
    seed: EntityOccurrence,
    occurrences: list[EntityOccurrence],
    text: str,
    blocks: list[tuple[int, int]],
    settings: GroupingConfig,
    allowed: tuple[int, int] | None = None,
) -> list[EntityOccurrence]:
    span = allowed or _allowed_span(seed, text, blocks, settings)
    members = []
    for item in occurrences:
        if seed.page_number is not None and item.page_number is not None:
            if seed.page_number != item.page_number:
                continue
        if span[0] <= item.character_start <= span[1]:
            members.append(item)
    if seed not in members:
        members.append(seed)
    return _one_per_entity_type(members, seed)


def _allowed_span(
    seed: EntityOccurrence,
    text: str,
    blocks: list[tuple[int, int]],
    settings: GroupingConfig,
) -> tuple[int, int]:
    exact_seed = seed.occurrence_kind == OccurrenceKind.EXACT
    max_span = (
        settings.exact_name_max_group_span if exact_seed else settings.max_group_span
    )
    gap_limit = (
        settings.exact_name_block_gap_limit if exact_seed else settings.block_gap_limit
    )
    block = _block_containing(seed.character_start, blocks)
    if block is None:
        radius = settings.record_radius if not exact_seed else max(settings.record_radius, 700)
        return (
            max(0, seed.character_start - radius),
            min(len(text), seed.character_end + radius),
        )

    start, end = block
    seed_name = " ".join((seed.matched_text or "").upper().split())
    for block_start, block_end in blocks:
        if block_start < end:
            continue
        gap = block_start - end
        if gap > gap_limit:
            break
        chunk = text[block_start:block_end]
        if _looks_like_new_person(chunk, seed_name):
            break
        end = block_end
        if end - start > max_span:
            end = start + max_span
            break
    return start, end


def _looks_like_new_person(chunk: str, seed_name: str) -> bool:
    lines = [line.strip() for line in chunk.splitlines() if line.strip()]
    if not lines:
        return False
    seed_tokens = normalize_name(seed_name).split()
    for line in lines:
        if _FIELD_LABEL.match(line):
            continue
        if line.upper() in {"ADDRESS", "CITY", "STATE", "NAME"}:
            continue
        # Address/city lines usually start with digits or are single tokens.
        if re.match(r"^\d", line):
            continue
        if _CITY_STATE_LINE.match(line):
            continue
        # Table rows / headers are not person-record boundaries.
        if "\t" in line:
            continue
        match = _NAME_LINE.match(line)
        if not match:
            continue
        candidate = " ".join(match.group(0).upper().split())
        if not candidate or candidate == seed_name:
            continue
        if candidate in _UI_CHROME:
            continue
        if candidate in seed_name or seed_name in candidate:
            continue
        candidate_tokens = normalize_name(candidate).split()
        if not candidate_tokens:
            continue
        headerish = {
            "report",
            "view",
            "search",
            "phone",
            "phones",
            "email",
            "emails",
            "data",
            "city",
            "state",
            "primary",
            "current",
            "glance",
            "number",
            "numbers",
            "line",
            "type",
            "carrier",
            "location",
            "time",
            "zone",
            "address",
            "addresses",
            "zip",
            "county",
            "provider",
            "disposable",
            "all",
            "more",
            "full",
            "associated",
            "with",
        }
        # UI labels and section headers are rarely person names.
        if all(len(token) <= 2 or token in headerish for token in candidate_tokens):
            continue
        if any(token in headerish for token in candidate_tokens) and len(candidate_tokens) >= 3:
            continue
        if is_compatible_name(seed_tokens, candidate_tokens):
            continue
        if _same_core_person(seed_tokens, candidate_tokens):
            continue
        return True
    return False


def _same_core_person(seed_tokens: list[str], candidate_tokens: list[str]) -> bool:
    """True when names share the same non-initial tokens (middle initials ignored).

    Used only for record-span extension so profile aliases like
    ``CIDAMBI S RAVI`` / ``RAVI C CIDAMBI`` stay in one evidence group.
    This does not relax the final exact-name acceptance policy.
    """
    seed_core = [token for token in seed_tokens if len(token) > 1]
    candidate_core = [token for token in candidate_tokens if len(token) > 1]
    if len(seed_core) < 2 or len(candidate_core) < 2:
        return False
    return sorted(seed_core) == sorted(candidate_core)


def _one_per_entity_type(
    members: list[EntityOccurrence],
    seed: EntityOccurrence,
) -> list[EntityOccurrence]:
    """Keep the best occurrence of each entity type near the seed."""
    rank = {
        OccurrenceKind.EXACT: 3,
        OccurrenceKind.COMPATIBLE: 2,
        OccurrenceKind.POTENTIAL: 1,
        OccurrenceKind.ADDRESS: 0,
    }
    best: dict[EntityType, EntityOccurrence] = {}
    for item in members:
        current = best.get(item.entity_type)
        if current is None:
            best[item.entity_type] = item
            continue
        new_key = (
            item.confidence,
            rank.get(item.occurrence_kind, 0),
            -_distance(seed, item),
        )
        old_key = (
            current.confidence,
            rank.get(current.occurrence_kind, 0),
            -_distance(seed, current),
        )
        if new_key > old_key:
            best[item.entity_type] = item
    return list(best.values())


def _distance(left: EntityOccurrence, right: EntityOccurrence) -> int:
    if left.character_end < right.character_start:
        return right.character_start - left.character_end
    if right.character_end < left.character_start:
        return left.character_start - right.character_end
    return 0


def _blank_line_blocks(text: str) -> list[tuple[int, int]]:
    if not text:
        return []
    blocks: list[tuple[int, int]] = []
    cursor = 0
    for piece in text.split("\n\n"):
        start = text.find(piece, cursor)
        if start < 0:
            start = cursor
        end = start + len(piece)
        if piece.strip():
            blocks.append((start, end))
        cursor = end + 2
    return blocks


def _block_containing(offset: int, blocks: list[tuple[int, int]]) -> tuple[int, int] | None:
    for start, end in blocks:
        if start <= offset <= end:
            return start, end
    return None

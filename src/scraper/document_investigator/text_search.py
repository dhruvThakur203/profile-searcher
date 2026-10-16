from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz.fuzz import ratio

from ...normalize import normalize_address, normalize_name
from .models import EntityOccurrence, EntityType, OccurrenceKind, ShareholderTarget


@dataclass(frozen=True)
class SearchConfig:
    """Tolerant source search settings. Final identity decisions live elsewhere."""

    min_address_token_overlap: int = 2
    address_window_slack: int = 2
    min_address_score: float = 80.0


def entity_queries(target: ShareholderTarget) -> list[tuple[EntityType, str]]:
    pairs = (
        (EntityType.NAME, target.name),
        (EntityType.ADDRESS1, target.address1),
        (EntityType.ADDRESS2, target.address2),
        (EntityType.ADDRESS3, target.address3),
        (EntityType.ADDRESS4, target.address4),
        (EntityType.JOINT1, target.joint1),
        (EntityType.JOINT2, target.joint2),
    )
    primary = normalize_name(target.name)
    queries = []
    for entity, value in pairs:
        if not value or not value.strip():
            continue
        if entity in NAME_ENTITY_TYPES and entity != EntityType.NAME:
            # A joint holder identical to the primary adds nothing and would
            # make every primary occurrence look like joint co-occurrence.
            if normalize_name(value) == primary:
                continue
        queries.append((entity, value.strip()))
    return queries


NAME_ENTITY_TYPES = {EntityType.NAME, EntityType.JOINT1, EntityType.JOINT2}


def search_entities(
    target: ShareholderTarget,
    text: str,
    *,
    page_of_offset=None,
    config: SearchConfig | None = None,
) -> list[EntityOccurrence]:
    """Search each target entity independently. Never concatenates fields."""
    settings = config or SearchConfig()
    occurrences: list[EntityOccurrence] = []
    for entity_type, query in entity_queries(target):
        if entity_type in NAME_ENTITY_TYPES:
            found = _search_name(
                query,
                text,
                page_of_offset=page_of_offset,
                entity_type=entity_type,
            )
        else:
            found = _search_address(
                entity_type,
                query,
                text,
                page_of_offset=page_of_offset,
                config=settings,
            )
        occurrences.extend(found)
    return occurrences


def _search_name(
    query: str,
    text: str,
    *,
    page_of_offset=None,
    entity_type: EntityType = EntityType.NAME,
) -> list[EntityOccurrence]:
    normalized_query = normalize_name(query)
    query_tokens = normalized_query.split()
    if not query_tokens:
        return []

    norm_text, index_map = normalized_with_map(text)
    text_tokens = norm_text.split()
    if not text_tokens:
        return []
    token_spans = token_spans_of(norm_text)
    occurrences: list[EntityOccurrence] = []
    seen: set[tuple[int, int, str]] = set()

    width = len(query_tokens)
    for start in range(len(text_tokens) - width + 1):
        window = text_tokens[start : start + width]
        if window != query_tokens:
            continue
        occ = occurrence_from_token_window(
            entity_type=entity_type,
            query=query,
            normalized_query=normalized_query,
            text=text,
            index_map=index_map,
            token_spans=token_spans,
            token_start=start,
            token_end=start + width,
            kind=OccurrenceKind.EXACT,
            confidence=1.0,
            page_of_offset=page_of_offset,
        )
        key = (occ.character_start, occ.character_end, occ.occurrence_kind.value)
        if key not in seen:
            seen.add(key)
            occurrences.append(occ)

    query_set = set(query_tokens)
    for start in range(len(text_tokens)):
        # COMPATIBLE needs window[0] == query[0]; POTENTIAL needs window ⊆ query.
        if text_tokens[start] not in query_set:
            continue
        max_width = min(len(query_tokens) + 2, len(text_tokens) - start + 1)
        for width in range(2, max_width):
            window = text_tokens[start : start + width]
            kind, confidence = name_window_kind(query_tokens, window)
            if kind is None or kind == OccurrenceKind.EXACT:
                continue
            occ = occurrence_from_token_window(
                entity_type=entity_type,
                query=query,
                normalized_query=normalized_query,
                text=text,
                index_map=index_map,
                token_spans=token_spans,
                token_start=start,
                token_end=start + width,
                kind=kind,
                confidence=confidence,
                page_of_offset=page_of_offset,
            )
            key = (occ.character_start, occ.character_end, occ.occurrence_kind.value)
            if key in seen:
                continue
            if any(
                other.occurrence_kind == OccurrenceKind.EXACT
                and other.character_start <= occ.character_start
                and occ.character_end <= other.character_end
                for other in occurrences
            ):
                continue
            seen.add(key)
            occurrences.append(occ)
    return occurrences


def name_window_kind(
    query_tokens: list[str], window: list[str]
) -> tuple[OccurrenceKind | None, float]:
    if not window or not query_tokens:
        return None, 0.0
    if window == query_tokens:
        return OccurrenceKind.EXACT, 1.0
    if is_compatible_name(query_tokens, window):
        return OccurrenceKind.COMPATIBLE, 0.85
    if is_ordered_subsequence(window, query_tokens) and len(window) < len(query_tokens):
        return OccurrenceKind.POTENTIAL, 0.45
    return None, 0.0


def is_compatible_name(query_tokens: list[str], window: list[str]) -> bool:
    """First/last aligned; query middle tokens must be present or initialised."""
    if len(window) < 2 or len(query_tokens) < 2:
        return False
    if window[0] != query_tokens[0]:
        return False

    q_middle = query_tokens[1:-1]
    q_last = query_tokens[-1]
    w_middle = window[1:-1]
    w_last = window[-1]

    last_ok = (
        w_last == q_last
        or q_last.endswith(w_last)
        or w_last.endswith(q_last)
    )
    if not last_ok:
        return False

    # Consume query middle tokens using window middles (full token or initial).
    qi = 0
    for token in w_middle:
        if qi < len(q_middle) and token == q_middle[qi]:
            qi += 1
            continue
        if len(token) == 1:
            # Initial for a query middle token, or for the compound last name.
            if qi < len(q_middle) and q_middle[qi].startswith(token):
                qi += 1
                continue
            if q_last.startswith(token) and w_last != q_last:
                continue
        return False

    # Unmatched query middle tokens are not allowed (prevents Dhruv Thakur == Dhruv Singh Thakur).
    if qi < len(q_middle):
        return False

    # Two-token compound surname vs first + initial + surname fragment.
    if len(query_tokens) == 2 and len(window) >= 3 and w_last != q_last:
        initial_tokens = [token for token in w_middle if len(token) == 1]
        if not initial_tokens:
            return False
        if not q_last.startswith(initial_tokens[0]):
            return False
    return True


def is_ordered_subsequence(short: list[str], long: list[str]) -> bool:
    if not short:
        return False
    iterator = iter(long)
    return all(token in iterator for token in short)


def _search_address(
    entity_type: EntityType,
    query: str,
    text: str,
    *,
    page_of_offset=None,
    config: SearchConfig,
) -> list[EntityOccurrence]:
    normalized_query = normalize_address(query)
    query_tokens = normalized_query.split()
    if not query_tokens:
        return []

    norm_text, index_map = normalized_with_map(text)
    text_tokens = norm_text.split()
    token_spans = token_spans_of(norm_text)
    target_len = len(query_tokens)
    lengths = {
        length
        for delta in range(-config.address_window_slack, config.address_window_slack + 1)
        if (length := target_len + delta) > 0
    }
    occurrences: list[EntityOccurrence] = []
    seen: set[tuple[int, int]] = set()
    required_overlap = min(config.min_address_token_overlap, len(query_tokens))

    for length in sorted(lengths):
        if length > len(text_tokens):
            continue
        for start in range(len(text_tokens) - length + 1):
            window = text_tokens[start : start + length]
            overlap = len(set(window) & set(query_tokens))
            if overlap < required_overlap:
                continue
            score = float(ratio(" ".join(query_tokens), " ".join(window)))
            if score < config.min_address_score and " ".join(window) != normalized_query:
                continue
            occ = occurrence_from_token_window(
                entity_type=entity_type,
                query=query,
                normalized_query=normalized_query,
                text=text,
                index_map=index_map,
                token_spans=token_spans,
                token_start=start,
                token_end=start + length,
                kind=OccurrenceKind.ADDRESS,
                confidence=round(score / 100.0, 3),
                page_of_offset=page_of_offset,
            )
            key = (occ.character_start, occ.character_end)
            if key in seen:
                continue
            seen.add(key)
            occurrences.append(occ)
    return occurrences


def occurrence_from_token_window(
    *,
    entity_type: EntityType,
    query: str,
    normalized_query: str,
    text: str,
    index_map: list[int],
    token_spans: list[tuple[int, int]],
    token_start: int,
    token_end: int,
    kind: OccurrenceKind,
    confidence: float,
    page_of_offset=None,
) -> EntityOccurrence:
    norm_start = token_spans[token_start][0]
    norm_end = token_spans[token_end - 1][1]
    char_start = index_map[norm_start] if norm_start < len(index_map) else 0
    end_index = min(norm_end - 1, len(index_map) - 1)
    char_end = index_map[end_index] + 1 if index_map else 0
    char_end = min(max(char_end, char_start), len(text))
    matched = text[char_start:char_end]
    page = page_of_offset(char_start) if page_of_offset else None
    return EntityOccurrence(
        entity_type=entity_type,
        original_query=query,
        normalized_query=normalized_query,
        matched_text=matched,
        character_start=char_start,
        character_end=char_end,
        surrounding_context="",
        confidence=confidence,
        occurrence_kind=kind,
        page_number=page,
    )


_NORMALIZED_CACHE: list = []


def normalized_with_map(text: str) -> tuple[str, list[int]]:
    """Build normalize_text(text) and map each normalized offset to a source offset.

    Every entity of one document is searched against the same text, so the
    last result is reused. Callers must treat the returned map as read-only.
    """
    if _NORMALIZED_CACHE and _NORMALIZED_CACHE[0] is text:
        return _NORMALIZED_CACHE[1]
    result = _build_normalized_with_map(text)
    _NORMALIZED_CACHE[:] = [text, result]
    return result


def _build_normalized_with_map(text: str) -> tuple[str, list[int]]:
    out: list[str] = []
    index_map: list[int] = []
    prev_space = True
    for index, char in enumerate(str(text)):
        pieces = list(unicodedata.normalize("NFKC", char).casefold())
        if char == "&" or pieces == ["&"]:
            pieces = list(" and ")
        for piece in pieces:
            if re.match(r"[a-z0-9]", piece):
                out.append(piece)
                index_map.append(index)
                prev_space = False
            else:
                if not prev_space:
                    out.append(" ")
                    index_map.append(index)
                    prev_space = True
    while out and out[0] == " ":
        out.pop(0)
        index_map.pop(0)
    while out and out[-1] == " ":
        out.pop()
        index_map.pop()
    return "".join(out), index_map


def token_spans_of(normalized: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in re.finditer(r"\S+", normalized)]

from __future__ import annotations

from dataclasses import dataclass

from .models import EntityOccurrence, SourceDocument


@dataclass(frozen=True)
class ContextConfig:
    chars_before: int = 500
    chars_after: int = 500


def attach_context(
    occurrences: list[EntityOccurrence],
    text: str,
    config: ContextConfig | None = None,
) -> list[EntityOccurrence]:
    settings = config or ContextConfig()
    enriched = []
    for occurrence in occurrences:
        start = max(0, occurrence.character_start - settings.chars_before)
        end = min(len(text), occurrence.character_end + settings.chars_after)
        enriched.append(
            EntityOccurrence(
                entity_type=occurrence.entity_type,
                original_query=occurrence.original_query,
                normalized_query=occurrence.normalized_query,
                matched_text=occurrence.matched_text,
                character_start=occurrence.character_start,
                character_end=occurrence.character_end,
                surrounding_context=text[start:end],
                confidence=occurrence.confidence,
                occurrence_kind=occurrence.occurrence_kind,
                page_number=occurrence.page_number,
            )
        )
    return enriched


def page_offset_resolver(document: SourceDocument):
    """Return a function mapping character offset -> 1-based page number."""
    if not document.page_texts:
        return lambda _offset: None

    boundaries: list[tuple[int, int, int]] = []
    cursor = 0
    for page_number, page_text in enumerate(document.page_texts, start=1):
        # Pages are joined with '\n' in SourceDocument.__post_init__ when text empty;
        # investigator always rebuilds text the same way.
        start = cursor
        end = start + len(page_text)
        boundaries.append((start, end, page_number))
        cursor = end + 1  # account for joining newline

    def page_of(offset: int) -> int | None:
        for start, end, page_number in boundaries:
            if start <= offset <= end:
                return page_number
        if boundaries and offset >= boundaries[-1][1]:
            return boundaries[-1][2]
        return None

    return page_of


def joined_text(document: SourceDocument) -> str:
    if document.page_texts:
        return "\n".join(document.page_texts)
    return document.text or ""

"""Obituary / condolence lead extraction from an already-fetched source.

Lead generation only. Nothing produced here is identity evidence: a lead can
never satisfy the final identity policy, and only entities explicitly written
in the passage are captured (no relationships are inferred from surnames, no
employment is inferred from a company mention).
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field

from ...normalize import normalize_name
from .context import joined_text, page_offset_resolver
from .models import OccurrenceKind, ShareholderTarget, SourceDocument
from .text_search import _search_name


LEAD_NOTE = (
    "Lead only: obituary/condolence context is not identity evidence and does "
    "not establish current contact details."
)
MAX_LEADS_PER_DOCUMENT = 3
MAX_EXCERPT_CHARS = 1000
MAX_FOLLOW_UP_QUERIES = 6

OBITUARY_KEYWORDS = re.compile(
    r"(?i)\b("
    r"obituar(?:y|ies)|condolences?|sympath(?:y|ies)|demise|passed\s+away|"
    r"rest\s+in\s+peace|in\s+loving\s+memory|funeral|memorial|bereavement|"
    r"deceased|died|death|cremation|prayer\s+meeting|tribute"
    r")\b"
)
_KEYWORD_STEMS = (
    "obituar", "condolence", "sympath", "demise", "passed", "rest in", "loving memory",
    "funeral", "memorial", "bereavement", "deceased", "died", "death", "cremation",
    "prayer", "tribute",
)
_REGION_RADIUS = 4000

RELATION_TERMS = {
    "parents": "parent",
    "parent": "parent",
    "father": "parent",
    "mother": "parent",
    "children": "child",
    "child": "child",
    "son": "child",
    "sons": "child",
    "daughter": "child",
    "daughters": "child",
    "wife": "spouse",
    "husband": "spouse",
    "spouse": "spouse",
    "widow": "spouse",
    "widower": "spouse",
    "brother": "sibling",
    "brothers": "sibling",
    "sister": "sibling",
    "sisters": "sibling",
    "siblings": "sibling",
    "grandparents": "grandparent",
    "grandfather": "grandparent",
    "grandmother": "grandparent",
    "grandchildren": "grandchild",
    "grandson": "grandchild",
    "grandsons": "grandchild",
    "granddaughter": "grandchild",
    "granddaughters": "grandchild",
    "family": "family",
    "families": "family",
    "relatives": "family",
}

_HONORIFICS = {"mr", "mrs", "ms", "miss", "shri", "sri", "smt", "dr", "late", "prof", "sir"}

_COMMON_WORDS = set(
    """
    a about above after again against all also am an and any are as at be because been
    before being below between both but by can could did do does doing down during each
    few for from further had has have having he her here hers herself him himself his how
    i if in into is it its itself just me more most my myself no nor not now of off on
    once only or other our ours ourselves out over own same she should so some such than
    that the their theirs them themselves then there these they this those through to too
    under until up very was we were what when where which while who whom why will with
    would you your yours yourself yourselves upon among within without whose may might
    must shall us one two three many much every everyone anyone someone
    obituary obituaries condolence condolences sympathy sympathies deepest deep heartfelt
    sincere sincerest tragic tragically sad sadly sudden suddenly untimely demise passed
    passing away death died dies deceased beloved dear dearest loving loved memory
    memories memorial remembrance remembered rest peace rip funeral service services
    cremation burial prayer prayers meet meeting held missed miss greatly deeply forever
    always survived survives survive predeceased leaves left behind gone soon life lived
    celebration celebrate tribute tributes homage respects pay paying offer offered
    offering offers extend extended express expressed staff team colleagues colleague
    friends friend management members member board directors director employees loss
    lost grief grieve grieving mourning mourn mourns bereaved bereavement thoughts heart
    hearts soul souls god bless blessed strength comfort time difficult news announce
    announces announced regret inform informs informed great good kind wonderful
    home hospital age aged years year old born day days today yesterday morning evening
    night week month
    january february march april june july august september october november december
    monday tuesday wednesday thursday friday saturday sunday
    page post posted comments comment reply share like read click here view website
    contact privacy terms login sign home search menu
    """.split()
)
_STOP_WORDS = _COMMON_WORDS | set(RELATION_TERMS) | _HONORIFICS

_COMPANY_SUFFIXES = {
    "ltd",
    "limited",
    "llc",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "plc",
    "llp",
    "gmbh",
    "bhd",
}
_COMPANY_BREAK = {
    "of", "by", "from", "at", "with", "the", "to", "for", "in", "on", "and", "our",
    "staff", "team", "management", "all", "colleagues", "friends", "family", "members",
    "everyone", "employees", "directors", "board",
}
_LOCATION_PREPS = {"in", "at", "from", "near"}
_COUNTRIES = {
    "india", "singapore", "usa", "u.s.a", "america", "canada", "australia", "malaysia",
    "uk", "england", "london", "kenya", "uae", "dubai", "hong", "japan", "germany",
    "france", "nepal", "srilanka", "indonesia", "thailand", "zealand",
}
_SENTENCE_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "ltd", "pte", "pvt", "co", "inc", "corp", "smt", "jr", "sr"}

_TOKEN = re.compile(r"\([A-Za-z0-9]{1,3}\)|[A-Za-z][A-Za-z'\-]*\.?|\d+|[,;:&/!?.]")


@dataclass
class RelationshipMention:
    term: str
    category: str
    phrase: str
    named: list[str] = field(default_factory=list)
    preceding_names: list[str] = field(default_factory=list)
    sentence_names: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "term": self.term,
            "category": self.category,
            "phrase": self.phrase,
            "named": list(self.named),
            "preceding_names": list(self.preceding_names),
            "sentence_names": list(self.sentence_names),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RelationshipMention":
        return cls(
            term=data.get("term", ""),
            category=data.get("category", ""),
            phrase=data.get("phrase", ""),
            named=list(data.get("named") or []),
            preceding_names=list(data.get("preceding_names") or []),
            sentence_names=list(data.get("sentence_names") or []),
        )


@dataclass
class ObituaryLead:
    source_url: str
    title: str
    document_type: str
    excerpt: str
    keywords: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    relationships: list[RelationshipMention] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)
    primary_name_mention: str = ""
    primary_name_text: str = ""
    joint_holder_mentions: dict[str, str] = field(default_factory=dict)
    follow_up_queries: list[str] = field(default_factory=list)
    page_number: int | None = None
    strategies: list[str] = field(default_factory=list)
    original_url: str = ""
    note: str = LEAD_NOTE

    def to_dict(self) -> dict:
        return {
            "source_url": self.source_url,
            "title": self.title,
            "document_type": self.document_type,
            "excerpt": self.excerpt,
            "keywords": list(self.keywords),
            "names": list(self.names),
            "relationships": [item.to_dict() for item in self.relationships],
            "locations": list(self.locations),
            "companies": list(self.companies),
            "primary_name_mention": self.primary_name_mention,
            "primary_name_text": self.primary_name_text,
            "joint_holder_mentions": dict(self.joint_holder_mentions),
            "follow_up_queries": list(self.follow_up_queries),
            "page_number": self.page_number,
            "strategies": list(self.strategies),
            "original_url": self.original_url,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ObituaryLead":
        return cls(
            source_url=data.get("source_url", ""),
            title=data.get("title", ""),
            document_type=data.get("document_type", ""),
            excerpt=data.get("excerpt", ""),
            keywords=list(data.get("keywords") or []),
            names=list(data.get("names") or []),
            relationships=[
                RelationshipMention.from_dict(item)
                for item in data.get("relationships") or []
            ],
            locations=list(data.get("locations") or []),
            companies=list(data.get("companies") or []),
            primary_name_mention=data.get("primary_name_mention", ""),
            primary_name_text=data.get("primary_name_text", ""),
            joint_holder_mentions=dict(data.get("joint_holder_mentions") or {}),
            follow_up_queries=list(data.get("follow_up_queries") or []),
            page_number=data.get("page_number"),
            strategies=list(data.get("strategies") or []),
            original_url=data.get("original_url", ""),
            note=data.get("note") or LEAD_NOTE,
        )


@dataclass
class _Token:
    text: str
    start: int
    end: int
    word: str
    kind: str
    sentence_end: bool = False


def extract_obituary_leads(
    target: ShareholderTarget,
    document: SourceDocument,
) -> list[ObituaryLead]:
    """Return obituary/condolence leads whose passage names the primary holder."""
    text = joined_text(document)
    primary_tokens = normalize_name(target.name).split()
    if len(primary_tokens) < 2 or not text:
        return []
    lowered = text.casefold()
    if primary_tokens[-1] not in lowered:
        return []
    positions = _keyword_positions(text, lowered)
    if not positions:
        return []

    page_of = page_offset_resolver(document)
    protected = set(primary_tokens)
    for joint in (target.joint1, target.joint2):
        protected.update(normalize_name(joint).split())

    leads: list[ObituaryLead] = []
    for start, end in _passage_windows(text, positions):
        passage = text[start:end]
        if primary_tokens[-1] not in passage.casefold():
            continue
        mention, mention_text = primary_name_mention(target.name, passage)
        if not mention:
            continue
        keywords = _unique(
            " ".join(match.group(0).split()).casefold()
            for match in OBITUARY_KEYWORDS.finditer(passage)
        )
        entities = extract_passage_entities(passage, protected_tokens=protected)
        joint_mentions = {}
        for field_name in ("joint1", "joint2"):
            supplied = (getattr(target, field_name, "") or "").strip()
            if not supplied:
                continue
            kind, _ = primary_name_mention(supplied, passage)
            joint_mentions[field_name] = kind or "NOT_MENTIONED"
        lead = ObituaryLead(
            source_url=document.source_url,
            title=document.title,
            document_type=document.document_type,
            excerpt=_clip(passage),
            keywords=keywords,
            names=entities["names"],
            relationships=entities["relationships"],
            locations=entities["locations"],
            companies=entities["companies"],
            primary_name_mention=mention,
            primary_name_text=mention_text,
            joint_holder_mentions=joint_mentions,
            page_number=page_of(start),
        )
        lead.follow_up_queries = follow_up_queries(target, lead)
        leads.append(lead)
        if len(leads) >= MAX_LEADS_PER_DOCUMENT:
            break
    return leads


def primary_name_mention(name: str, passage: str) -> tuple[str, str]:
    """How a holder name is written in the passage: EXACT, COMPATIBLE or
    COORDINATED_SHARED_SURNAME (``PRATAP AND VIBHA MALPANI``). Empty if absent."""
    occurrences = _search_name(name, passage)
    for kind in (OccurrenceKind.EXACT, OccurrenceKind.COMPATIBLE):
        for occ in occurrences:
            if occ.occurrence_kind == kind:
                return kind.value, occ.matched_text
    coordinated = _coordinated_surname_mention(name, passage)
    if coordinated:
        return "COORDINATED_SHARED_SURNAME", coordinated
    return "", ""


def _coordinated_surname_mention(name: str, passage: str) -> str:
    query = normalize_name(name).split()
    if len(query) < 2:
        return ""
    words = normalize_name(passage).split()
    head, last = query[:-1], query[-1]
    for index in range(len(words) - len(head)):
        if words[index : index + len(head)] != head:
            continue
        rest = words[index + len(head) : index + len(head) + 5]
        if not rest or rest[0] not in {"and"}:
            continue
        for offset in range(2, len(rest)):
            if rest[offset] == last:
                return " ".join(words[index : index + len(head) + offset + 1]).upper()
    return ""


def extract_passage_entities(
    passage: str,
    *,
    protected_tokens: set[str] | None = None,
) -> dict:
    """Extract only explicitly written names, relationships, locations, companies."""
    protected = protected_tokens or set()
    tokens = _tokenize(passage)
    consumed: set[int] = set()

    companies = _extract_companies(passage, tokens, consumed)
    locations = _extract_locations(passage, tokens, consumed, protected)
    runs = _name_runs(tokens, consumed, protected)
    names = _unique(_surface(passage, tokens, run) for run in runs)
    relationships = _extract_relationships(passage, tokens, runs)
    return {
        "names": names,
        "relationships": relationships,
        "locations": locations,
        "companies": companies,
    }


def follow_up_queries(target: ShareholderTarget, lead: ObituaryLead) -> list[str]:
    """Suggested (not executed) follow-up searches built from explicit entities."""
    primary = set(normalize_name(target.name).split())
    queries: list[str] = []
    location = lead.locations[0].replace(",", " ") if lead.locations else ""
    for name in lead.names:
        tokens = set(normalize_name(name).split())
        if not tokens or tokens <= primary:
            continue
        queries.append(" ".join(f"{name} {location}".split()))
    for company in lead.companies:
        queries.append(f"{target.name.strip()} {company}")
    return _unique(queries)[:MAX_FOLLOW_UP_QUERIES]


def _keyword_positions(text: str, lowered: str) -> list[int]:
    """Keyword offsets found with literal scans; a whole-document regex over
    multi-megabyte PDF text is far too slow to run per source."""
    positions: set[int] = set()
    for stem in _KEYWORD_STEMS:
        cursor = lowered.find(stem)
        while cursor >= 0:
            left = max(0, cursor - 1)
            match = OBITUARY_KEYWORDS.search(text, left, cursor + 40)
            if match is not None and match.start() <= cursor + len(stem):
                positions.add(match.start())
            cursor = lowered.find(stem, cursor + len(stem))
    return sorted(positions)


def _passage_windows(text: str, positions: list[int]) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    for position in positions:
        region_start = max(0, position - _REGION_RADIUS)
        region_end = min(len(text), position + _REGION_RADIUS)
        units = [
            (region_start + start, region_start + end)
            for start, end in _sentence_units(text[region_start:region_end])
        ]
        starts = [start for start, _ in units]
        index = bisect_right(starts, position) - 1
        if index < 0 or position >= units[index][1]:
            continue
        # Neighbouring units join the passage only when they read like prose,
        # so navigation/menu lines and headings do not contribute "names".
        first = last = index
        if _is_prose(text, units[index]):
            while first > 0 and index - first < 2 and _is_prose(text, units[first - 1]):
                first -= 1
            while last < len(units) - 1 and last - index < 3 and _is_prose(text, units[last + 1]):
                last += 1
        start, end = units[first][0], units[last][1]
        if end - start > MAX_EXCERPT_CHARS * 2:
            start, end = units[index]
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    return windows


def _is_prose(text: str, unit: tuple[int, int]) -> bool:
    return len(re.findall(r"[A-Za-z]{2,}", text[unit[0] : unit[1]])) >= 5


def _sentence_units(text: str) -> list[tuple[int, int]]:
    units = []
    cursor = 0
    for match in re.finditer(r"(?<![A-Z]\.)(?<=[.!?])\s+|\n+", text):
        if match.start() > cursor:
            units.append((cursor, match.start()))
        cursor = match.end()
    if cursor < len(text):
        units.append((cursor, len(text)))
    return units or [(0, len(text))]


def _tokenize(passage: str) -> list[_Token]:
    tokens = []
    for match in _TOKEN.finditer(passage):
        raw = match.group(0)
        if raw.startswith("("):
            kind = "paren"
            word = raw.casefold()
        elif raw[0].isalpha():
            kind = "word"
            word = raw.rstrip(".").casefold()
        elif raw.isdigit():
            kind = "num"
            word = raw
        else:
            kind = "punct"
            word = raw
        token = _Token(raw, match.start(), match.end(), word, kind)
        if kind == "punct" and raw in {".", "!", "?"}:
            token.sentence_end = True
        elif kind == "word" and raw.endswith(".") and len(word) > 1:
            token.sentence_end = word not in _SENTENCE_ABBREVIATIONS
        tokens.append(token)
    return tokens


def _is_name_token(token: _Token, protected: set[str]) -> bool:
    if token.kind != "word":
        return False
    if not token.text[0].isupper():
        return False
    if token.word in protected:
        return True
    if token.word in _STOP_WORDS or token.word in _COMPANY_SUFFIXES:
        return False
    if token.word.replace(".", "") in _COUNTRIES:
        return False
    return True


def _extract_companies(passage: str, tokens: list[_Token], consumed: set[int]) -> list[str]:
    companies = []
    for index, token in enumerate(tokens):
        if token.kind != "word" or token.word not in _COMPANY_SUFFIXES:
            continue
        first = index
        cursor = index - 1
        while cursor >= 0 and index - cursor <= 8:
            prior = tokens[cursor]
            if prior.sentence_end and cursor != index - 1:
                break
            if prior.kind == "paren" or prior.text == "&":
                first = cursor
            elif prior.kind == "word" and prior.word not in _COMPANY_BREAK and not prior.sentence_end:
                first = cursor
            else:
                break
            cursor -= 1
        span = list(range(first, index + 1))
        core = [
            tokens[i]
            for i in span
            if tokens[i].kind == "word"
            and tokens[i].word not in _COMPANY_SUFFIXES
            and tokens[i].word not in {"pte", "pvt", "private", "sdn", "co"}
        ]
        if not core:
            continue
        consumed.update(span)
        companies.append(passage[tokens[first].start : tokens[index].end].rstrip(".,"))
    return _unique(companies)


def _extract_locations(
    passage: str,
    tokens: list[_Token],
    consumed: set[int],
    protected: set[str],
) -> list[str]:
    locations = []
    for index, token in enumerate(tokens):
        if token.kind != "word" or token.word not in _LOCATION_PREPS:
            continue
        parts: list[list[int]] = []
        current: list[int] = []
        cursor = index + 1
        while cursor < len(tokens):
            item = tokens[cursor]
            if cursor in consumed:
                break
            is_place = item.kind == "word" and item.text[0].isupper() and (
                item.word.replace(".", "") in _COUNTRIES or _is_name_token(item, set())
            )
            if is_place and item.word not in protected:
                current.append(cursor)
                cursor += 1
                if item.sentence_end:
                    break
                continue
            if item.text == "," and current:
                parts.append(current)
                current = []
                cursor += 1
                continue
            break
        if current:
            parts.append(current)
        parts = [part for part in parts if len(part) <= 3]
        if not parts:
            continue
        for part in parts:
            consumed.update(part)
        locations.append(", ".join(_surface(passage, tokens, part) for part in parts))
    for index, token in enumerate(tokens):
        if index in consumed or token.kind != "word":
            continue
        if token.text[0].isupper() and token.word.replace(".", "") in _COUNTRIES:
            consumed.add(index)
            locations.append(token.text.rstrip(".") if len(token.word) > 3 else token.text)
    return _unique(locations)


def _name_runs(
    tokens: list[_Token],
    consumed: set[int],
    protected: set[str],
) -> list[list[int]]:
    runs: list[list[int]] = []
    current: list[int] = []

    def close() -> None:
        if current and len(current) <= 4 and any(len(tokens[i].word) > 1 for i in current):
            runs.append(list(current))
        current.clear()

    for index, token in enumerate(tokens):
        if index not in consumed and _is_name_token(token, protected):
            current.append(index)
            if token.sentence_end:
                close()
            continue
        close()
    close()
    return runs


def _extract_relationships(
    passage: str,
    tokens: list[_Token],
    runs: list[list[int]],
) -> list[RelationshipMention]:
    run_at = {run[0]: run for run in runs}
    run_end = {run[-1]: run for run in runs}
    sentences = _sentence_ranges(tokens)
    mentions: list[RelationshipMention] = []
    for index, token in enumerate(tokens):
        if token.kind != "word" or token.word not in RELATION_TERMS:
            continue
        named_runs: list[list[int]] = []
        cursor = index + 1
        while cursor < len(tokens):
            item = tokens[cursor]
            if cursor in run_at:
                run = run_at[cursor]
                named_runs.append(run)
                cursor = run[-1] + 1
                if tokens[run[-1]].sentence_end:
                    break
                continue
            if item.word in {"and", ",", "&"} | _HONORIFICS:
                cursor += 1
                continue
            break
        preceding_runs = []
        if index - 1 in run_end:
            preceding_runs.append(run_end[index - 1])
        elif index - 2 in run_end and tokens[index - 1].word in {"'s", "s"}:
            preceding_runs.append(run_end[index - 2])
        sentence = next((rng for rng in sentences if rng[0] <= index <= rng[1]), (0, len(tokens) - 1))
        listed = {tuple(run) for run in named_runs + preceding_runs}
        sentence_runs = [
            run
            for run in runs
            if sentence[0] <= run[0] <= sentence[1] and tuple(run) not in listed
        ]
        phrase_start = tokens[max(sentence[0], index - 3)].start
        if preceding_runs:
            phrase_start = min(phrase_start, tokens[preceding_runs[0][0]].start)
        phrase_end = tokens[named_runs[-1][-1]].end if named_runs else token.end
        mentions.append(
            RelationshipMention(
                term=token.text.rstrip(".,"),
                category=RELATION_TERMS[token.word],
                phrase=" ".join(passage[phrase_start:phrase_end].split()).rstrip("."),
                named=[_surface(passage, tokens, run) for run in named_runs],
                preceding_names=[_surface(passage, tokens, run) for run in preceding_runs],
                sentence_names=[_surface(passage, tokens, run) for run in sentence_runs],
            )
        )
    return mentions


def _sentence_ranges(tokens: list[_Token]) -> list[tuple[int, int]]:
    ranges = []
    start = 0
    for index, token in enumerate(tokens):
        if token.sentence_end:
            ranges.append((start, index))
            start = index + 1
    if start < len(tokens):
        ranges.append((start, len(tokens) - 1))
    return ranges


def _surface(passage: str, tokens: list[_Token], run: list[int]) -> str:
    return " ".join(passage[tokens[run[0]].start : tokens[run[-1]].end].split()).rstrip(".,")


def _clip(passage: str) -> str:
    compact = " ".join(passage.split())
    if len(compact) > MAX_EXCERPT_CHARS:
        compact = compact[: MAX_EXCERPT_CHARS - 3] + "..."
    return compact


def _unique(values) -> list[str]:
    seen = set()
    out = []
    for value in values:
        key = " ".join(str(value).split()).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(" ".join(str(value).split()))
    return out

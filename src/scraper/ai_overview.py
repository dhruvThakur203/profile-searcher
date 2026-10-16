"""Detect and parse Google AI Overviews from rendered SERP HTML or Playwright JS."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urlparse

from ..normalize import normalize_text
from .phones import plausible_phone
from .result_parser import EMAIL_PATTERN, extract_contacts
from .urls import is_valid_external_source_url, unwrap_google_href


STATUS_PRESENT = "PRESENT"
STATUS_NOT_PRESENT = "NOT_PRESENT"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_CAPTURE_FAILED = "CAPTURE_FAILED"
STATUS_BLOCKED = "BLOCKED"

VERIFY_UNVERIFIED = "UNVERIFIED"
VERIFY_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
VERIFY_SOURCE_LISTED = "SOURCE_LISTED"

OVERVIEW_CONTAINER_ATTRS = (
    ('data-attrid', 'SGCOP'),
    ('data-container-id', 'aimfl'),
)

OVERVIEW_UNAVAILABLE_RE = re.compile(
    r"(?is)"
    r"(?:AI\s+Overview\s+is\s+not\s+available|"
    r"can(?:'|’)?t\s+generate\s+an\s+AI\s+overview|"
    r"cannot\s+generate\s+an\s+AI\s+overview)",
)

OVERVIEW_JUNK_BODY_RE = re.compile(
    r"(?is)"
    r"(?:Error\s+translating\s+content|Privacy\s+Policy|Terms\s+of\s+Service)",
)

CSS_LEAK_RE = re.compile(
    r"(?is)"
    r"(?:\.\w+\s*\{|background-color\s*:|var\s*\(--|font-family\s*:|"
    r"border-radius\s*:|@media\s|!important)",
)

CSS_INTEGER_BLOCKLIST = frozenset(
    {
        "2147483648",
        "2654435769",
        "4294967295",
        "16777216",
    }
)

INSTITUTION_STOPWORDS = frozenset(
    {
        "var",
        "calc",
        "rgb",
        "rgba",
        "none",
        "inherit",
        "important",
        "flex",
        "center",
    }
)

GOOGLE_NON_SOURCE_HOST_SUFFIXES = (
    "policies.google.com",
    "support.google.com",
    "accounts.google.com",
    "consent.google.com",
    "myaccount.google.com",
)

INSTITUTIONAL_CONTEXT = re.compile(
    r"(?i)\b("
    r"institutional|university|universiti|college|hospital|office\s+phone|"
    r"fax|department|centre\s+for|center\s+for|faculty|campus"
    r")\b"
)
PERSONAL_CONTEXT = re.compile(r"(?i)\b(personal|mobile|cell|private)\b")

AI_OVERVIEW_EXTRACT_JS = """
() => {
  const UNAVAILABLE = /AI Overview is not available|Can't generate an AI overview|cannot generate an AI overview/i;
  const CSS = /\\.\\w+\\s*\\{|background-color\\s*:|var\\s*\\(--/i;
  const JSNAME_MARKERS = ["ZLxsqf", "Ya7sxe"];

  const isBlocked = (href) => {
    if (!href) return true;
    const lower = href.toLowerCase();
    return (
      lower.includes("policies.google.com") ||
      lower.includes("support.google.com") ||
      lower.includes("accounts.google.com") ||
      lower.includes("google.com/preferences")
    );
  };

  const isVisible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const looksLikeNavChip = (text) => {
    const norm = text.replace(/\\s+/g, " ").trim();
    if (/^AI\\s+Overview\\s+AI\\s+Mode/i.test(norm)) return true;
    const head = norm.slice(0, 200);
    if (/AI\\s+Mode/i.test(head) && /Images/i.test(head) && /Shopping/i.test(head)) return true;
    return false;
  };

  const hasOverviewHeading = (el) => {
    for (const h of el.querySelectorAll("span, div, h2, h3")) {
      const t = (h.innerText || "").trim();
      if (/^AI\\s+Overview$/i.test(t) && isVisible(h)) return true;
    }
    const t = (el.innerText || "").replace(/\\s+/g, " ").trim();
    return /^AI\\s+Overview\\b/i.test(t);
  };

  const bodyAfterHeading = (text) =>
    text.replace(/^AI\\s+Overview\\s*/i, "").replace(/\\s+/g, " ").trim();

  const normalizeOverviewText = (raw) => {
    const text = (raw || "").replace(/\\s+/g, " ").trim();
    if (!text) return "";
    if (!UNAVAILABLE.test(text)) return text;
    const matches = [...text.matchAll(/AI\\s+Overview/gi)];
    for (let idx = matches.length - 1; idx >= 0; idx--) {
      const slice = text.slice(matches[idx].index).trim();
      if (UNAVAILABLE.test(slice)) continue;
      const body = bodyAfterHeading(slice);
      if (
        body.length >= 40 &&
        !looksLikeNavChip(slice) &&
        !CSS.test(slice) &&
        !/Error translating content|Privacy Policy/i.test(body)
      ) {
        return slice;
      }
    }
    return text;
  };

  const collectSources = (root) => {
    const sources = [];
    const seen = new Set();
    root.querySelectorAll("a[href]").forEach((a) => {
      const href = a.getAttribute("href") || a.href || "";
      if (!href || isBlocked(href)) return;
      const title = (a.innerText || a.textContent || "").trim();
      const key = href + "\\n" + title;
      if (seen.has(key)) return;
      seen.add(key);
      sources.push({ title, url: href });
    });
    return sources;
  };

  const extractFromRoot = (root) => {
    const text = normalizeOverviewText(root.innerText || "");
    if (UNAVAILABLE.test(text)) {
      return {
        state: "unavailable",
        text: "",
        sources: [],
        truncated: false,
        notes: "Google reported that an AI Overview is not available for this search.",
      };
    }
    if (!text || text.length < 20 || CSS.test(text) || looksLikeNavChip(text)) {
      return null;
    }
    if (!/^AI\\s+Overview\\b/i.test(text)) {
      return null;
    }
    if (bodyAfterHeading(text).length < 20) {
      return null;
    }
    const sources = collectSources(root);
    const truncated =
      /show more/i.test(root.innerText || "") ||
      Boolean(root.querySelector("[aria-expanded='false']"));
    return {
      state: "present",
      text,
      sources,
      truncated,
      notes: truncated ? "Overview may be truncated in the rendered page (Show more)." : "",
    };
  };

  const findLegacyRoot = () => {
    const roots = [
      ...document.querySelectorAll('[data-attrid="SGCOP"]'),
      ...document.querySelectorAll('[data-container-id="aimfl"]'),
    ];
    for (const root of roots) {
      if (!isVisible(root)) continue;
      const t = (root.innerText || "").replace(/\\s+/g, " ").trim();
      if (!t || t.length < 20) continue;
      return root;
    }
    return null;
  };

  const overviewSearchScopes = () => {
    const scopes = [];
    for (const sel of ["#rso", "#search", "#cnt", "#center_col", "main", "#main"]) {
      const el = document.querySelector(sel);
      if (el && !scopes.includes(el)) scopes.push(el);
    }
    if (document.body && !scopes.includes(document.body)) scopes.push(document.body);
    return scopes;
  };

  const pickJsnameCandidate = (search) => {
    for (const jsname of JSNAME_MARKERS) {
      for (const el of search.querySelectorAll(`div[jsname="${jsname}"]`)) {
        if (!isVisible(el) || !hasOverviewHeading(el)) continue;
        const t = normalizeOverviewText(el.innerText || "");
        if (UNAVAILABLE.test(t) || looksLikeNavChip(t)) continue;
        if (bodyAfterHeading(t).length < 20 || CSS.test(t)) continue;
        return el;
      }
    }
    for (const el of search.querySelectorAll("div, section")) {
      if (!isVisible(el)) continue;
      const t = normalizeOverviewText(el.innerText || "");
      if (!/^AI\\s+Overview\\s+/i.test(t)) continue;
      if (UNAVAILABLE.test(t) || looksLikeNavChip(t)) continue;
      if (bodyAfterHeading(t).length < 20 || CSS.test(t)) continue;
      if (t.length > 20000) continue;
      return el;
    }
    return null;
  };

  const findJsnameRoot = () => {
    const seen = new Set();
    for (const search of overviewSearchScopes()) {
      const candidate = pickJsnameCandidate(search);
      if (candidate && !seen.has(candidate)) {
        seen.add(candidate);
        return candidate;
      }
    }
    return null;
  };

  const root = findLegacyRoot() || findJsnameRoot();
  if (!root) {
    return { state: "not_present", text: "", sources: [], truncated: false, notes: "" };
  }
  const result = extractFromRoot(root);
  if (!result) {
    return { state: "not_present", text: "", sources: [], truncated: false, notes: "" };
  }
  return result;
}
"""

_JSNAME_OVERVIEW_MARKERS = ("ZLxsqf", "Ya7sxe")


@dataclass
class AiOverviewSource:
    title: str
    url: str

    def to_dict(self) -> dict:
        return {"title": self.title, "url": self.url}

    @classmethod
    def from_dict(cls, payload: dict) -> AiOverviewSource:
        return cls(title=str(payload.get("title") or ""), url=str(payload.get("url") or ""))


@dataclass
class AiOverviewLogEntry:
    excel_row: int | None
    folio_number: str
    primary_holder: str
    holder_associated: str
    query_strategy: str
    search_query: str
    capture_timestamp: str
    overview_status: str
    raw_overview_text: str = ""
    source_titles: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    extraction_notes: str = ""

    def dedupe_key(self) -> tuple:
        return (
            self.excel_row,
            normalize_text(self.search_query),
            self.overview_status,
            normalize_text(self.raw_overview_text),
        )

    def to_dict(self) -> dict:
        return {
            "excel_row": self.excel_row,
            "folio_number": self.folio_number,
            "primary_holder": self.primary_holder,
            "holder_associated": self.holder_associated,
            "query_strategy": self.query_strategy,
            "search_query": self.search_query,
            "capture_timestamp": self.capture_timestamp,
            "overview_status": self.overview_status,
            "raw_overview_text": self.raw_overview_text,
            "source_titles": list(self.source_titles),
            "source_urls": list(self.source_urls),
            "extraction_notes": self.extraction_notes,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> AiOverviewLogEntry:
        return cls(
            excel_row=payload.get("excel_row"),
            folio_number=str(payload.get("folio_number") or ""),
            primary_holder=str(payload.get("primary_holder") or ""),
            holder_associated=str(payload.get("holder_associated") or ""),
            query_strategy=str(payload.get("query_strategy") or ""),
            search_query=str(payload.get("search_query") or ""),
            capture_timestamp=str(payload.get("capture_timestamp") or ""),
            overview_status=str(payload.get("overview_status") or ""),
            raw_overview_text=str(payload.get("raw_overview_text") or ""),
            source_titles=list(payload.get("source_titles") or []),
            source_urls=list(payload.get("source_urls") or []),
            extraction_notes=str(payload.get("extraction_notes") or ""),
        )


@dataclass
class AiContactLead:
    person_name: str = ""
    email: str = ""
    phone: str = ""
    institution: str = ""
    job_title: str = ""
    location: str = ""
    contact_type: str = ""
    excel_row: int | None = None
    folio_number: str = ""
    holder_associated: str = ""
    search_query: str = ""
    supporting_source_title: str = ""
    supporting_source_url: str = ""
    supporting_evidence_text: str = ""
    verification_status: str = VERIFY_UNVERIFIED
    extraction_notes: str = ""

    def identity_key(self) -> tuple:
        return (
            normalize_text(self.holder_associated),
            normalize_text(self.search_query),
            self.email.casefold() if self.email else "",
            self.phone,
            normalize_text(self.institution),
        )

    def to_dict(self) -> dict:
        return {
            "person_name": self.person_name,
            "email": self.email,
            "phone": self.phone,
            "institution": self.institution,
            "job_title": self.job_title,
            "location": self.location,
            "contact_type": self.contact_type,
            "excel_row": self.excel_row,
            "folio_number": self.folio_number,
            "holder_associated": self.holder_associated,
            "search_query": self.search_query,
            "supporting_source_title": self.supporting_source_title,
            "supporting_source_url": self.supporting_source_url,
            "supporting_evidence_text": self.supporting_evidence_text,
            "verification_status": self.verification_status,
            "extraction_notes": self.extraction_notes,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> AiContactLead:
        return cls(
            person_name=str(payload.get("person_name") or ""),
            email=str(payload.get("email") or ""),
            phone=str(payload.get("phone") or ""),
            institution=str(payload.get("institution") or ""),
            job_title=str(payload.get("job_title") or ""),
            location=str(payload.get("location") or ""),
            contact_type=str(payload.get("contact_type") or ""),
            excel_row=payload.get("excel_row"),
            folio_number=str(payload.get("folio_number") or ""),
            holder_associated=str(payload.get("holder_associated") or ""),
            search_query=str(payload.get("search_query") or ""),
            supporting_source_title=str(payload.get("supporting_source_title") or ""),
            supporting_source_url=str(payload.get("supporting_source_url") or ""),
            supporting_evidence_text=str(payload.get("supporting_evidence_text") or ""),
            verification_status=str(payload.get("verification_status") or VERIFY_UNVERIFIED),
            extraction_notes=str(payload.get("extraction_notes") or ""),
        )


@dataclass
class AiOverviewCapture:
    """Raw capture from one executed Google search."""

    status: str
    raw_text: str = ""
    sources: list[AiOverviewSource] = field(default_factory=list)
    extraction_notes: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "raw_text": self.raw_text,
            "sources": [item.to_dict() for item in self.sources],
            "extraction_notes": self.extraction_notes,
        }

    @classmethod
    def from_dict(cls, payload: dict | None) -> AiOverviewCapture | None:
        if not payload:
            return None
        capture = cls(
            status=str(payload.get("status") or STATUS_NOT_PRESENT),
            raw_text=str(payload.get("raw_text") or ""),
            sources=[
                AiOverviewSource.from_dict(item) for item in (payload.get("sources") or [])
            ],
            extraction_notes=str(payload.get("extraction_notes") or ""),
        )
        return revalidate_capture(capture)


def capture_from_playwright_payload(payload: dict | None) -> AiOverviewCapture:
    if not payload:
        return AiOverviewCapture(status=STATUS_CAPTURE_FAILED, extraction_notes="Empty JS payload.")
    try:
        state = str(payload.get("state") or "").lower()
        if not state:
            state = "present" if payload.get("present") else "not_present"
        if state == "not_present":
            return AiOverviewCapture(status=STATUS_NOT_PRESENT)
        if state == "unavailable":
            notes = str(payload.get("notes") or "").strip()
            return AiOverviewCapture(
                status=STATUS_UNAVAILABLE,
                extraction_notes=notes
                or "Google reported that an AI Overview is not available for this search.",
            )
        text = _clean_overview_visible_text(str(payload.get("text") or ""))
        sources = _normalize_sources(payload.get("sources") or [])
        notes = str(payload.get("notes") or "")
        if payload.get("truncated") and "truncated" not in notes.casefold():
            notes = (notes + " Overview may be truncated.").strip()
        return finalize_overview_content(text, sources, notes)
    except Exception as exc:
        return AiOverviewCapture(
            status=STATUS_CAPTURE_FAILED,
            extraction_notes=f"Overview parse error: {exc}",
        )


def parse_ai_overview_from_html(html: str) -> AiOverviewCapture:
    if not html or not html.strip():
        return AiOverviewCapture(
            status=STATUS_CAPTURE_FAILED,
            extraction_notes="Empty HTML document.",
        )
    lowered = html.casefold()
    if "unusual traffic" in lowered or "captcha-form" in lowered:
        return AiOverviewCapture(
            status=STATUS_BLOCKED,
            extraction_notes="Page appears blocked or challenged.",
        )
    root_html = _find_overview_root_html(html)
    if root_html is None:
        root_html = _find_jsname_overview_fragment(html)
    if root_html is None:
        return AiOverviewCapture(status=STATUS_NOT_PRESENT)
    visible = _normalize_overview_visible(_html_to_visible_text(root_html))
    if OVERVIEW_UNAVAILABLE_RE.search(visible):
        return AiOverviewCapture(
            status=STATUS_UNAVAILABLE,
            extraction_notes="Google reported that an AI Overview is not available for this search.",
        )
    text = _clean_overview_visible_text(visible)
    sources = _extract_sources_from_html(root_html)
    notes = ""
    if re.search(r"show\s+more", root_html, re.I):
        notes = "Overview may be truncated in the rendered page (Show more)."
    return finalize_overview_content(text, sources, notes)


def finalize_overview_content(
    text: str,
    sources: list[AiOverviewSource],
    notes: str = "",
) -> AiOverviewCapture:
    cleaned = _clean_overview_visible_text(text)
    if not cleaned:
        if OVERVIEW_UNAVAILABLE_RE.search(text):
            return AiOverviewCapture(
                status=STATUS_UNAVAILABLE,
                extraction_notes="Google reported that an AI Overview is not available for this search.",
            )
        return AiOverviewCapture(
            status=STATUS_CAPTURE_FAILED,
            extraction_notes="AI Overview container found but visible text was empty.",
        )
    if OVERVIEW_UNAVAILABLE_RE.search(cleaned):
        return AiOverviewCapture(
            status=STATUS_UNAVAILABLE,
            extraction_notes="Google reported that an AI Overview is not available for this search.",
        )
    if CSS_LEAK_RE.search(cleaned) or len(cleaned) < 20:
        return AiOverviewCapture(
            status=STATUS_CAPTURE_FAILED,
            extraction_notes="Overview text looked like page chrome or CSS, not summary content.",
        )
    if OVERVIEW_JUNK_BODY_RE.search(cleaned) and not re.search(
        r"[@\d]{3,}", cleaned
    ):
        return AiOverviewCapture(
            status=STATUS_CAPTURE_FAILED,
            extraction_notes="Overview text looked like page chrome, not summary content.",
        )
    filtered_sources = _normalize_sources([item.to_dict() for item in sources])
    if not filtered_sources and sources:
        notes = (notes + " No valid in-overview source links were found.").strip()
    return AiOverviewCapture(
        status=STATUS_PRESENT,
        raw_text=cleaned,
        sources=filtered_sources,
        extraction_notes=notes,
    )


def revalidate_capture(capture: AiOverviewCapture) -> AiOverviewCapture:
    if capture.status != STATUS_PRESENT:
        return capture
    revised = finalize_overview_content(
        capture.raw_text,
        capture.sources,
        capture.extraction_notes,
    )
    if revised.status != STATUS_PRESENT:
        revised.extraction_notes = (
            revised.extraction_notes or "Stored overview capture failed re-validation."
        )
    return revised


def revalidate_log_entry(entry: AiOverviewLogEntry) -> AiOverviewLogEntry:
    if entry.overview_status != STATUS_PRESENT:
        return entry
    capture = revalidate_capture(
        AiOverviewCapture(
            status=STATUS_PRESENT,
            raw_text=entry.raw_overview_text,
            sources=[
                AiOverviewSource(title=title, url=url)
                for title, url in zip(entry.source_titles, entry.source_urls)
            ],
            extraction_notes=entry.extraction_notes,
        )
    )
    if capture.status == entry.overview_status and capture.raw_text == entry.raw_overview_text:
        return entry
    titles, urls = _paired_sources(capture.sources)
    return AiOverviewLogEntry(
        excel_row=entry.excel_row,
        folio_number=entry.folio_number,
        primary_holder=entry.primary_holder,
        holder_associated=entry.holder_associated,
        query_strategy=entry.query_strategy,
        search_query=entry.search_query,
        capture_timestamp=entry.capture_timestamp,
        overview_status=capture.status,
        raw_overview_text=capture.raw_text if capture.status == STATUS_PRESENT else "",
        source_titles=titles,
        source_urls=urls,
        extraction_notes=capture.extraction_notes or entry.extraction_notes,
    )


def sanitize_stored_ai_evidence(
    log: list[AiOverviewLogEntry],
    leads: list[AiContactLead],
) -> tuple[list[AiOverviewLogEntry], list[AiContactLead]]:
    """Drop or downgrade legacy false-positive captures without re-running Google."""
    new_log = [revalidate_log_entry(entry) for entry in log]
    valid_queries = {
        normalize_text(entry.search_query)
        for entry in new_log
        if entry.overview_status == STATUS_PRESENT and entry.raw_overview_text.strip()
    }
    kept_leads = [
        lead
        for lead in leads
        if normalize_text(lead.search_query) in valid_queries
        and not _lead_looks_invalid(lead)
    ]
    return dedupe_log_entries(new_log), dedupe_contact_leads(kept_leads)


def build_log_entry(
    *,
    capture: AiOverviewCapture | None,
    excel_row: int | None,
    folio_number: str,
    primary_holder: str,
    holder_associated: str,
    query_strategy: str,
    search_query: str,
    capture_timestamp: str,
    blocked_message: str = "",
) -> AiOverviewLogEntry:
    if capture is None:
        status = STATUS_CAPTURE_FAILED
        notes = "No capture object returned from search client."
        raw_text = ""
        sources: list[AiOverviewSource] = []
    elif capture.status == STATUS_BLOCKED:
        status = STATUS_BLOCKED
        notes = capture.extraction_notes or blocked_message
        raw_text = ""
        sources = []
    else:
        capture = revalidate_capture(capture)
        status = capture.status
        notes = capture.extraction_notes
        raw_text = capture.raw_text if status == STATUS_PRESENT else ""
        sources = capture.sources if status == STATUS_PRESENT else []
    titles, urls = _paired_sources(sources)
    return AiOverviewLogEntry(
        excel_row=excel_row,
        folio_number=folio_number,
        primary_holder=primary_holder,
        holder_associated=holder_associated,
        query_strategy=query_strategy,
        search_query=search_query,
        capture_timestamp=capture_timestamp,
        overview_status=status,
        raw_overview_text=raw_text,
        source_titles=titles,
        source_urls=urls,
        extraction_notes=notes or blocked_message,
    )


def extract_contact_leads_from_overview(
    capture: AiOverviewCapture,
    *,
    excel_row: int | None,
    folio_number: str,
    holder_associated: str,
    search_query: str,
    expected_name: str = "",
) -> list[AiContactLead]:
    capture = revalidate_capture(capture)
    if capture.status != STATUS_PRESENT or not capture.raw_text:
        return []
    text = capture.raw_text
    person_name = expected_name.strip() or _guess_person_name(text)
    institution = _guess_institution(text)
    location = _guess_location(text)
    job_title = _guess_job_title(text)
    sources = capture.sources or []
    leads: list[AiContactLead] = []

    phones, emails = _extract_overview_contacts(text)
    for email in emails:
        line = _line_for_token(text, email)
        lead = AiContactLead(
            person_name=person_name,
            email=email,
            institution=institution,
            job_title=job_title,
            location=location,
            contact_type=_contact_type_from_context(line or text, kind="email"),
            excel_row=excel_row,
            folio_number=folio_number,
            holder_associated=holder_associated,
            search_query=search_query,
            supporting_evidence_text=_clip(line or email, 500),
            verification_status=VERIFY_UNVERIFIED,
            extraction_notes="AI Overview lead; not verified against original source.",
        )
        matched = _match_source_for_value(email, sources)
        if matched:
            lead.supporting_source_title = matched.title
            lead.supporting_source_url = matched.url
            lead.verification_status = VERIFY_SOURCE_LISTED
        else:
            lead.extraction_notes += " Direct supporting source URL not established."
        leads.append(lead)

    for phone in phones:
        line = _line_for_token(text, phone)
        contact_type = _contact_type_from_context(line or text, kind="phone")
        inst = _institution_from_line(line) or institution
        lead = AiContactLead(
            person_name=person_name,
            phone=phone,
            institution=inst,
            job_title=job_title,
            location=location,
            contact_type=contact_type,
            excel_row=excel_row,
            folio_number=folio_number,
            holder_associated=holder_associated,
            search_query=search_query,
            supporting_evidence_text=_clip(line or phone, 500),
            verification_status=VERIFY_UNVERIFIED,
            extraction_notes="AI Overview lead; not verified against original source.",
        )
        if contact_type == "institutional":
            lead.extraction_notes += " Classified as institutional from overview wording."
        matched = _match_source_for_value(phone, sources)
        if matched:
            lead.supporting_source_title = matched.title
            lead.supporting_source_url = matched.url
            lead.verification_status = VERIFY_SOURCE_LISTED
        else:
            lead.extraction_notes += " Direct supporting source URL not established."
        leads.append(lead)

    return dedupe_contact_leads(leads)


def dedupe_log_entries(entries: Iterable[AiOverviewLogEntry]) -> list[AiOverviewLogEntry]:
    seen: set[tuple] = set()
    output: list[AiOverviewLogEntry] = []
    for entry in entries:
        key = (entry.excel_row, normalize_text(entry.search_query))
        if key in seen:
            continue
        seen.add(key)
        output.append(entry)
    return output


def dedupe_contact_leads(leads: Iterable[AiContactLead]) -> list[AiContactLead]:
    merged: dict[tuple, AiContactLead] = {}
    for lead in leads:
        key = lead.identity_key()
        existing = merged.get(key)
        if existing is None:
            merged[key] = lead
            continue
        for url in (lead.supporting_source_url,):
            if url and url not in (existing.supporting_source_url or ""):
                if existing.supporting_source_url:
                    existing.extraction_notes += f" Also referenced: {url}"
                else:
                    existing.supporting_source_url = url
                    existing.supporting_source_title = lead.supporting_source_title
        if lead.supporting_evidence_text and lead.supporting_evidence_text not in (
            existing.supporting_evidence_text or ""
        ):
            existing.supporting_evidence_text = (
                f"{existing.supporting_evidence_text}\n{lead.supporting_evidence_text}".strip()
            )
    return list(merged.values())


def merge_overview_log(
    existing: list[AiOverviewLogEntry],
    new_entry: AiOverviewLogEntry,
) -> list[AiOverviewLogEntry]:
    query_key = normalize_text(new_entry.search_query)
    kept = [
        item
        for item in existing
        if normalize_text(item.search_query) != query_key
        or item.excel_row != new_entry.excel_row
    ]
    kept.append(new_entry)
    return dedupe_log_entries(kept)


def merge_contact_leads(
    existing: list[AiContactLead],
    new_leads: list[AiContactLead],
) -> list[AiContactLead]:
    valid = [lead for lead in new_leads if not _lead_looks_invalid(lead)]
    return dedupe_contact_leads([*existing, *valid])


def folio_from_headers_and_values(headers: list[str], values: list[object]) -> str:
    for header, value in zip(headers, values):
        key = _normalize_folio_header(header)
        if not key:
            continue
        text = "" if value is None else str(value).strip()
        if text:
            return text
    return ""


def _normalize_folio_header(header: str) -> bool:
    text = " ".join(str(header).replace("/", " ").replace("-", " ").casefold().split())
    return "folio" in text or text in {"cl id", "cl id no", "cl id folio no"}


def _lead_looks_invalid(lead: AiContactLead) -> bool:
    if lead.phone and re.sub(r"\D", "", lead.phone) in CSS_INTEGER_BLOCKLIST:
        return True
    if lead.institution.casefold() in INSTITUTION_STOPWORDS:
        return True
    if OVERVIEW_UNAVAILABLE_RE.search(lead.supporting_evidence_text or ""):
        return True
    if CSS_LEAK_RE.search(lead.supporting_evidence_text or ""):
        return True
    return False


def _extract_overview_contacts(text: str) -> tuple[list[str], list[str]]:
    clean = _clean_overview_visible_text(text)
    if not clean:
        return [], []
    phones, emails = extract_contacts(clean)
    out_phones = []
    for phone in phones:
        validated = _plausible_overview_phone(phone, clean)
        if validated:
            out_phones.append(validated)
    out_emails = []
    for email in emails:
        if email.casefold() in clean.casefold():
            out_emails.append(email)
    return out_phones, out_emails


def _plausible_overview_phone(raw: str, context: str) -> str | None:
    display = plausible_phone(raw)
    if not display:
        return None
    digits = re.sub(r"\D", "", display)
    if digits in CSS_INTEGER_BLOCKLIST:
        return None
    if CSS_LEAK_RE.search(context):
        return None
    window = _line_for_token(context, display) or context
    if not re.search(r"(?i)\b(phone|tel|mobile|fax|call|contact|\+)\b", window):
        if not display.strip().startswith("+") and not re.search(r"[\s().-]", display):
            return None
    return display


def _clean_overview_visible_text(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    cleaned = re.sub(r"\.\w+\s*\{[^}]*\}", " ", cleaned)
    cleaned = re.sub(r"var\s*\([^)]+\)", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"^\s*AI\s+Overview\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if OVERVIEW_UNAVAILABLE_RE.search(cleaned):
        return ""
    if CSS_LEAK_RE.search(cleaned):
        return ""
    return cleaned


def _paired_sources(sources: list[AiOverviewSource]) -> tuple[list[str], list[str]]:
    titles: list[str] = []
    urls: list[str] = []
    for item in sources:
        if not item.url and not item.title:
            continue
        titles.append(item.title)
        urls.append(item.url)
    return titles, urls


def _normalize_sources(raw_sources: list) -> list[AiOverviewSource]:
    output: list[AiOverviewSource] = []
    seen: set[str] = set()
    for item in raw_sources:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = _clean_overview_source_url(str(item.get("url") or ""))
        if not url:
            continue
        key = f"{url}\n{title}"
        if key in seen:
            continue
        seen.add(key)
        output.append(AiOverviewSource(title=title or _domain(url), url=url))
    return output


def _clean_overview_source_url(url: str) -> str:
    cleaned = unwrap_google_href(url) or url.strip()
    if not cleaned:
        return ""
    host = urlparse(cleaned).netloc.casefold()
    for blocked in GOOGLE_NON_SOURCE_HOST_SUFFIXES:
        if host == blocked or host.endswith("." + blocked):
            return ""
    if host.endswith(".google.com") and "/url" not in cleaned:
        return ""
    if is_valid_external_source_url(cleaned):
        return cleaned
    return ""


def _find_overview_root_html(html: str) -> str | None:
    parser = _OverviewHTMLParser()
    parser.feed(html)
    return parser.overview_html


def _html_search_regions(html: str) -> list[str]:
    regions: list[str] = []
    for marker in (
        'id="rso"',
        "id='rso'",
        'id="search"',
        "id='search'",
        'id="cnt"',
        "id='cnt'",
        'id="center_col"',
        "id='center_col'",
    ):
        idx = html.find(marker)
        if idx >= 0:
            regions.append(html[idx : idx + 1_200_000])
    regions.append(html)
    return regions


def _substantive_overview_slice(slice_text: str) -> bool:
    body = re.sub(r"^AI\s+Overview\s*", "", slice_text, flags=re.I).strip()
    if len(body) < 40:
        return False
    if _looks_like_overview_navigation_chip(slice_text):
        return False
    if CSS_LEAK_RE.search(slice_text):
        return False
    if OVERVIEW_JUNK_BODY_RE.search(body):
        return False
    return True


def _normalize_overview_visible(raw: str) -> str:
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return ""
    if not OVERVIEW_UNAVAILABLE_RE.search(text):
        return text
    matches = list(re.finditer(r"AI\s+Overview", text, re.I))
    for match in reversed(matches):
        slice_text = text[match.start() :].strip()
        if OVERVIEW_UNAVAILABLE_RE.search(slice_text):
            continue
        if _substantive_overview_slice(slice_text):
            return slice_text
    return text


def _looks_like_overview_navigation_chip(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip()
    if re.match(r"^AI\s+Overview\s+AI\s+Mode", normalized, re.I):
        return True
    head = normalized[:200]
    if re.search(r"AI\s+Mode", head, re.I) and re.search(r"Images", head, re.I):
        if re.search(r"Shopping", head, re.I):
            return True
    return False


def _is_valid_overview_visible(visible: str) -> bool:
    visible = _normalize_overview_visible(visible)
    if not visible or not visible.strip():
        return False
    if OVERVIEW_UNAVAILABLE_RE.search(visible):
        return False
    normalized = re.sub(r"\s+", " ", visible).strip()
    if not re.match(r"^AI\s+Overview\b", normalized, re.I):
        return False
    if _looks_like_overview_navigation_chip(normalized):
        return False
    if not _substantive_overview_slice(visible):
        return False
    body = re.sub(r"^AI\s+Overview\s*", "", visible, flags=re.I | re.M).strip()
    body = re.sub(r"\s+", " ", body)
    if len(body) < 20:
        return False
    if CSS_LEAK_RE.search(visible):
        return False
    return True


def _extract_bounded_div_html(html: str, start: int, max_len: int = 50_000) -> str:
    end = min(len(html), start + max_len)
    chunk = html[start:end]
    depth = 0
    i = 0
    while i < len(chunk):
        lower = chunk[i : i + 6].casefold()
        if lower.startswith("<div"):
            depth += 1
            close_tag = chunk.find(">", i)
            if close_tag < 0:
                break
            i = close_tag + 1
            continue
        if lower.startswith("</div"):
            depth -= 1
            close_tag = chunk.find(">", i)
            if close_tag < 0:
                break
            i = close_tag + 1
            if depth == 0:
                return chunk[:i]
            continue
        i += 1
    return chunk


def _jsname_div_open_pattern(jsname: str) -> re.Pattern[str]:
    return re.compile(
        rf'<div[^>]*\bjsname=["\']{jsname}["\'][^>]*>',
        re.I,
    )


def _find_jsname_overview_fragment(html: str) -> str | None:
    for region in _html_search_regions(html):
        for jsname in _JSNAME_OVERVIEW_MARKERS:
            pattern = _jsname_div_open_pattern(jsname)
            for match in pattern.finditer(region):
                fragment = _extract_bounded_div_html(region, match.start())
                visible = _html_to_visible_text(fragment)
                if _is_valid_overview_visible(visible):
                    return fragment
        heading_pattern = re.compile(
            r"<(?:span|div|h2|h3)[^>]*>\s*AI\s+Overview\s*</(?:span|div|h2|h3)>",
            re.I,
        )
        for match in heading_pattern.finditer(region):
            start = max(0, match.start() - 200)
            fragment = region[start : start + 30_000]
            visible = _html_to_visible_text(fragment)
            if _is_valid_overview_visible(visible):
                return fragment
    return None


class _OverviewHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.overview_html: str | None = None
        self._capture_depth = 0
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capture_depth > 0:
            self._capture_depth += 1
            self._buffer.append(self.get_starttag_text() or "")
            return
        attr_map = {key: (value or "") for key, value in attrs}
        if _is_overview_container_attrs(attr_map):
            self._start_capture()
            self._buffer.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag: str) -> None:
        if self._capture_depth > 0:
            self._buffer.append(f"</{tag}>")
            self._capture_depth -= 1
            if self._capture_depth == 0:
                self.overview_html = "".join(self._buffer)

    def handle_data(self, data: str) -> None:
        if self._capture_depth > 0:
            self._buffer.append(data)

    def _start_capture(self) -> None:
        if self._capture_depth == 0:
            self._buffer = []
        self._capture_depth = 1


def _is_overview_container_attrs(attr_map: dict[str, str]) -> bool:
    for key, value in OVERVIEW_CONTAINER_ATTRS:
        if attr_map.get(key) == value:
            return True
    return False


def _html_to_visible_text(fragment: str) -> str:
    without_style = re.sub(r"<style[^>]*>.*?</style>", " ", fragment, flags=re.I | re.S)
    without_script = re.sub(r"<script[^>]*>.*?</script>", " ", without_style, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", without_script, flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _extract_sources_from_html(fragment: str) -> list[AiOverviewSource]:
    sources: list[AiOverviewSource] = []
    seen: set[str] = set()
    for match in re.finditer(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', fragment, re.I | re.S
    ):
        href = _clean_overview_source_url(match.group(1))
        if not href:
            continue
        title = _html_to_visible_text(match.group(2))
        key = f"{href}\n{title}"
        if key in seen:
            continue
        seen.add(key)
        sources.append(AiOverviewSource(title=title or _domain(href), url=href))
    return sources


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return url


def _line_for_token(text: str, token: str) -> str:
    for line in text.splitlines():
        if token in line:
            return line.strip()
    if token in text:
        start = max(0, text.index(token) - 120)
        end = min(len(text), text.index(token) + len(token) + 120)
        return text[start:end].strip()
    return ""


def _contact_type_from_context(context: str, *, kind: str) -> str:
    if INSTITUTIONAL_CONTEXT.search(context):
        return "institutional"
    if PERSONAL_CONTEXT.search(context):
        return "personal"
    if kind == "phone" and re.search(r"(?i)\boffice\b", context):
        return "office"
    if kind == "email" and re.search(r"(?i)\b(academic|institutional)\s+email\b", context):
        return "institutional"
    return ""


def _institution_from_line(line: str) -> str:
    match = re.search(
        r"(?i)(?:institutional\s+phone|phone)\s*\(([^)]+)\)",
        line,
    )
    if match:
        value = match.group(1).strip()
        if value.casefold() not in INSTITUTION_STOPWORDS:
            return value
    match = re.search(r"(?i)from (?:the )?(.+?)(?:\s*\(|\.|,|$)", line)
    if match:
        value = match.group(1).strip()
        if value.casefold() not in INSTITUTION_STOPWORDS:
            return value
    return ""


def _guess_institution(text: str) -> str:
    for line in text.splitlines():
        value = _institution_from_line(line)
        if value:
            return value
    match = re.search(
        r"(?i)(Universiti[^.\n]+|University[^.\n]+|Centre for [^.\n]+|Center for [^.\n]+)",
        text,
    )
    if match:
        value = match.group(1).strip()
        if value.casefold() not in INSTITUTION_STOPWORDS:
            return value
    return ""


def _guess_location(text: str) -> str:
    match = re.search(r"(?i)\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,\s*[A-Z][a-z]+)\b", text)
    return match.group(1).strip() if match else ""


def _guess_job_title(text: str) -> str:
    match = re.search(r"(?i)\b(Professor|Dr\.|Director|Researcher|Scientist)\b[^.\n]{0,60}", text)
    return match.group(0).strip() if match else ""


def _guess_person_name(text: str) -> str:
    first_line = text.splitlines()[0] if text else ""
    match = re.match(r"^([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)+)", first_line)
    return match.group(1).strip() if match else ""


def _match_source_for_value(value: str, sources: list[AiOverviewSource]) -> AiOverviewSource | None:
    if not sources:
        return None
    for source in sources:
        if value and value in source.title:
            return source
    for source in sources:
        if value and value in (source.url or ""):
            return source
    return None


def _clip(text: str, limit: int) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."

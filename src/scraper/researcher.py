import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from ..matching import ADDRESS_REVIEW_THRESHOLD, ADDRESS_STRONG_THRESHOLD
from ..models import PersonRecord
from ..normalize import normalize_address, normalize_name, normalize_text
from ..postal import derive_postal_fragments
from ..query_planner import (
    FAMILY_HOLDER_ADDRESS,
    FAMILY_HOLDERS,
    FAMILY_OBITUARY,
    QUERY_VERIFIED_FAMILIES,
    build_query_plan,
    holder_name_for_plan,
)
from .ai_overview import (
    AiContactLead,
    AiOverviewCapture,
    AiOverviewLogEntry,
    STATUS_BLOCKED,
    STATUS_CAPTURE_FAILED,
    STATUS_NOT_PRESENT,
    STATUS_PRESENT,
    build_log_entry,
    extract_contact_leads_from_overview,
    merge_contact_leads,
    merge_overview_log,
    sanitize_stored_ai_evidence,
)
from .checkpoint import NullCheckpoint, RowCheckpoint
from .document_investigator import (
    NameEquivalence,
    ObituaryLead,
    ShareholderTarget,
    SourceDocument,
    SourceMatchLevel,
    extract_obituary_leads,
    investigate,
)
from .result_parser import dedupe_emails, dedupe_phones, usable_source_url
from .search_client import (
    PoliteSearchClient,
    SearchClient,
    SearchPolicy,
    SearchStatus,
)
from .source_fetcher import FetchResult, SourceFetcher, reject_empty_document
from .urls import canonical_url


logger = logging.getLogger("shareholder.research")

DEFAULT_SOURCE_INSPECTION_TOP_N = 3


class ResearchStatus(str, Enum):
    STRONG_MATCH = "STRONG_MATCH"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    NO_STRONG_MATCH = "NO_STRONG_MATCH"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


FINDING_STRONG = "STRONG_MATCH"
FINDING_REVIEW = "MANUAL_REVIEW"
ADDRESS_FIELDS = ("address1", "address2", "address3", "address4")


@dataclass
class QueryObservation:
    query: str
    strategy: str
    result_count: int
    accepted_candidate_count: int
    status: SearchStatus
    message: str = ""
    timestamp: str = ""
    result_urls: list[str] = field(default_factory=list)
    inspected_urls: list[str] = field(default_factory=list)
    accepted_source_urls: list[str] = field(default_factory=list)
    investigation_notes: list[str] = field(default_factory=list)
    # rank, url, title, snippet for every Google result returned.
    result_details: list[dict] = field(default_factory=list)


@dataclass
class StrongFinding:
    url: str
    title: str
    mobiles: list[str]
    emails: list[str]
    address1_similarity: float | None
    address2_similarity: float | None
    address3_similarity: float | None
    address4_similarity: float | None
    strategies: list[str]
    evidence_summary: str = ""
    page_number: int | None = None
    name_equivalence: str = ""
    match_basis: str = ""
    kind: str = FINDING_STRONG
    original_url: str = ""
    document_type: str = ""
    group_id: str = ""
    evidence_span: str = ""
    matched_address_fields: list[str] = field(default_factory=list)
    joint_holders: list[dict] = field(default_factory=list)
    reason: str = ""


# One retained source per URL; ``kind`` separates automatic matches from
# sources that only qualify for manual review.
SourceFinding = StrongFinding


@dataclass
class PersonResearch:
    record_id: str
    input_name: str
    status: ResearchStatus
    matched_name: str = ""
    mobiles: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    source_links: list[str] = field(default_factory=list)
    evidence: str = ""
    address1_match_pct: float | None = None
    address2_match_pct: float | None = None
    address3_match_pct: float | None = None
    address4_match_pct: float | None = None
    strategies_used: list[str] = field(default_factory=list)
    source_strategies: dict[str, list[str]] = field(default_factory=dict)
    log: list[QueryObservation] = field(default_factory=list)
    message: str = ""
    strong_findings: list[StrongFinding] = field(default_factory=list)
    review_findings: list[StrongFinding] = field(default_factory=list)
    obituary_leads: list[ObituaryLead] = field(default_factory=list)
    skipped_strategies: list[str] = field(default_factory=list)
    postal_fragments: list[str] = field(default_factory=list)
    manual_review_reason: str = ""
    # strategy, query, family, holder, reason for each planned search not run.
    unexecuted_plans: list[dict] = field(default_factory=list)
    ai_overview_log: list[AiOverviewLogEntry] = field(default_factory=list)
    ai_contact_leads: list[AiContactLead] = field(default_factory=list)


class ShareholderResearcher:
    """Sequential research: Google discovery, then source-document investigation."""

    def __init__(
        self,
        client: SearchClient,
        policy: SearchPolicy | None = None,
        address_threshold: float = ADDRESS_STRONG_THRESHOLD,
        checkpoint=None,
        source_fetcher: SourceFetcher | None = None,
        source_inspection_top_n: int | None = None,
        use_result_text_when_unfetched: bool = True,
    ) -> None:
        if isinstance(client, PoliteSearchClient) or getattr(client, "manages_browser", False):
            self.client = client
            self.policy = getattr(client, "policy", None) or policy or SearchPolicy()
        else:
            self.policy = policy or SearchPolicy()
            self.client = PoliteSearchClient(client, self.policy)
        self.address_threshold = address_threshold
        self.checkpoint = checkpoint or NullCheckpoint()
        self.source_fetcher = source_fetcher
        self.source_inspection_top_n = (
            source_inspection_top_n
            if source_inspection_top_n is not None
            else int(os.environ.get("SOURCE_INSPECTION_TOP_N", DEFAULT_SOURCE_INSPECTION_TOP_N))
        )
        self.use_result_text_when_unfetched = use_result_text_when_unfetched
        self._partial_state: dict[str, RowCheckpoint] = {}

    def research_people(self, people: list[PersonRecord]) -> list[PersonResearch]:
        return [self.research_person(person) for person in people]

    def research_person(self, person: PersonRecord) -> PersonResearch:
        try:
            return self._research_person(person)
        except Exception as exc:
            return PersonResearch(
                record_id=person.record_id,
                input_name=person.name,
                status=ResearchStatus.ERROR,
                evidence=f"Record failed: {exc}",
                message=str(exc),
            )

    def _research_person(self, person: PersonRecord) -> PersonResearch:
        saved = self.checkpoint.load(person.record_id)
        all_plans = build_query_plan(person)
        completed = set(saved.completed_strategies if saved else [])
        completed_queries = dict(saved.completed_queries if saved else {})
        is_done = _completion_check(completed, completed_queries)

        previous_outcome = saved.outcome if saved else None
        previous_log = (
            list(_outcome_from_dict(previous_outcome).log) if previous_outcome else []
        )
        if saved and saved.finished and previous_outcome:
            pending = _pending_verified(all_plans, is_done) + _pending_retries(
                all_plans, is_done, previous_log
            )
            if not pending:
                logger.info("[ROW] Completed from checkpoint %s", person.record_id)
                return _outcome_from_dict(previous_outcome)
            logger.info(
                "[ROW] Reopening %s for %d new or previously failed searches",
                person.record_id,
                len(pending),
            )
        # A reopened or resumed row keeps the log of the searches that succeeded;
        # failed attempts are retried and logged afresh.
        prior_log = [entry for entry in previous_log if entry.status == SearchStatus.OK]

        findings, seen_urls = _findings_from_saved(saved.findings if saved else [])
        review_findings, _ = _findings_from_saved(
            saved.review_findings if saved else [],
            default_kind=FINDING_REVIEW,
        )
        leads = [ObituaryLead.from_dict(item) for item in (saved.leads if saved else [])]
        prior_outcome = _outcome_from_dict(previous_outcome) if previous_outcome else None
        ai_overview_log = list(prior_outcome.ai_overview_log if prior_outcome else [])
        ai_contact_leads = list(prior_outcome.ai_contact_leads if prior_outcome else [])
        ai_overview_log, ai_contact_leads = sanitize_stored_ai_evidence(
            ai_overview_log, ai_contact_leads
        )
        if getattr(self.client, "circuit_open", False):
            message = (
                "Google cooldown is active. Remaining searches were not sent."
            )
            not_run = _unexecuted(
                [plan for plan in all_plans if not is_done(plan)],
                "search halted (cooldown)",
            )
            if findings or review_findings or leads:
                outcome = _assemble(
                    person,
                    findings,
                    prior_log,
                    list(completed),
                    review_findings=review_findings,
                    leads=leads,
                    unexecuted=not_run,
                    ai_overview_log=ai_overview_log,
                    ai_contact_leads=ai_contact_leads,
                )
                outcome.message = message
                outcome.evidence = outcome.evidence + "\n" + message
                return outcome
            return PersonResearch(
                record_id=person.record_id,
                input_name=person.name,
                status=ResearchStatus.BLOCKED,
                evidence=message,
                message=message,
                strategies_used=list(completed),
                log=prior_log,
                skipped_strategies=_skipped_text(not_run),
                unexecuted_plans=not_run,
                postal_fragments=_postal_descriptions(person),
            )

        log: list[QueryObservation] = list(prior_log)
        strategies: list[str] = list(saved.completed_strategies if saved else [])
        unexecuted: list[dict] = []
        person_failures = 0
        halted = False
        target = _target_from_person(person)
        used: dict[str, int] = {}
        for plan in all_plans:
            if is_done(plan):
                key = _budget_key(plan)
                used[key] = used.get(key, 0) + 1

        def skip(plans, reason: str) -> None:
            unexecuted.extend(_unexecuted(plans, reason))

        def current_outcome() -> PersonResearch:
            return _assemble(
                person,
                findings,
                log,
                strategies,
                review_findings=review_findings,
                leads=leads,
                unexecuted=unexecuted,
                ai_overview_log=ai_overview_log,
                ai_contact_leads=ai_contact_leads,
            )

        def checkpoint_state(finished: bool, outcome) -> RowCheckpoint:
            state = RowCheckpoint(
                finished=finished,
                completed_strategies=list(completed),
                findings=_serialize_findings(findings),
                outcome=outcome,
                review_findings=_serialize_findings(review_findings),
                leads=[lead.to_dict() for lead in leads],
                completed_queries=dict(completed_queries),
            )
            self._partial_state[person.record_id] = state
            return state

        for index, plan in enumerate(all_plans):
            if not plan.query.strip() or is_done(plan):
                continue
            if getattr(self.client, "circuit_open", False):
                halted = True
                skip([p for p in all_plans[index:] if not is_done(p)], "search halted")
                break
            if plan.family == FAMILY_OBITUARY and findings:
                skip([plan], "STRONG_MATCH already found")
                continue
            key = _budget_key(plan)
            limit = self._budget_limit(key)
            if used.get(key, 0) >= limit:
                skip([plan], _budget_reason(key, limit))
                continue
            used[key] = used.get(key, 0) + 1
            response = self.client.search(plan.query)
            capture_ts = datetime.now(timezone.utc).isoformat()
            self._record_ai_overview(
                person,
                plan,
                response,
                capture_ts,
                ai_overview_log,
                ai_contact_leads,
            )
            accepted = 0
            inspected: list[str] = []
            accepted_urls: list[str] = []
            notes: list[str] = []
            if response.status == SearchStatus.OK:
                person_failures = 0
                known_emails = {email.casefold() for finding in findings for email in finding.emails}
                known_phones = {phone for finding in findings for phone in finding.mobiles}
                top_results = list(response.results)[: self.source_inspection_top_n]
                for fallback_rank, result in enumerate(top_results, start=1):
                    serp_rank = getattr(result, "serp_index", None) or fallback_rank
                    count, note, url = self._inspect_google_result(
                        person,
                        target,
                        plan.strategy,
                        result,
                        findings,
                        seen_urls,
                        known_emails,
                        known_phones,
                        serp_rank=serp_rank,
                        review_findings=review_findings,
                        leads=leads,
                    )
                    if url:
                        inspected.append(url)
                    if note:
                        notes.append(note)
                    if count:
                        accepted += count
                        if url and url not in accepted_urls:
                            accepted_urls.append(url)
                strategies.append(plan.strategy)
                completed.add(plan.strategy)
                completed_queries[plan.strategy] = normalize_text(plan.query)
            else:
                person_failures += 1
                if response.status == SearchStatus.ERROR:
                    notes.append(f"GOOGLE_BROWSER_ERROR: {response.message}")
                elif response.status == SearchStatus.BLOCKED:
                    notes.append(f"BLOCKED: {response.message}")
            log.append(
                QueryObservation(
                    query=plan.query,
                    strategy=plan.strategy,
                    result_count=(
                        len(response.results)
                        if response.status == SearchStatus.OK
                        else 0
                    ),
                    accepted_candidate_count=accepted,
                    status=response.status,
                    message=response.message or "; ".join(notes),
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    result_urls=[result.url for result in response.results],
                    inspected_urls=inspected,
                    accepted_source_urls=accepted_urls,
                    investigation_notes=notes,
                    result_details=[
                        {
                            "rank": getattr(result, "serp_index", None) or rank,
                            "url": result.url,
                            "title": getattr(result, "title", "") or "",
                            "snippet": getattr(result, "snippet", "") or "",
                        }
                        for rank, result in enumerate(response.results, start=1)
                    ],
                )
            )
            # The partial outcome keeps this row's log if the run stops early.
            self.checkpoint.save(
                person.record_id,
                checkpoint_state(False, _outcome_to_dict(current_outcome())),
            )
            stop = (
                response.status != SearchStatus.OK
                and person_failures >= self.policy.max_consecutive_failures
            ) or response.halted
            if stop:
                halted = True
                skip(
                    [p for p in all_plans[index + 1 :] if not is_done(p)],
                    "search halted",
                )
                break

        outcome = current_outcome()
        self.checkpoint.save(
            person.record_id,
            checkpoint_state(not halted, _outcome_to_dict(outcome)),
        )
        self._partial_state.pop(person.record_id, None)
        logger.info("[ROW] Completed %s", person.record_id)
        return outcome

    def _record_ai_overview(
        self,
        person: PersonRecord,
        plan,
        response,
        capture_timestamp: str,
        ai_overview_log: list[AiOverviewLogEntry],
        ai_contact_leads: list[AiContactLead],
    ) -> None:
        """Capture AI Overview evidence without affecting identity matching."""
        try:
            raw = getattr(response, "ai_overview", None)
            capture = (
                raw
                if isinstance(raw, AiOverviewCapture)
                else AiOverviewCapture.from_dict(raw)
                if isinstance(raw, dict)
                else None
            )
            if response.status == SearchStatus.BLOCKED and capture is None:
                capture = AiOverviewCapture(
                    status=STATUS_BLOCKED,
                    extraction_notes=response.message or "Search blocked.",
                )
            elif response.status != SearchStatus.OK and capture is None:
                return
            if capture is None and response.status == SearchStatus.OK:
                capture = AiOverviewCapture(status=STATUS_NOT_PRESENT)
            holder = holder_name_for_plan(person, plan)
            entry = build_log_entry(
                capture=capture,
                excel_row=None,
                folio_number="",
                primary_holder=person.name.strip(),
                holder_associated=holder,
                query_strategy=plan.strategy,
                search_query=plan.query,
                capture_timestamp=capture_timestamp,
                blocked_message=response.message or "",
            )
            ai_overview_log[:] = merge_overview_log(ai_overview_log, entry)
            if capture and capture.status == STATUS_PRESENT:
                new_leads = extract_contact_leads_from_overview(
                    capture,
                    excel_row=None,
                    folio_number="",
                    holder_associated=holder,
                    search_query=plan.query,
                    expected_name=holder,
                )
                ai_contact_leads[:] = merge_contact_leads(ai_contact_leads, new_leads)
        except Exception as exc:
            logger.info("[AI_OVERVIEW] Capture failed for %s: %s", plan.strategy, exc)
            holder = holder_name_for_plan(person, plan)
            failed = build_log_entry(
                capture=AiOverviewCapture(
                    status=STATUS_CAPTURE_FAILED,
                    extraction_notes=str(exc),
                ),
                excel_row=None,
                folio_number="",
                primary_holder=person.name.strip(),
                holder_associated=holder,
                query_strategy=plan.strategy,
                search_query=plan.query,
                capture_timestamp=capture_timestamp,
            )
            ai_overview_log[:] = merge_overview_log(ai_overview_log, failed)

    def _budget_limit(self, key: str) -> int:
        if key == BUDGET_PRIMARY:
            return self.policy.max_searches_per_person
        if key == BUDGET_HOLDER_COMBINATIONS:
            return self.policy.max_holder_combination_searches
        return self.policy.max_searches_per_additional_holder

    def interrupted_outcome(self, person: PersonRecord) -> PersonResearch:
        """Outcome for a row the run stopped on (or never reached), listing
        every planned search that did not run. Nothing is marked completed
        unless the checkpoint shows it ran."""
        state = self._partial_state.get(person.record_id) or self.checkpoint.load(
            person.record_id
        )
        all_plans = build_query_plan(person)
        if state is None:
            completed, completed_queries, payload, finished = set(), {}, None, False
        else:
            completed = set(state.completed_strategies)
            completed_queries = dict(state.completed_queries)
            payload, finished = state.outcome, state.finished
        is_done = _completion_check(completed, completed_queries)
        outcome = (
            _outcome_from_dict(payload)
            if payload
            else PersonResearch(
                record_id=person.record_id,
                input_name=person.name,
                status=ResearchStatus.NO_STRONG_MATCH,
                postal_fragments=_postal_descriptions(person),
            )
        )
        if finished and payload:
            missing = _pending_verified(all_plans, is_done)
            if not missing:
                return outcome
            missing_names = {plan.strategy for plan in missing}
            kept = [
                item
                for item in outcome.unexecuted_plans
                if item.get("strategy") not in missing_names
            ]
        else:
            missing = [plan for plan in all_plans if not is_done(plan)]
            kept = []
        not_run = kept + _unexecuted(missing, "run interrupted before this search ran")
        outcome.unexecuted_plans = not_run
        outcome.skipped_strategies = _skipped_text(not_run)
        if outcome.status not in {ResearchStatus.STRONG_MATCH, ResearchStatus.MANUAL_REVIEW}:
            outcome.status = ResearchStatus.ERROR
        note = (
            f"INTERRUPTED: run stopped with {len(missing)} planned searches not executed; "
            "rerun to resume from the checkpoint."
        )
        outcome.message = note
        outcome.evidence = f"{outcome.evidence}\n{note}" if outcome.evidence else note
        return outcome

    def _inspect_google_result(
        self,
        person: PersonRecord,
        target: ShareholderTarget,
        strategy: str,
        result,
        findings: list[StrongFinding],
        seen_urls: set[str],
        known_emails: set[str],
        known_phones: set[str],
        serp_rank: int | None = None,
        review_findings: list[StrongFinding] | None = None,
        leads: list[ObituaryLead] | None = None,
    ) -> tuple[int, str, str]:
        url = canonical_url(getattr(result, "url", "") or "")
        if not url or not usable_source_url(url):
            return 0, "SOURCE_FETCH_ERROR: invalid source URL", ""

        fetch = self._fetch_source(
            url,
            getattr(result, "title", "") or "",
            result,
            serp_rank=serp_rank,
        )
        if not fetch.ok or fetch.document is None:
            code = fetch.error_code or "SOURCE_FETCH_ERROR"
            return 0, f"{code}: {fetch.error_message}", url

        original_url = url
        # Prefer the final destination URL after browser redirects (/goto → source).
        source_url = canonical_url(fetch.document.source_url) or url
        if source_url != fetch.document.source_url:
            fetch.document.source_url = source_url
        url = source_url

        lead_note = ""
        if leads is not None:
            new_leads = _merge_leads(
                leads,
                extract_obituary_leads(target, fetch.document),
                strategy,
                original_url,
            )
            if new_leads:
                lead_note = f"OBITUARY_LEAD: {url}"

        investigation = investigate(target, fetch.document)

        accepted_eval = None
        accepted_basis = ""
        for evaluation in investigation.evaluations:
            ok, basis = final_identity_accepts(
                evaluation,
                address_threshold=self.address_threshold,
            )
            if not ok:
                continue
            accepted_eval = evaluation
            accepted_basis = basis
            break

        if accepted_eval is None:
            review = None
            for evaluation in investigation.evaluations:
                ok, basis, reason = final_identity_review(
                    evaluation,
                    target,
                    address_threshold=self.address_threshold,
                )
                if ok:
                    review = (evaluation, basis, reason)
                    break
            if review is not None and review_findings is not None:
                evaluation, basis, reason = review
                if url not in seen_urls:
                    _record_review_finding(
                        review_findings,
                        evaluation,
                        fetch.document,
                        url=url,
                        original_url=original_url,
                        strategy=strategy,
                        basis=basis,
                        reason=reason,
                        address_threshold=self.address_threshold,
                    )
                note = f"MANUAL_REVIEW_CANDIDATE: {url} ({basis})"
            elif investigation.level == SourceMatchLevel.NOT_RELEVANT:
                note = "NO_DOCUMENT_MATCH: no same-record evidence in source"
            elif investigation.level == SourceMatchLevel.STRONG_MATCH:
                note = "NO_DOCUMENT_MATCH: document evidence did not meet final identity policy"
            else:
                note = "NO_DOCUMENT_MATCH: insufficient document evidence"
            if lead_note:
                note = f"{note}; {lead_note}"
            return 0, note, url

        mobiles = [
            item.value
            for item in investigation.contacts
            if item.type == "phone" and item.group_id == accepted_eval.group.group_id
        ]
        emails = [
            item.value
            for item in investigation.contacts
            if item.type == "email" and item.group_id == accepted_eval.group.group_id
        ]
        summary = _document_evidence_summary(
            accepted_eval,
            url,
            match_basis=accepted_basis,
        )
        logger.info(
            "[MATCH] Accepted source document basis=%s name=%s",
            accepted_basis,
            accepted_eval.name_equivalence.value,
        )
        if url in seen_urls:
            logger.info("[SOURCE] Existing URL merged")
            _merge_finding_document(
                findings,
                url,
                mobiles,
                emails,
                accepted_eval.address_scores,
                strategy,
                summary,
                accepted_eval.group.page_number,
                name_equivalence=accepted_eval.name_equivalence.value,
                match_basis=accepted_basis,
            )
        else:
            logger.info("[SOURCE] New URL discovered")
            seen_urls.add(url)
            if review_findings is not None:
                # A source promoted to STRONG_MATCH is no longer a review item.
                review_findings[:] = [item for item in review_findings if item.url != url]
            findings.append(
                StrongFinding(
                    url=url,
                    title=fetch.document.title or getattr(result, "title", "") or "",
                    mobiles=list(mobiles),
                    emails=list(emails),
                    address1_similarity=accepted_eval.address_scores.get("address1"),
                    address2_similarity=accepted_eval.address_scores.get("address2"),
                    address3_similarity=accepted_eval.address_scores.get("address3"),
                    address4_similarity=accepted_eval.address_scores.get("address4"),
                    strategies=[strategy],
                    evidence_summary=summary,
                    page_number=accepted_eval.group.page_number,
                    name_equivalence=accepted_eval.name_equivalence.value,
                    match_basis=accepted_basis,
                    kind=FINDING_STRONG,
                    original_url=original_url,
                    document_type=fetch.document.document_type or "",
                    group_id=accepted_eval.group.group_id,
                    evidence_span=_span(accepted_eval.group),
                    matched_address_fields=_fields_at_or_above(
                        accepted_eval.address_scores, self.address_threshold
                    ),
                    joint_holders=_joint_dicts(accepted_eval),
                )
            )
        for email in emails:
            if email.casefold() not in known_emails:
                known_emails.add(email.casefold())
                logger.info("[CONTACT] New email discovered")
        for phone in mobiles:
            if phone not in known_phones:
                known_phones.add(phone)
                logger.info("[CONTACT] New phone discovered")
        note = f"ACCEPTED_SOURCE: {url}"
        if lead_note:
            note = f"{note}; {lead_note}"
        return 1, note, url

    def _fetch_source(
        self,
        url: str,
        title: str,
        result,
        serp_rank: int | None = None,
    ) -> FetchResult:
        # Prefer the live headed Chrome session so /goto redirects resolve and
        # bot-blocked people-search pages can still be opened by the user profile.
        opener = getattr(self.client, "open_source", None)
        if callable(opener):
            try:
                browser_result = opener(url, title=title, serp_rank=serp_rank)
            except TypeError:
                browser_result = opener(url, title=title)
            except Exception as exc:
                browser_result = FetchResult(
                    ok=False,
                    error_code="SOURCE_FETCH_ERROR",
                    error_message=f"Browser source open failed: {exc}",
                )
            if browser_result.ok and browser_result.document is not None:
                empty = reject_empty_document(browser_result.document)
                if empty is not None:
                    browser_result = empty
                else:
                    if self.source_fetcher is not None:
                        try:
                            self.source_fetcher.store(
                                browser_result.document.source_url,
                                browser_result,
                            )
                            if url and url != browser_result.document.source_url:
                                self.source_fetcher.store(url, browser_result)
                        except Exception:
                            pass
                    return browser_result
            # Fall through to urllib fetcher / offline path.
            browser_error = browser_result
        else:
            browser_error = None

        if self.source_fetcher is not None:
            fetch = self.source_fetcher.fetch(url, title=title)
            if fetch.ok:
                return fetch
            if browser_error is not None and not browser_error.ok:
                return browser_error
            return fetch

        if not self.use_result_text_when_unfetched:
            return browser_error or FetchResult(
                ok=False,
                error_code="SOURCE_FETCH_ERROR",
                error_message="No source fetcher configured.",
            )
        # Offline/unit-test path: treat the Google result text as the source body.
        text = "\n".join(
            part
            for part in (getattr(result, "title", ""), getattr(result, "snippet", ""))
            if part
        )
        return FetchResult(
            ok=True,
            document=SourceDocument(
                source_url=url,
                title=title or getattr(result, "title", "") or url,
                document_type="text",
                text=text,
            ),
        )


def _target_from_person(person: PersonRecord) -> ShareholderTarget:
    return ShareholderTarget(
        name=person.name,
        address1=person.address1,
        address2=person.address2,
        address3=person.address3,
        address4=person.address4,
        pincode=person.pincode,
        country=person.country,
        scrip=person.scrip,
        joint1=person.joint1,
        joint2=person.joint2,
        record_id=person.record_id,
    )


def final_identity_accepts(
    evaluation,
    *,
    address_threshold: float = ADDRESS_STRONG_THRESHOLD,
) -> tuple[bool, str]:
    """Apply final identity policy to one same-record group evaluation.

    Rule A: EXACT name + >=1 strong address field
    Rule B: COMPATIBLE name + >=2 strong address fields from the same group
    """
    if evaluation is None or evaluation.level != SourceMatchLevel.STRONG_MATCH:
        return False, ""

    strong_fields = [
        field
        for field, score in (evaluation.address_scores or {}).items()
        if score is not None and score >= address_threshold
    ]
    name_eq = evaluation.name_equivalence

    if name_eq == NameEquivalence.EXACT and strong_fields:
        return True, "EXACT_NAME + ONE_STRONG_ADDRESS"
    if name_eq == NameEquivalence.COMPATIBLE and len(strong_fields) >= 2:
        return True, "COMPATIBLE_NAME + TWO_STRONG_ADDRESSES"
    return False, ""


def final_identity_review(
    evaluation,
    target: ShareholderTarget | None = None,
    *,
    address_threshold: float = ADDRESS_STRONG_THRESHOLD,
    review_threshold: float = ADDRESS_REVIEW_THRESHOLD,
) -> tuple[bool, str, str]:
    """Decide whether a non-accepted record group should be kept for MANUAL_REVIEW.

    Requires plausible identity: an EXACT or COMPATIBLE name for a multi-token
    target in the same record group, plus one of
      - EXACT name + a distinctive address field in [90, 95)
      - COMPATIBLE name + a distinctive address field in [90, 95), or exactly
        one distinctive field >= 95 (Rule B needs two)
      - a supplied joint holder co-occurring in the same record (corroboration)
    Never returns True for anything final_identity_accepts accepts.
    """
    if evaluation is None:
        return False, "", ""
    if final_identity_accepts(evaluation, address_threshold=address_threshold)[0]:
        return False, "", ""
    name_eq = evaluation.name_equivalence
    if name_eq not in {NameEquivalence.EXACT, NameEquivalence.COMPATIBLE}:
        return False, "", ""
    if target is not None and len(normalize_name(target.name).split()) < 2:
        return False, "", ""

    def distinctive(field_name: str) -> bool:
        if target is None:
            return True
        return is_distinctive_address(getattr(target, field_name, "") or "")

    scores = evaluation.address_scores or {}
    review_fields = [
        name
        for name, score in scores.items()
        if score is not None
        and review_threshold <= score < address_threshold
        and distinctive(name)
    ]
    strong_fields = [
        name
        for name, score in scores.items()
        if score is not None and score >= address_threshold and distinctive(name)
    ]
    joints = [item for item in (evaluation.joint_holders or []) if item.same_record]

    def describe(fields: list[str]) -> str:
        return ", ".join(f"{name} {format_similarity(scores[name])}%" for name in fields)

    if name_eq == NameEquivalence.EXACT and review_fields:
        basis = "EXACT_NAME + ADDRESS_90_TO_95"
        reason = (
            f"Exact name with {describe(review_fields)} in the same record; "
            f"below the {format_similarity(address_threshold)}% automatic threshold."
        )
    elif name_eq == NameEquivalence.COMPATIBLE and review_fields:
        basis = "COMPATIBLE_NAME + ADDRESS_90_TO_95"
        reason = (
            f"Compatible name with {describe(review_fields + strong_fields)} in the "
            f"same record; Rule B needs two address fields >= "
            f"{format_similarity(address_threshold)}%."
        )
    elif name_eq == NameEquivalence.COMPATIBLE and len(strong_fields) == 1:
        basis = "COMPATIBLE_NAME + ONE_STRONG_ADDRESS"
        reason = (
            f"Compatible name with only one strong address ({describe(strong_fields)}); "
            "Rule B needs two strong address fields in the same record."
        )
    elif joints:
        basis = f"{name_eq.value}_NAME + JOINT_HOLDER_SAME_RECORD"
        reason = (
            "Joint holder co-occurs with the shareholder name in the same record, "
            f"but no address field reached {format_similarity(review_threshold)}%."
        )
    else:
        return False, "", ""
    if joints:
        reason += " Joint holder corroboration: " + "; ".join(
            f"{item.field} {item.matched_text!r} ({item.classification.value})"
            for item in joints
        ) + "."
    return True, basis, reason


def is_distinctive_address(value: str) -> bool:
    """A single word such as a city or country cannot support review on its own."""
    tokens = [token for token in normalize_address(value).split() if len(token) >= 2]
    return len(tokens) >= 2


def _fields_at_or_above(scores: dict, threshold: float) -> list[str]:
    return [
        name
        for name in ADDRESS_FIELDS
        if (scores or {}).get(name) is not None and scores[name] >= threshold
    ]


def _span(group) -> str:
    span = f"{group.group_id} chars {group.start_position}-{group.end_position}"
    if group.page_number is not None:
        span += f" page {group.page_number}"
    return span


def _joint_dicts(evaluation) -> list[dict]:
    return [item.to_dict() for item in (evaluation.joint_holders or [])]


def _record_review_finding(
    review_findings: list[StrongFinding],
    evaluation,
    document: SourceDocument,
    *,
    url: str,
    original_url: str,
    strategy: str,
    basis: str,
    reason: str,
    address_threshold: float,
) -> None:
    for existing in review_findings:
        if existing.url == url:
            if strategy not in existing.strategies:
                existing.strategies.append(strategy)
            return
    scores = evaluation.address_scores or {}
    review_findings.append(
        StrongFinding(
            url=url,
            title=document.title or "",
            mobiles=[],
            emails=[],
            address1_similarity=scores.get("address1"),
            address2_similarity=scores.get("address2"),
            address3_similarity=scores.get("address3"),
            address4_similarity=scores.get("address4"),
            strategies=[strategy],
            evidence_summary=_document_evidence_summary(evaluation, url, match_basis=basis),
            page_number=evaluation.group.page_number,
            name_equivalence=evaluation.name_equivalence.value,
            match_basis=basis,
            kind=FINDING_REVIEW,
            original_url=original_url,
            document_type=document.document_type or "",
            group_id=evaluation.group.group_id,
            evidence_span=_span(evaluation.group),
            matched_address_fields=_fields_at_or_above(scores, ADDRESS_REVIEW_THRESHOLD),
            joint_holders=_joint_dicts(evaluation),
            reason=reason,
        )
    )


def _merge_leads(
    leads: list[ObituaryLead],
    new: list[ObituaryLead],
    strategy: str,
    original_url: str,
) -> list[ObituaryLead]:
    added = []
    for lead in new:
        key = (lead.source_url, " ".join(lead.excerpt.split()).casefold())
        existing = next(
            (
                item
                for item in leads
                if (item.source_url, " ".join(item.excerpt.split()).casefold()) == key
            ),
            None,
        )
        if existing is not None:
            if strategy not in existing.strategies:
                existing.strategies.append(strategy)
            continue
        lead.strategies = [strategy]
        lead.original_url = original_url
        leads.append(lead)
        added.append(lead)
    return added


BUDGET_PRIMARY = "PRIMARY"
BUDGET_HOLDER_COMBINATIONS = "HOLDER_COMBINATIONS"


def _budget_key(plan) -> str:
    if plan.family == FAMILY_HOLDERS:
        return BUDGET_HOLDER_COMBINATIONS
    if plan.family == FAMILY_HOLDER_ADDRESS:
        return f"HOLDER{plan.holder}"
    return BUDGET_PRIMARY


def _budget_reason(key: str, limit: int) -> str:
    if key == BUDGET_PRIMARY:
        return f"per-person search budget of {limit} reached"
    if key == BUDGET_HOLDER_COMBINATIONS:
        return f"holder-combination search budget of {limit} reached"
    return f"holder {key.removeprefix('HOLDER')} address search budget of {limit} reached"


def _completion_check(completed: set[str], completed_queries: dict[str, str]):
    """A plan is done only if its exact query ran. Checkpoints written before
    queries were recorded fall back to the strategy name, except for families
    introduced after them, whose labels must never match a stale entry."""

    def is_done(plan) -> bool:
        recorded = completed_queries.get(plan.strategy)
        if recorded is not None:
            return recorded == normalize_text(plan.query)
        if plan.family in QUERY_VERIFIED_FAMILIES:
            return False
        return plan.strategy in completed

    return is_done


def _pending_verified(plans, is_done) -> list:
    return [plan for plan in plans if plan.family in QUERY_VERIFIED_FAMILIES and not is_done(plan)]


def _pending_retries(plans, is_done, previous_log) -> list:
    """Plans whose earlier attempt failed (browser error, block) and never succeeded."""
    failed = {entry.strategy for entry in previous_log if entry.status != SearchStatus.OK}
    return [
        plan
        for plan in plans
        if plan.strategy in failed
        and plan.family not in QUERY_VERIFIED_FAMILIES
        and not is_done(plan)
    ]


def _unexecuted(plans, reason: str) -> list[dict]:
    return [
        {
            "strategy": plan.strategy,
            "query": plan.query,
            "family": plan.family,
            "holder": plan.holder,
            "reason": reason,
        }
        for plan in plans
        if plan.query.strip()
    ]


def _skipped_text(unexecuted: list[dict]) -> list[str]:
    return [f"{item['strategy']} (not executed: {item['reason']})" for item in unexecuted]


def _postal_descriptions(person: PersonRecord) -> list[str]:
    return [item.describe() for item in derive_postal_fragments(person)]


def _document_evidence_summary(
    evaluation,
    url: str,
    *,
    match_basis: str = "",
) -> str:
    group = evaluation.group
    context = " ".join((group.text or "").split())
    if len(context) > 280:
        context = context[:277] + "..."
    lines = [f"Source: {url}"]
    if match_basis:
        lines.append(f"match_basis: {match_basis}")
    lines.append(f"name_match: {evaluation.name_equivalence.value}")
    label = {
        "address1": "Address1",
        "address2": "Address2",
        "address3": "Address3",
        "address4": "Address4",
    }
    address_parts = []
    for field, score in evaluation.address_scores.items():
        if score is None:
            continue
        pretty = label.get(field, field)
        address_parts.append(f"{pretty}: {format_similarity(score)}%")
        lines.append(f"{pretty} similarity: {format_similarity(score)}%.")
    if address_parts:
        lines.append("address_matches: " + "; ".join(address_parts))
    if group.page_number is not None:
        lines.append(f"Page: {group.page_number}")
    if context:
        lines.append(f"Record context: {context}")
    return "\n".join(lines)


def _merge_finding_document(
    findings,
    url,
    mobiles,
    emails,
    address_scores,
    strategy: str,
    summary: str,
    page_number: int | None,
    *,
    name_equivalence: str = "",
    match_basis: str = "",
) -> None:
    for finding in findings:
        if finding.url != url:
            continue
        finding.mobiles.extend(mobiles)
        finding.emails.extend(emails)
        if strategy not in finding.strategies:
            finding.strategies.append(strategy)
        finding.address1_similarity = _max_score(
            finding.address1_similarity, address_scores.get("address1")
        )
        finding.address2_similarity = _max_score(
            finding.address2_similarity, address_scores.get("address2")
        )
        finding.address3_similarity = _max_score(
            finding.address3_similarity, address_scores.get("address3")
        )
        finding.address4_similarity = _max_score(
            finding.address4_similarity, address_scores.get("address4")
        )
        if name_equivalence and not finding.name_equivalence:
            finding.name_equivalence = name_equivalence
        if match_basis and not finding.match_basis:
            finding.match_basis = match_basis
        if summary and summary not in finding.evidence_summary:
            finding.evidence_summary = (
                f"{finding.evidence_summary}\n\n{summary}".strip()
            )
        if finding.page_number is None:
            finding.page_number = page_number
        return


def _format_strategies_used(log, fallback_strategies: list[str] | None = None) -> list[str]:
    used = []
    for entry in log or []:
        if entry.status != SearchStatus.OK:
            continue
        used.append(f"{entry.strategy} ({entry.result_count} results)")
    if used:
        return used
    return list(fallback_strategies or [])


def _assemble(
    person,
    findings,
    log,
    strategies,
    *,
    review_findings=None,
    leads=None,
    unexecuted=None,
    ai_overview_log=None,
    ai_contact_leads=None,
) -> PersonResearch:
    review_findings = [
        item
        for item in (review_findings or [])
        if item.url not in {finding.url for finding in findings}
    ]
    leads = list(leads or [])
    mobiles = dedupe_phones([value for finding in findings for value in finding.mobiles])
    emails = dedupe_emails([value for finding in findings for value in finding.emails])
    status = _status_for(findings, log, review_findings)
    review_reason = ""
    if findings:
        evidence = "\n\n".join(_evidence_block(finding) for finding in findings)
        message = ""
    elif status == ResearchStatus.MANUAL_REVIEW:
        review_reason = "\n".join(
            _unique_text(
                f"{finding.match_basis}: {finding.reason}" for finding in review_findings
            )
        )
        evidence = (
            "MANUAL_REVIEW: borderline evidence retained for human review; "
            "this is not a confirmed match.\n\n"
            + "\n\n".join(_evidence_block(finding) for finding in review_findings)
        )
        message = _investigation_message(log)
    elif status == ResearchStatus.BLOCKED:
        message = _failure_message(log) or (
            "Search provider blocked automated access."
        )
        evidence = message
    elif status == ResearchStatus.ERROR:
        message = _failure_message(log) or "Search failed."
        evidence = message
    else:
        message = _investigation_message(log)
        evidence = message or (
            "No source satisfied final identity policy "
            "(exact name + one strong address, or compatible name + two "
            "strong addresses in the same record) with a source URL."
        )
    scored = findings or review_findings
    return PersonResearch(
        record_id=person.record_id,
        input_name=person.name,
        status=status,
        matched_name=person.name.strip() if findings else "",
        mobiles=mobiles,
        emails=emails,
        source_links=[finding.url for finding in findings],
        evidence=evidence,
        address1_match_pct=_max_present(
            finding.address1_similarity for finding in scored
        ),
        address2_match_pct=_max_present(
            finding.address2_similarity for finding in scored
        ),
        address3_match_pct=_max_present(
            finding.address3_similarity for finding in scored
        ),
        address4_match_pct=_max_present(
            finding.address4_similarity for finding in scored
        ),
        strategies_used=_format_strategies_used(log, strategies),
        source_strategies={finding.url: list(finding.strategies) for finding in findings},
        log=log,
        message=message,
        strong_findings=list(findings),
        review_findings=review_findings,
        obituary_leads=leads,
        skipped_strategies=_skipped_text(unexecuted or []),
        unexecuted_plans=[dict(item) for item in unexecuted or []],
        postal_fragments=_postal_descriptions(person),
        manual_review_reason=review_reason,
        ai_overview_log=list(ai_overview_log or []),
        ai_contact_leads=list(ai_contact_leads or []),
    )


def _unique_text(values) -> list[str]:
    seen = set()
    out = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _status_for(findings, log, review_findings=None) -> ResearchStatus:
    if findings:
        return ResearchStatus.STRONG_MATCH
    if review_findings:
        return ResearchStatus.MANUAL_REVIEW
    if not log:
        return ResearchStatus.NO_STRONG_MATCH
    statuses = [entry.status for entry in log]
    if SearchStatus.OK in statuses:
        return ResearchStatus.NO_STRONG_MATCH
    if SearchStatus.BLOCKED in statuses:
        return ResearchStatus.BLOCKED
    if all(status == SearchStatus.ERROR for status in statuses):
        return ResearchStatus.ERROR
    return ResearchStatus.NO_STRONG_MATCH


def _failure_message(log) -> str:
    messages = []
    seen = set()
    for entry in log:
        if entry.status == SearchStatus.OK or not entry.message:
            continue
        if entry.message in seen:
            continue
        seen.add(entry.message)
        messages.append(entry.message)
    return "\n".join(messages)


def _investigation_message(log) -> str:
    notes = []
    seen = set()
    for entry in log:
        for note in entry.investigation_notes or []:
            if note in seen:
                continue
            seen.add(note)
            notes.append(note)
        if entry.message and entry.message not in seen:
            # Keep coded messages from fetch/investigation.
            if any(
                code in entry.message
                for code in (
                    "SOURCE_FETCH_ERROR",
                    "PDF_EXTRACTION_ERROR",
                    "NO_DOCUMENT_MATCH",
                    "GOOGLE_BROWSER_ERROR",
                    "BLOCKED",
                )
            ):
                seen.add(entry.message)
                notes.append(entry.message)
    return "\n".join(notes)


OUTCOME_CODES = (
    "ACCEPTED_SOURCE",
    "MANUAL_REVIEW_CANDIDATE",
    "OBITUARY_LEAD",
    "NO_DOCUMENT_MATCH",
    "SOURCE_FETCH_ERROR",
    "PDF_EXTRACTION_ERROR",
    "GOOGLE_BROWSER_ERROR",
    "BLOCKED",
)
_OUTCOME_CODE_PATTERN = re.compile(r"\b(" + "|".join(OUTCOME_CODES) + r")\b")


def research_status_summary(outcome: PersonResearch) -> str:
    """Executed-query and per-source outcome counts. Fetch/extraction failures
    are counted separately from NO_DOCUMENT_MATCH."""
    lines = [f"status={outcome.status.value}"]
    if outcome.log:
        statuses: dict[str, int] = {}
        for entry in outcome.log:
            statuses[entry.status.value] = statuses.get(entry.status.value, 0) + 1
        lines.append(
            f"queries executed={len(outcome.log)} ("
            + ", ".join(f"{key} {value}" for key, value in statuses.items())
            + ")"
        )
    counts: dict[str, int] = {}
    for entry in outcome.log:
        for note in entry.investigation_notes or []:
            for match in _OUTCOME_CODE_PATTERN.finditer(note):
                counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    for code in OUTCOME_CODES:
        if counts.get(code):
            lines.append(f"{code} x{counts[code]}")
    if outcome.status in {ResearchStatus.BLOCKED, ResearchStatus.ERROR} and outcome.message:
        lines.append(outcome.message.splitlines()[0][:300])
    return "\n".join(lines)


def _evidence_block(finding: StrongFinding) -> str:
    lines = []
    if finding.match_basis:
        lines.append(f"match_basis: {finding.match_basis}")
    lines.append(f"name_match: {finding.name_equivalence or 'EXACT'}")

    address_parts = []
    if finding.address1_similarity is not None:
        address_parts.append(
            f"Address1: {format_similarity(finding.address1_similarity)}%"
        )
        lines.append(
            f"Address 1 similarity: {format_similarity(finding.address1_similarity)}%."
        )
    if finding.address2_similarity is not None:
        address_parts.append(
            f"Address2: {format_similarity(finding.address2_similarity)}%"
        )
        lines.append(
            f"Address 2 similarity: {format_similarity(finding.address2_similarity)}%."
        )
    if finding.address3_similarity is not None:
        address_parts.append(
            f"Address3: {format_similarity(finding.address3_similarity)}%"
        )
        lines.append(
            f"Address 3 similarity: {format_similarity(finding.address3_similarity)}%."
        )
    if finding.address4_similarity is not None:
        address_parts.append(
            f"Address4: {format_similarity(finding.address4_similarity)}%"
        )
        lines.append(
            f"Address 4 similarity: {format_similarity(finding.address4_similarity)}%."
        )
    if address_parts:
        lines.append("address_matches: " + "; ".join(address_parts))
    if finding.strategies:
        lines.append("Strategies: " + ", ".join(finding.strategies))
    if finding.title:
        lines.append(f"Title: {finding.title}")
    lines.append(f"Source: {finding.url}")
    if finding.page_number is not None:
        lines.append(f"Page: {finding.page_number}")
    joint_line = format_joint_holders(finding.joint_holders)
    if joint_line:
        lines.append(f"Joint holders: {joint_line}")
    if finding.kind == FINDING_REVIEW and finding.reason:
        lines.append(f"Manual review reason: {finding.reason}")
    if finding.evidence_summary:
        # Keep a concise record-context line without duplicating address lines.
        for line in finding.evidence_summary.splitlines():
            if line.startswith("Record context:"):
                lines.append(line)
                break
    return "\n".join(lines)


def format_joint_holders(items: list[dict]) -> str:
    parts = []
    for item in items or []:
        label = f"{item.get('field')} {item.get('input_name')!r}"
        if item.get("same_record"):
            parts.append(
                f"{label}: {item.get('classification')} in same record "
                f"({item.get('matched_text')!r})"
            )
        elif item.get("matched_text"):
            parts.append(
                f"{label}: {item.get('classification')} nearby but not linked to the "
                "shareholder name"
            )
        else:
            parts.append(f"{label}: not found in this record")
    return "; ".join(parts)


def format_similarity(value: float) -> str:
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def _max_score(current: float | None, new: float | None) -> float | None:
    if new is None:
        return current
    if current is None:
        return new
    return max(current, new)


def _serialize_findings(findings: list[StrongFinding]) -> list[dict]:
    return [
        {
            "url": finding.url,
            "title": finding.title,
            "mobiles": list(finding.mobiles),
            "emails": list(finding.emails),
            "address1_similarity": finding.address1_similarity,
            "address2_similarity": finding.address2_similarity,
            "address3_similarity": finding.address3_similarity,
            "address4_similarity": finding.address4_similarity,
            "strategies": list(finding.strategies),
            "evidence_summary": finding.evidence_summary,
            "page_number": finding.page_number,
            "name_equivalence": finding.name_equivalence,
            "match_basis": finding.match_basis,
            "kind": finding.kind,
            "original_url": finding.original_url,
            "document_type": finding.document_type,
            "group_id": finding.group_id,
            "evidence_span": finding.evidence_span,
            "matched_address_fields": list(finding.matched_address_fields),
            "joint_holders": list(finding.joint_holders),
            "reason": finding.reason,
        }
        for finding in findings
    ]


def _findings_from_saved(
    items: list[dict],
    *,
    default_kind: str = FINDING_STRONG,
) -> tuple[list[StrongFinding], set[str]]:
    findings = []
    seen = set()
    for item in items:
        url = item.get("url") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        findings.append(
            StrongFinding(
                url=url,
                title=item.get("title") or "",
                mobiles=list(item.get("mobiles") or []),
                emails=list(item.get("emails") or []),
                address1_similarity=item.get("address1_similarity"),
                address2_similarity=item.get("address2_similarity"),
                address3_similarity=item.get("address3_similarity"),
                address4_similarity=item.get("address4_similarity"),
                strategies=list(item.get("strategies") or []),
                evidence_summary=item.get("evidence_summary") or "",
                page_number=item.get("page_number"),
                name_equivalence=item.get("name_equivalence") or "",
                match_basis=item.get("match_basis") or "",
                kind=item.get("kind") or default_kind,
                original_url=item.get("original_url") or "",
                document_type=item.get("document_type") or "",
                group_id=item.get("group_id") or "",
                evidence_span=item.get("evidence_span") or "",
                matched_address_fields=list(item.get("matched_address_fields") or []),
                joint_holders=list(item.get("joint_holders") or []),
                reason=item.get("reason") or "",
            )
        )
    return findings, seen


def _outcome_to_dict(outcome: PersonResearch) -> dict:
    return {
        "record_id": outcome.record_id,
        "input_name": outcome.input_name,
        "status": outcome.status.value,
        "matched_name": outcome.matched_name,
        "mobiles": outcome.mobiles,
        "emails": outcome.emails,
        "source_links": outcome.source_links,
        "evidence": outcome.evidence,
        "address1_match_pct": outcome.address1_match_pct,
        "address2_match_pct": outcome.address2_match_pct,
        "address3_match_pct": outcome.address3_match_pct,
        "address4_match_pct": outcome.address4_match_pct,
        "strategies_used": outcome.strategies_used,
        "source_strategies": outcome.source_strategies,
        "message": outcome.message,
        "strong_findings": _serialize_findings(outcome.strong_findings),
        "review_findings": _serialize_findings(outcome.review_findings),
        "obituary_leads": [lead.to_dict() for lead in outcome.obituary_leads],
        "skipped_strategies": list(outcome.skipped_strategies),
        "unexecuted_plans": [dict(item) for item in outcome.unexecuted_plans],
        "postal_fragments": list(outcome.postal_fragments),
        "manual_review_reason": outcome.manual_review_reason,
        "ai_overview_log": [entry.to_dict() for entry in outcome.ai_overview_log],
        "ai_contact_leads": [lead.to_dict() for lead in outcome.ai_contact_leads],
        "log": [
            {
                "query": entry.query,
                "strategy": entry.strategy,
                "result_count": entry.result_count,
                "accepted_candidate_count": entry.accepted_candidate_count,
                "status": entry.status.value,
                "message": entry.message,
                "timestamp": entry.timestamp,
                "result_urls": entry.result_urls,
                "inspected_urls": entry.inspected_urls,
                "accepted_source_urls": entry.accepted_source_urls,
                "investigation_notes": entry.investigation_notes,
                "result_details": entry.result_details,
            }
            for entry in outcome.log
        ],
    }


def _sanitize_ai_from_payload(payload: dict) -> tuple[list[AiOverviewLogEntry], list[AiContactLead]]:
    log = [
        AiOverviewLogEntry.from_dict(item) for item in payload.get("ai_overview_log") or []
    ]
    leads = [AiContactLead.from_dict(item) for item in payload.get("ai_contact_leads") or []]
    return sanitize_stored_ai_evidence(log, leads)


def _outcome_from_dict(payload: dict) -> PersonResearch:
    ai_log, ai_leads = _sanitize_ai_from_payload(payload)
    return PersonResearch(
        record_id=payload.get("record_id", ""),
        input_name=payload.get("input_name", ""),
        status=ResearchStatus(payload.get("status", ResearchStatus.NO_STRONG_MATCH.value)),
        matched_name=payload.get("matched_name", ""),
        mobiles=list(payload.get("mobiles") or []),
        emails=list(payload.get("emails") or []),
        source_links=list(payload.get("source_links") or []),
        evidence=payload.get("evidence", ""),
        address1_match_pct=payload.get("address1_match_pct"),
        address2_match_pct=payload.get("address2_match_pct"),
        address3_match_pct=payload.get("address3_match_pct"),
        address4_match_pct=payload.get("address4_match_pct"),
        strategies_used=list(payload.get("strategies_used") or []),
        source_strategies={
            url: list(strategies)
            for url, strategies in (payload.get("source_strategies") or {}).items()
        },
        message=payload.get("message", ""),
        strong_findings=_findings_from_saved(payload.get("strong_findings") or [])[0],
        review_findings=_findings_from_saved(
            payload.get("review_findings") or [],
            default_kind=FINDING_REVIEW,
        )[0],
        obituary_leads=[
            ObituaryLead.from_dict(item) for item in payload.get("obituary_leads") or []
        ],
        skipped_strategies=list(payload.get("skipped_strategies") or []),
        unexecuted_plans=[dict(item) for item in payload.get("unexecuted_plans") or []],
        postal_fragments=list(payload.get("postal_fragments") or []),
        manual_review_reason=payload.get("manual_review_reason", ""),
        ai_overview_log=ai_log,
        ai_contact_leads=ai_leads,
        log=[
            QueryObservation(
                query=entry.get("query", ""),
                strategy=entry.get("strategy", ""),
                result_count=entry.get("result_count", 0),
                accepted_candidate_count=entry.get("accepted_candidate_count", 0),
                status=SearchStatus(entry.get("status", SearchStatus.OK.value)),
                message=entry.get("message", ""),
                timestamp=entry.get("timestamp", ""),
                result_urls=list(entry.get("result_urls") or []),
                inspected_urls=list(entry.get("inspected_urls") or []),
                accepted_source_urls=list(entry.get("accepted_source_urls") or []),
                investigation_notes=list(entry.get("investigation_notes") or []),
                result_details=[dict(item) for item in entry.get("result_details") or []],
            )
            for entry in payload.get("log") or []
        ],
    )


def _max_present(values) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return max(present)

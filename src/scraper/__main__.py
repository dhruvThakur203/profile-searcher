import argparse
import logging
import sys
from dataclasses import replace

from .checkpoint import SqliteCheckpoint
from .excel_io import run_workbook
from .google_config import GoogleBrowserConfig
from .playwright_google_client import SyncPlaywrightGoogleSearchClient
from .researcher import PersonResearch
from .search_client import SearchPolicy
from .source_fetcher import SourceFetcher


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Research shareholder rows in headed Chrome on Google and write a new workbook. "
            "The input file is left unchanged."
        )
    )
    parser.add_argument("input", help="Input Excel workbook")
    parser.add_argument("output", nargs="?", help="Output Excel workbook")
    parser.add_argument("--test-row", type=int, default=None, help="Excel row number to research")
    parser.add_argument(
        "--test-rows",
        default=None,
        help="Comma-separated Excel row numbers to research (e.g. 2,3,4,5)",
    )
    parser.add_argument("--delay", type=float, default=None, help="Seconds between Google searches")
    parser.add_argument(
        "--max-searches",
        type=int,
        default=None,
        help="Searches per row for the primary holder (core, joint, postal, obituary)",
    )
    parser.add_argument(
        "--max-holder-address-searches",
        type=int,
        default=None,
        help="Name + address searches per joint holder, counted separately per holder",
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--profile-dir", default=None)
    return parser


def parse_test_rows(value: str | None) -> list[int] | None:
    if value is None or not str(value).strip():
        return None
    rows = []
    for part in str(value).split(","):
        part = part.strip()
        if not part:
            continue
        rows.append(int(part))
    if not rows:
        return None
    return rows


def create_google_client(config: GoogleBrowserConfig) -> SyncPlaywrightGoogleSearchClient:
    return SyncPlaywrightGoogleSearchClient(config)


def format_test_report(outcomes: list[PersonResearch]) -> str:
    blocks = []
    for outcome in outcomes:
        lines = [f"Row {outcome.record_id} — {outcome.input_name}"]
        for number, entry in enumerate(outcome.log, start=1):
            lines.append(f"Query {number}")
            lines.append(entry.strategy)
            lines.append(entry.query)
            lines.append("Google results:")
            if entry.result_urls:
                lines.extend(f"- {url}" for url in entry.result_urls)
            else:
                lines.append("- (none)")
            lines.append("Source URLs inspected:")
            if entry.inspected_urls:
                lines.extend(f"- {url}" for url in entry.inspected_urls)
            else:
                lines.append("- (none)")
            lines.append("Source URLs accepted:")
            if entry.accepted_source_urls:
                lines.extend(f"- {url}" for url in entry.accepted_source_urls)
            else:
                lines.append("- (none)")
            rejected = [
                note
                for note in (entry.investigation_notes or [])
                if note and not note.startswith("ACCEPTED_SOURCE:")
            ]
            lines.append("Source investigation notes:")
            if rejected:
                lines.extend(f"- {note}" for note in rejected)
            else:
                lines.append("- (none)")
            lines.append(f"Accepted candidates: {entry.accepted_candidate_count}")
            lines.append("")
        lines.append("Final aggregated sources")
        lines.extend(outcome.source_links or ["(none)"])
        lines.append("Final contacts")
        lines.append("Emails: " + (" | ".join(outcome.emails) or "(none)"))
        lines.append("Mobiles: " + (" | ".join(outcome.mobiles) or "(none)"))
        lines.append(f"Match status: {outcome.status.value}")
        if outcome.review_findings:
            lines.append("Manual review sources (not confirmed matches):")
            lines.extend(
                f"- {item.url} [{item.match_basis}] {item.reason}"
                for item in outcome.review_findings
            )
        if outcome.obituary_leads:
            lines.append("Obituary / condolence leads (not identity evidence):")
            lines.extend(
                f"- {lead.source_url} | names: {', '.join(lead.names)} | "
                f"locations: {', '.join(lead.locations)} | companies: {', '.join(lead.companies)}"
                for lead in outcome.obituary_leads
            )
        if outcome.unexecuted_plans:
            lines.append("Planned strategies not executed:")
            lines.extend(
                f"- {item['strategy']}: {item['query']} ({item['reason']})"
                for item in outcome.unexecuted_plans
            )
        elif outcome.skipped_strategies:
            lines.append("Planned strategies not executed:")
            lines.extend(f"- {item}" for item in outcome.skipped_strategies)
        if outcome.message:
            lines.append(f"Message: {outcome.message}")
        if outcome.evidence:
            lines.append("Evidence:")
            lines.append(outcome.evidence)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    test_rows = parse_test_rows(args.test_rows)
    if args.output is None and args.test_row is None and test_rows is None:
        parser.error("An output workbook is required unless --test-row/--test-rows is set.")

    config = GoogleBrowserConfig.from_env()
    if args.headless:
        config = replace(config, headless=True)
    if args.profile_dir:
        config = replace(config, profile_dir=args.profile_dir)
    if args.delay is not None:
        config = replace(
            config,
            timing=replace(
                config.timing,
                between_searches_min=args.delay,
                between_searches_max=args.delay,
            ),
        )
    defaults = SearchPolicy()
    policy = SearchPolicy(
        delay_seconds=0,
        max_searches_per_person=args.max_searches or defaults.max_searches_per_person,
        max_searches_per_additional_holder=(
            args.max_holder_address_searches
            if args.max_holder_address_searches is not None
            else defaults.max_searches_per_additional_holder
        ),
    )
    client = create_google_client(config)
    checkpoint = SqliteCheckpoint("data/research_checkpoint.sqlite")
    source_fetcher = SourceFetcher.from_env()
    try:
        outcomes = run_workbook(
            args.input,
            args.output,
            client,
            policy,
            checkpoint=checkpoint,
            excel_row=args.test_row if test_rows is None else None,
            excel_rows=test_rows,
            source_fetcher=source_fetcher,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "Interrupted. Unexecuted searches are listed as NOT_EXECUTED in the "
            "Research Log; rerun to resume from the checkpoint.",
            file=sys.stderr,
        )
        return 130
    finally:
        client.close()
    if args.test_row is not None or test_rows is not None:
        print(format_test_report(outcomes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

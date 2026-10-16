import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class RowCheckpoint:
    finished: bool
    completed_strategies: list[str]
    findings: list[dict]
    outcome: dict | None = None
    review_findings: list[dict] = field(default_factory=list)
    leads: list[dict] = field(default_factory=list)
    # strategy -> normalized query text actually executed for it.
    completed_queries: dict[str, str] = field(default_factory=dict)


class NullCheckpoint:
    def load(self, record_id: str) -> RowCheckpoint | None:
        return None

    def save(self, record_id: str, checkpoint: RowCheckpoint) -> None:
        return None


class SqliteCheckpoint:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS row_progress (
                    record_id TEXT PRIMARY KEY,
                    finished INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def load(self, record_id: str) -> RowCheckpoint | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload FROM row_progress WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return RowCheckpoint(
            finished=bool(payload.get("finished")),
            completed_strategies=list(payload.get("completed_strategies") or []),
            findings=list(payload.get("findings") or []),
            outcome=payload.get("outcome"),
            review_findings=list(payload.get("review_findings") or []),
            leads=list(payload.get("leads") or []),
            completed_queries=dict(payload.get("completed_queries") or {}),
        )

    def save(self, record_id: str, checkpoint: RowCheckpoint) -> None:
        payload = {
            "finished": checkpoint.finished,
            "completed_strategies": checkpoint.completed_strategies,
            "findings": checkpoint.findings,
            "outcome": checkpoint.outcome,
            "review_findings": checkpoint.review_findings,
            "leads": checkpoint.leads,
            "completed_queries": checkpoint.completed_queries,
        }
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO row_progress (record_id, finished, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    finished = excluded.finished,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    record_id,
                    1 if checkpoint.finished else 0,
                    json.dumps(payload),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

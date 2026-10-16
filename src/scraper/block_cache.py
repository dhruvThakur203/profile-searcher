import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


class GoogleBlockCache:
    """Remember a Google block so later searches wait out the cooldown."""

    def __init__(self, path: str | Path, cooldown_seconds: float, clock=None) -> None:
        self.path = Path(path)
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def is_blocked(self) -> bool:
        payload = self._read()
        if payload is None:
            return False
        until = _parse_time(payload.get("blocked_until", ""))
        if until is None:
            return False
        return self._clock() < until

    def reason(self) -> str:
        payload = self._read() or {}
        return str(payload.get("reason") or "")

    def blocked_until(self) -> str:
        payload = self._read() or {}
        return str(payload.get("blocked_until") or "")

    def record_block(self, reason: str) -> None:
        until = self._clock() + timedelta(seconds=self.cooldown_seconds)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "blocked_until": until.isoformat(),
            "reason": reason,
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def _read(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed

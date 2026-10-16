import os
from dataclasses import dataclass
from pathlib import Path

from .timing import SearchTiming


ACCEPT_LANGUAGE = "en-IN,en;q=0.9"
CHROME_LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-sandbox",
)
WEBDRIVER_INIT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class GoogleBrowserConfig:
    home_url: str = "https://www.google.com"
    profile_dir: str = "data/browser_profile"
    headless: bool = False
    locale: str = "en-IN"
    accept_language: str = ACCEPT_LANGUAGE
    viewport_width: int = 1366
    viewport_height: int = 900
    debug: bool = False
    diagnostics_dir: str = "data/diagnostics"
    block_path: str = "data/google_block.json"
    channel: str = "chrome"
    timing: SearchTiming = SearchTiming()

    @classmethod
    def from_env(cls) -> "GoogleBrowserConfig":
        timing = SearchTiming(
            pre_search_min=_env_float("GOOGLE_PRE_SEARCH_MIN", 0.5),
            pre_search_max=_env_float("GOOGLE_PRE_SEARCH_MAX", 2.5),
            typing_min_ms=_env_int("GOOGLE_TYPING_MIN_MS", 80),
            typing_max_ms=_env_int("GOOGLE_TYPING_MAX_MS", 250),
            typing_pause_probability=_env_float("GOOGLE_TYPING_PAUSE_PROBABILITY", 0.08),
            typing_pause_min=_env_float("GOOGLE_TYPING_PAUSE_MIN", 0.15),
            typing_pause_max=_env_float("GOOGLE_TYPING_PAUSE_MAX", 0.45),
            post_submit_min=_env_float("GOOGLE_POST_SUBMIT_MIN", 2.0),
            post_submit_max=_env_float("GOOGLE_POST_SUBMIT_MAX", 8.0),
            between_searches_min=_env_float("GOOGLE_BETWEEN_SEARCHES_MIN", 15.0),
            between_searches_max=_env_float("GOOGLE_BETWEEN_SEARCHES_MAX", 45.0),
            cooldown_after_searches=_env_int("GOOGLE_COOLDOWN_AFTER", 5),
            cooldown_min=_env_float("GOOGLE_COOLDOWN_MIN", 60.0),
            cooldown_max=_env_float("GOOGLE_COOLDOWN_MAX", 120.0),
            reading_min=_env_float("GOOGLE_READING_MIN", 3.0),
            reading_max=_env_float("GOOGLE_READING_MAX", 10.0),
            block_cooldown_seconds=_env_float("GOOGLE_BLOCK_COOLDOWN_SECONDS", 30 * 60),
        )
        return cls(
            home_url=os.environ.get("GOOGLE_HOME_URL", "https://www.google.com").strip()
            or "https://www.google.com",
            profile_dir=os.environ.get("BROWSER_PROFILE_DIR", "data/browser_profile"),
            headless=_env_flag("HEADLESS", False),
            debug=_env_flag("GOOGLE_DEBUG", False),
            timing=timing,
        )

    def profile_path(self) -> Path:
        return Path(self.profile_dir)

    def block_file(self) -> Path:
        return Path(self.block_path)

    def diagnostics_path(self) -> Path:
        return Path(self.diagnostics_dir)

    def persistent_launch_kwargs(self) -> dict:
        """Exact kwargs for launch_persistent_context. Chrome only; no Chromium fallback."""
        return {
            "user_data_dir": str(self.profile_path()),
            "channel": self.channel,
            "headless": self.headless,
            "viewport": {
                "width": self.viewport_width,
                "height": self.viewport_height,
            },
            "locale": self.locale,
            "args": list(CHROME_LAUNCH_ARGS),
            "extra_http_headers": {"Accept-Language": self.accept_language},
        }

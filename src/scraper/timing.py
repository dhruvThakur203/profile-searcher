import random
from dataclasses import dataclass


@dataclass(frozen=True)
class SearchTiming:
    """Configurable randomized pauses for the headed Google browser flow."""

    pre_search_min: float = 0.5
    pre_search_max: float = 2.5
    typing_min_ms: int = 80
    typing_max_ms: int = 250
    typing_pause_probability: float = 0.08
    typing_pause_min: float = 0.15
    typing_pause_max: float = 0.45
    post_submit_min: float = 2.0
    post_submit_max: float = 8.0
    between_searches_min: float = 15.0
    between_searches_max: float = 45.0
    cooldown_after_searches: int = 5
    cooldown_min: float = 60.0
    cooldown_max: float = 120.0
    reading_min: float = 3.0
    reading_max: float = 10.0
    block_cooldown_seconds: float = 30 * 60

    def __post_init__(self) -> None:
        pairs = (
            (self.pre_search_min, self.pre_search_max, "pre_search"),
            (self.post_submit_min, self.post_submit_max, "post_submit"),
            (self.between_searches_min, self.between_searches_max, "between_searches"),
            (self.cooldown_min, self.cooldown_max, "cooldown"),
            (self.reading_min, self.reading_max, "reading"),
            (self.typing_pause_min, self.typing_pause_max, "typing_pause"),
        )
        for low, high, label in pairs:
            if low < 0 or high < 0:
                raise ValueError(f"{label} delays cannot be negative")
            if low > high:
                raise ValueError(f"{label} min cannot exceed max")
        if self.typing_min_ms < 0 or self.typing_max_ms < 0:
            raise ValueError("typing delays cannot be negative")
        if self.typing_min_ms > self.typing_max_ms:
            raise ValueError("typing_min_ms cannot exceed typing_max_ms")
        if not 0 <= self.typing_pause_probability <= 1:
            raise ValueError("typing_pause_probability must be between 0 and 1")
        if self.cooldown_after_searches < 1:
            raise ValueError("cooldown_after_searches must be at least 1")
        if self.block_cooldown_seconds < 0:
            raise ValueError("block_cooldown_seconds cannot be negative")

    @classmethod
    def instant(cls, **overrides) -> "SearchTiming":
        """Zero delays for offline unit tests. Pass overrides for specific ranges."""
        values = {
            "pre_search_min": 0.0,
            "pre_search_max": 0.0,
            "typing_min_ms": 0,
            "typing_max_ms": 0,
            "typing_pause_probability": 0.0,
            "typing_pause_min": 0.0,
            "typing_pause_max": 0.0,
            "post_submit_min": 0.0,
            "post_submit_max": 0.0,
            "between_searches_min": 0.0,
            "between_searches_max": 0.0,
            "cooldown_after_searches": 5,
            "cooldown_min": 0.0,
            "cooldown_max": 0.0,
            "reading_min": 0.0,
            "reading_max": 0.0,
            "block_cooldown_seconds": 30 * 60,
        }
        values.update(overrides)
        return cls(**values)

    def sample_pre_search(self, rng: random.Random | None = None) -> float:
        return self._uniform(self.pre_search_min, self.pre_search_max, rng)

    def sample_post_submit(self, rng: random.Random | None = None) -> float:
        return self._uniform(self.post_submit_min, self.post_submit_max, rng)

    def sample_between_searches(self, rng: random.Random | None = None) -> float:
        return self._uniform(self.between_searches_min, self.between_searches_max, rng)

    def sample_cooldown(self, rng: random.Random | None = None) -> float:
        return self._uniform(self.cooldown_min, self.cooldown_max, rng)

    def sample_reading(self, rng: random.Random | None = None) -> float:
        return self._uniform(self.reading_min, self.reading_max, rng)

    def sample_typing_delay_ms(self, rng: random.Random | None = None) -> int:
        source = rng or random
        return source.randint(self.typing_min_ms, self.typing_max_ms)

    def sample_typing_pause(self, rng: random.Random | None = None) -> float | None:
        source = rng or random
        if source.random() >= self.typing_pause_probability:
            return None
        return self._uniform(self.typing_pause_min, self.typing_pause_max, source)

    @staticmethod
    def _uniform(low: float, high: float, rng: random.Random | None) -> float:
        if low == high:
            return float(low)
        source = rng or random
        return source.uniform(low, high)

"""Time is injected everywhere it matters so tests never sleep."""
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)


class ManualClock:
    """A clock that only moves when told to (reuse window, reaper, lock expiry)."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)

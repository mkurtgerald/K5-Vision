"""Bounded host-clock observations, without synchronization or clock writes."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

MAX_CLOCK_STEP_SECONDS = 315_576_000
MAX_CLOCK_CHANGE_COUNT = 1_000_000
_STEP_THRESHOLD_SECONDS = 2.0


class HostClockObserver:
    """Compare consecutive UTC/monotonic samples and latch detected clock steps.

    A steady clock does not establish NTP synchronization. Only the last sample
    and bounded change evidence are retained, for this process session alone.
    """

    def __init__(
        self,
        *,
        utc_now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._utc_now = utc_now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._previous: tuple[datetime, float] | None = None
        self._change_count = 0
        self._last_step_seconds = 0.0
        self._last_change_at: str | None = None
        self.observe()

    def observe(self) -> None:
        """Sample without starting a timer, performing I/O, or changing clocks."""
        self.snapshot()

    def snapshot(self) -> dict:
        """Return current observed host UTC and safe, bounded change evidence."""
        with self._lock:
            server_utc = None
            available = False
            try:
                wall = self._utc_now()
                if not isinstance(wall, datetime) or wall.utcoffset() is None:
                    raise ValueError
                wall = wall.astimezone(UTC)
                server_utc = wall.isoformat(timespec="milliseconds").replace("+00:00", "Z")
                monotonic = self._monotonic()
                if type(monotonic) not in (int, float) or not math.isfinite(monotonic):
                    raise ValueError
                if self._previous is not None:
                    previous_wall, previous_monotonic = self._previous
                    elapsed = monotonic - previous_monotonic
                    if elapsed < 0:
                        raise ValueError
                    step = (wall - previous_wall).total_seconds() - elapsed
                    if not math.isfinite(step):
                        raise ValueError
                    if abs(step) >= _STEP_THRESHOLD_SECONDS:
                        self._last_step_seconds = round(
                            max(-MAX_CLOCK_STEP_SECONDS, min(MAX_CLOCK_STEP_SECONDS, step)), 6
                        )
                        self._change_count = min(MAX_CLOCK_CHANGE_COUNT, self._change_count + 1)
                        self._last_change_at = server_utc
                self._previous = wall, monotonic
                available = True
            except Exception:
                # A failed observation cannot interrupt recording or invent time.
                # Re-establish a baseline after a gap; retain earlier step evidence.
                self._previous = None
            return {
                "server_utc": server_utc,
                "clock": {
                    "status": "unavailable"
                    if not available
                    else "changed"
                    if self._change_count
                    else "observing",
                    "last_step_seconds": self._last_step_seconds,
                    "change_count": self._change_count,
                    "last_change_at": self._last_change_at,
                    "sync_verified": False,
                },
            }

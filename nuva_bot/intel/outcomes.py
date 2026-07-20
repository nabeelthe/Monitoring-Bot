"""Outcome Tracker — the bot keeps score on itself.

Every meaningful signal is stamped; later we measure what the HASH price
actually did 1h and 24h afterward. The resulting hit-rate table per signal
kind ("large mints preceded a +2% day 8 of 11 times") feeds the decision
engine and /predict — evidence instead of vibes. Signals with no price data
around them are marked measured-with-NULL and excluded from statistics.
"""

import logging
import time

from .events import Event, EventStore
from .scoring import SIGNAL_TAGS

log = logging.getLogger("nuva.outcomes")

GIVE_UP_AFTER = 48 * 3600  # if the tape has no price near the horizon by then, mark NULL


def signal_kind(ev: Event) -> str:
    """The primary signal tag of an event, falling back to its layer."""
    for t in ev.tags:
        if t in SIGNAL_TAGS:
            return t
    return ev.layer


class OutcomeTracker:
    def __init__(self, store: EventStore, positive_pct: float = 2.0):
        self.store = store
        self.positive_pct = positive_pct

    def record(self, ev: Event):
        """Stamp a signal for later measurement (critical/high/medium only —
        the ones we'd act on)."""
        if ev.priority in ("critical", "high", "medium"):
            self.store.outcome_record(ev.id, ev.ts, signal_kind(ev))

    def _measure(self, base_ts: float, horizon_s: float) -> float | None:
        p0 = self.store.price_near(base_ts)
        p1 = self.store.price_near(base_ts + horizon_s)
        if p0 and p1 and p0 > 0:
            return (p1 - p0) / p0 * 100.0
        return None

    def annotate(self, now: float | None = None) -> int:
        """Fill in forward returns for every signal whose horizon has passed.
        Returns how many measurements were written."""
        now = now or time.time()
        written = 0
        for horizon, seconds in (("1h", 3600.0), ("24h", 86400.0)):
            for event_id, ts in self.store.outcomes_pending(horizon, now - seconds):
                ret = self._measure(ts, seconds)
                if ret is None and now - (ts + seconds) < GIVE_UP_AFTER:
                    continue  # tape may still fill in; try again next cycle
                self.store.outcome_set(event_id, horizon, ret)
                written += 1
        return written

    def hit_rates(self, min_n: int = 3) -> dict:
        return self.store.hit_rates(min_n=min_n, positive_pct=self.positive_pct)

    def evidence_line(self, kind: str) -> str | None:
        """One plain sentence of historical evidence for a signal kind, or None."""
        stats = self.hit_rates().get(kind)
        if not stats:
            return None
        pct = int(stats["up_rate"] * 100)
        return (f"History check: {kind} signals like this were followed by a "
                f"+{self.positive_pct:g}% (or better) day {pct}% of the time "
                f"({stats['n']} measured cases, avg 24h move {stats['avg_24h']:+.1f}%).")

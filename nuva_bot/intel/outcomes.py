"""Outcome Tracker — the bot keeps score on itself.

Every meaningful signal is stamped; later we measure what the HASH price
actually did 1h, 24h and 7d afterward. The resulting hit-rate table per signal
kind ("large mints preceded a +2% day 8 of 11 times") feeds the decision
engine and /predict — evidence instead of vibes. Signals with no price data
around them are marked measured-with-NULL and excluded from statistics.

The Analyst Accuracy Score compares the measured hit rate of the bot's signals
against the tape's own BASE RATE (how often ANY random day gained the same
threshold) — edge over chance, the only honest way to grade a signal engine.
"""

import logging
import time

from .events import Event, EventStore
from .scoring import SIGNAL_TAGS

log = logging.getLogger("nuva.outcomes")

GIVE_UP_AFTER = 48 * 3600  # if the tape has no price near the horizon by then, mark NULL
HORIZONS = (("1h", 3600.0), ("24h", 86400.0), ("7d", 7 * 86400.0))


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
        for horizon, seconds in HORIZONS:
            for event_id, ts in self.store.outcomes_pending(horizon, now - seconds):
                ret = self._measure(ts, seconds)
                if ret is None and now - (ts + seconds) < GIVE_UP_AFTER:
                    continue  # tape may still fill in; try again next cycle
                self.store.outcome_set(event_id, horizon, ret)
                written += 1
        return written

    def hit_rates(self, min_n: int = 3) -> dict:
        return self.store.hit_rates(min_n=min_n, positive_pct=self.positive_pct)

    # ---- Analyst Accuracy Score: measured edge over chance -----------------
    def base_rate(self, days: float = 60) -> float | None:
        """How often did ANY hour's forward-24h return clear +positive_pct?
        This is the chance baseline the signals must beat."""
        from .quant import resample_hourly  # noqa: PLC0415 (avoid import cycle at module load)
        bars = resample_hourly(self.store.ticks(days * 24, limit=20000))
        closes = [b[1] for b in bars]
        if len(closes) < 48:
            return None
        wins = total = 0
        for i in range(len(closes) - 24):
            if closes[i] > 0:
                total += 1
                if (closes[i + 24] - closes[i]) / closes[i] * 100.0 >= self.positive_pct:
                    wins += 1
        return wins / total if total else None

    def accuracy(self) -> dict | None:
        """{'n', 'hit_rate', 'base_rate', 'edge', 'score'} across all measured
        signals, or None when the sample is too thin to grade honestly."""
        rates = self.hit_rates(min_n=1)
        n = sum(s["n"] for s in rates.values())
        if n < 5:
            return None
        hit = sum(s["up_rate"] * s["n"] for s in rates.values()) / n
        base = self.base_rate()
        if base is None:
            return None
        edge = hit - base
        # 50 = no edge over chance; each pct-point of edge moves the score ~1.5
        score = int(max(0, min(50 + edge * 150, 100)))
        return {"n": n, "hit_rate": hit, "base_rate": base, "edge": edge, "score": score}

    def accuracy_line(self) -> str | None:
        a = self.accuracy()
        if a is None:
            return None
        return (f"Analyst accuracy: {a['score']}/100 — signals were followed by a "
                f"+{self.positive_pct:g}% day {a['hit_rate']:.0%} of the time vs a "
                f"{a['base_rate']:.0%} chance baseline ({a['n']} measured signals, "
                f"edge {a['edge']:+.0%}).")

    def evidence_line(self, kind: str) -> str | None:
        """One plain sentence of historical evidence for a signal kind, or None."""
        stats = self.hit_rates().get(kind)
        if not stats:
            return None
        pct = int(stats["up_rate"] * 100)
        return (f"History check: {kind} signals like this were followed by a "
                f"+{self.positive_pct:g}% (or better) day {pct}% of the time "
                f"({stats['n']} measured cases, avg 24h move {stats['avg_24h']:+.1f}%).")

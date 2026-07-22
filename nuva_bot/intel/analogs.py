"""Historical Analog Engine — "has this happened before, and what followed?"

Layer 4 of the intelligence stack. The current story (its signal tags + the
layers it spans) is compared against every past story in the event memory.
Similarity is Jaccard overlap on tags plus a layer-mix bonus; for each analog
we measure the REAL forward price move from the tape (24h and 7d after the
story began). Output is honest: below three price-measured analogs the engine
says "insufficient history" instead of inventing a percentage.
"""

import statistics
import time
from dataclasses import dataclass, field

from .events import EventStore

LOOKBACK_DAYS = 120       # how far back we search for analogs
MIN_GAP_S = 24 * 3600     # a story must be at least this old to count as "past"
MIN_MEASURED = 3          # analogs with price data needed before we quote stats


@dataclass
class Analog:
    story_id: str
    ts: float               # story start
    similarity: float       # 0..1
    tags: set = field(default_factory=set)
    layers: set = field(default_factory=set)
    n_events: int = 0
    ret_24h: float | None = None
    ret_7d: float | None = None
    headline: str = ""


@dataclass
class AnalogReport:
    ok: bool
    note: str = ""
    n_found: int = 0        # analogs found at all
    n_measured: int = 0     # analogs with a measurable price outcome
    similarity: float = 0.0  # mean similarity of the top analogs (0..100 scale)
    avg_24h: float | None = None
    median_24h: float | None = None
    avg_7d: float | None = None
    up_rate_24h: float | None = None   # share of analogs followed by a positive 24h
    analogs: list = field(default_factory=list)

    def line(self) -> str | None:
        """One plain-language sentence for alerts/briefs, or None if too thin."""
        if not self.ok:
            return None
        pct = int(self.similarity)
        parts = [f"{self.n_measured} similar past situation(s) found (≈{pct}% pattern match)"]
        if self.avg_24h is not None:
            parts.append(f"avg next-24h move {self.avg_24h:+.1f}%")
        if self.up_rate_24h is not None:
            parts.append(f"positive follow-through {int(self.up_rate_24h * 100)}% of the time")
        if self.avg_7d is not None:
            parts.append(f"avg 7-day move {self.avg_7d:+.1f}%")
        return "; ".join(parts) + "."


def _story_signature(events: list) -> tuple[set, set]:
    tags: set = set()
    layers: set = set()
    for e in events:
        layers.add(e.layer)
        tags |= {t for t in e.tags if t != e.layer and not t.startswith("watch:")}
    return tags, layers


def _similarity(tags_a: set, layers_a: set, tags_b: set, layers_b: set) -> float:
    if not tags_a or not tags_b:
        return 0.0
    jac = len(tags_a & tags_b) / len(tags_a | tags_b)
    layer_bonus = len(layers_a & layers_b) / max(len(layers_a | layers_b), 1)
    return 0.75 * jac + 0.25 * layer_bonus


class AnalogEngine:
    """Groups the event memory into stories once per call window and matches
    the live story against them. All math is over real stored data."""

    def __init__(self, store: EventStore, max_age: float = 600):
        self.store = store
        self.max_age = max_age
        self._cache: tuple[float, list[Analog]] | None = None

    # ---- past-story index -------------------------------------------------
    def _past_stories(self, now: float) -> list[Analog]:
        if self._cache and now - self._cache[0] < self.max_age:
            return self._cache[1]
        events = self.store.recent(LOOKBACK_DAYS * 24, limit=4000)
        by_story: dict[str, list] = {}
        for e in events:
            if e.story_id and e.priority != "ignore":
                by_story.setdefault(e.story_id, []).append(e)
        stories: list[Analog] = []
        for sid, evs in by_story.items():
            start = min(e.ts for e in evs)
            if now - start < MIN_GAP_S:
                continue  # too recent — that's the live situation, not history
            tags, layers = _story_signature(evs)
            if not tags:
                continue
            head = max(evs, key=lambda e: e.confidence)
            stories.append(Analog(
                story_id=sid, ts=start, similarity=0.0, tags=tags, layers=layers,
                n_events=len(evs), headline=head.title[:100],
            ))
        self._cache = (now, stories)
        return stories

    def _measure(self, a: Analog):
        p0 = self.store.price_near(a.ts)
        if not p0 or p0 <= 0:
            return
        p24 = self.store.price_near(a.ts + 86400)
        p7d = self.store.price_near(a.ts + 7 * 86400, tolerance=6 * 3600)
        if p24 and p24 > 0:
            a.ret_24h = (p24 - p0) / p0 * 100.0
        if p7d and p7d > 0:
            a.ret_7d = (p7d - p0) / p0 * 100.0

    # ---- the public read --------------------------------------------------
    def compare(self, tags: set, layers: set, now: float | None = None,
                top_k: int = 8, min_similarity: float = 0.25) -> AnalogReport:
        now = now or time.time()
        tags = {t for t in tags if not t.startswith("watch:")}
        if not tags:
            return AnalogReport(ok=False, note="current situation carries no signal tags to match on")

        scored: list[Analog] = []
        for st in self._past_stories(now):
            sim = _similarity(tags, layers, st.tags, st.layers)
            if sim >= min_similarity:
                st.similarity = sim
                scored.append(st)
        scored.sort(key=lambda a: -a.similarity)
        top = scored[:top_k]
        for a in top:
            self._measure(a)
        measured = [a for a in top if a.ret_24h is not None]

        if len(measured) < MIN_MEASURED:
            return AnalogReport(
                ok=False, n_found=len(scored), n_measured=len(measured),
                note=(f"only {len(measured)} price-measured analog(s) in memory — "
                      f"needs {MIN_MEASURED} before historical odds are quoted"),
                analogs=top,
            )

        rets24 = [a.ret_24h for a in measured]
        rets7 = [a.ret_7d for a in measured if a.ret_7d is not None]
        return AnalogReport(
            ok=True,
            n_found=len(scored), n_measured=len(measured),
            similarity=statistics.mean(a.similarity for a in measured) * 100,
            avg_24h=statistics.mean(rets24),
            median_24h=statistics.median(rets24),
            avg_7d=statistics.mean(rets7) if rets7 else None,
            up_rate_24h=sum(1 for r in rets24 if r > 0) / len(rets24),
            analogs=measured,
        )

    def for_event(self, ev, now: float | None = None) -> AnalogReport:
        """Analog report for the story a single event belongs to."""
        evs = self.store.story_events(ev.story_id) or [ev]
        tags, layers = _story_signature(evs)
        return self.compare(tags, layers, now=now)

    def current(self, hours: float = 24, now: float | None = None) -> AnalogReport:
        """Analog report for the whole live situation (all recent signal flow)."""
        now = now or time.time()
        recent = [e for e in self.store.recent(hours, limit=300) if e.priority != "ignore"]
        if not recent:
            return AnalogReport(ok=False, note="no live signals in the window to match on")
        tags, layers = _story_signature(recent)
        return self.compare(tags, layers, now=now)

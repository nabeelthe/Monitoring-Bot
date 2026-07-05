"""Continuous risk engine: seven dimensions, each with score, trend and recommendation.

Scores are derived from the event store (48h window vs the prior 48h) plus
live monitor health. 0 = calm, 100 = on fire.
"""

import time
from dataclasses import dataclass

from .events import EventStore

DIMENSIONS = ("security", "market", "liquidity", "governance", "developer", "reputation", "operational")


@dataclass
class RiskDimension:
    name: str
    score: int
    trend: str          # "rising" | "falling" | "flat"
    detail: str
    recommendation: str


class RiskEngine:
    def __init__(self, store: EventStore):
        self.store = store
        self._cache: tuple[float, list[RiskDimension]] | None = None

    def snapshot(self, monitor_statuses: dict | None = None, max_age: float = 900) -> list[RiskDimension]:
        if self._cache and time.time() - self._cache[0] < max_age:
            return self._cache[1]
        dims = self._compute(monitor_statuses or {})
        self._cache = (time.time(), dims)
        return dims

    def _kw(self, words, hours=48) -> int:
        return sum(self.store.escalation_hits(w, hours) for w in words)

    @staticmethod
    def _trend(now: int, before: int) -> str:
        if now > before:
            return "rising"
        if now < before:
            return "falling"
        return "flat"

    @staticmethod
    def _clamp(x: float) -> int:
        return int(max(0, min(x, 100)))

    def _compute(self, statuses: dict) -> list[RiskDimension]:
        dims = []

        sec_now = self._kw(("exploit", "hack", "hacked", "depeg"), 48)
        sec_prev = self._kw(("exploit", "hack", "hacked", "depeg"), 96) - sec_now
        dims.append(RiskDimension(
            "security", self._clamp(10 + sec_now * 25), self._trend(sec_now, sec_prev),
            f"{sec_now} exploit/hack/depeg signal(s) in 48h",
            "Investigate immediately and verify on-chain." if sec_now else "No active threat signals.",
        ))

        mkt_events = [e for e in self.store.recent(48, layer="market") if "move" in e.title.lower() or "spike" in e.title.lower()]
        mkt_prev = [e for e in self.store.recent(96, layer="market") if e.ts < time.time() - 48 * 3600]
        dims.append(RiskDimension(
            "market", self._clamp(15 + len(mkt_events) * 12), self._trend(len(mkt_events), len(mkt_prev)),
            f"{len(mkt_events)} threshold price/volume event(s) in 48h",
            "Elevated volatility — size positions accordingly." if len(mkt_events) >= 2 else "Volatility within normal range.",
        ))

        liq = self._kw(("depeg",), 48) * 30 + sum(
            1 for e in self.store.recent(48, layer="market") if "tvl" in e.title.lower() or "liquidity" in e.title.lower()) * 15
        dims.append(RiskDimension(
            "liquidity", self._clamp(12 + liq), "flat" if liq == 0 else "rising",
            "TVL/pool swing signals in window" if liq else "No liquidity stress detected",
            "Check pool depth before large orders." if liq else "Liquidity stable.",
        ))

        gov_now = len([e for e in self.store.recent(72, layer="onchain") if "proposal" in e.title.lower() or "upgrade" in e.title.lower()])
        dims.append(RiskDimension(
            "governance", self._clamp(10 + gov_now * 18), "rising" if gov_now else "flat",
            f"{gov_now} active proposal/upgrade signal(s)",
            "Review proposals before voting deadlines." if gov_now else "No pending governance action.",
        ))

        dev_now = len(self.store.recent(72, layer="dev"))
        dims.append(RiskDimension(
            "developer", self._clamp(35 - dev_now * 6), "falling" if dev_now else "rising",
            f"{dev_now} dev event(s) in 72h",
            "Healthy dev cadence." if dev_now else "Dev activity quiet — watch for stalled roadmap.",
        ))

        social_burst = len(self.store.recent(24, layer="social")) + len(self.store.recent(24, layer="news"))
        scam_hits = self._kw(("scam", "fake", "phishing"), 48)
        dims.append(RiskDimension(
            "reputation", self._clamp(10 + scam_hits * 20 + max(0, social_burst - 20)),
            "rising" if scam_hits else "flat",
            f"{social_burst} social/news mention(s) 24h; {scam_hits} scam-pattern hit(s)",
            "Verify announcements only via official domains." if scam_hits else "No reputation attacks detected.",
        ))

        failing = sum(1 for st in statuses.values() if getattr(st, "consecutive_failures", 0) >= 5)
        total = max(len(statuses), 1)
        dims.append(RiskDimension(
            "operational", self._clamp(5 + failing / total * 100 * 0.6),
            "rising" if failing else "flat",
            f"{failing}/{total} monitor(s) failing",
            "Fix failing collectors — blind spots hide signals." if failing else "All collectors nominal.",
        ))

        return dims

"""Predictive intelligence: transparent, evidence-based probability estimates.

Each prediction is a base rate plus increments per observed signal, with every
contributing signal named — the WHY is always shown, never a black box.
"""

from dataclasses import dataclass, field

from .events import EventStore


@dataclass
class Prediction:
    name: str
    probability: int          # 0-95, capped — never certainty
    horizon: str
    evidence: list = field(default_factory=list)


class Predictor:
    def __init__(self, store: EventStore):
        self.store = store

    def _signals(self, hours: float, *needles: str, layer: str | None = None) -> list:
        out = []
        for e in self.store.recent(hours, layer=layer, limit=500):
            text = f"{e.title} {e.body}".lower()
            for n in needles:
                if n in text:
                    out.append((n, e))
                    break
        return out

    @staticmethod
    def _cap(p: float) -> int:
        return int(max(2, min(p, 95)))

    def all(self) -> list[Prediction]:
        preds = []

        tge = self._signals(24 * 7, "tge", "token generation", "listing", "genesis pass", "snapshot", "public sale")
        token_layer = len(self.store.recent(24 * 7, layer="token"))
        p = 8 + len(tge) * 9 + token_layer * 4
        preds.append(Prediction(
            "NUVA TGE / first listing within 30d", self._cap(p), "30 days",
            [f"{len(tge)} TGE-adjacent keyword signal(s) in 7d",
             f"{token_layer} official token-page change(s) in 7d"],
        ))

        gov = self._signals(24 * 7, "proposal", "upgrade", layer="onchain")
        preds.append(Prediction(
            "Governance proposal / chain upgrade within 14d", self._cap(12 + len(gov) * 20), "14 days",
            [f"{len(gov)} governance/upgrade signal(s) in 7d"],
        ))

        dev = len(self.store.recent(24 * 7, layer="dev"))
        rel = self._signals(24 * 7, "release", layer="dev")
        preds.append(Prediction(
            "New software release within 14d", self._cap(15 + dev * 5 + len(rel) * 10), "14 days",
            [f"{dev} dev event(s) in 7d, {len(rel)} recent release(s)"],
        ))

        ann = self._signals(24 * 3, "partnership", "announcement", "integration")
        social = len(self.store.recent(24 * 3, layer="social"))
        preds.append(Prediction(
            "Major announcement / partnership within 14d", self._cap(10 + len(ann) * 12 + max(0, social - 10) * 2), "14 days",
            [f"{len(ann)} partnership/announcement signal(s) in 72h",
             f"social volume: {social} item(s) in 72h"],
        ))

        threat = self._signals(24 * 2, "exploit", "hack", "depeg", "drain")
        preds.append(Prediction(
            "Active security incident", self._cap(2 + len(threat) * 30), "now",
            [f"{len(threat)} threat-pattern signal(s) in 48h"],
        ))

        mint = self._signals(24 * 2, "mint", layer=None)
        moves = self._signals(24 * 2, "big move", "spike", layer="market")
        preds.append(Prediction(
            "Continued elevated market activity 48h", self._cap(20 + len(moves) * 15 + len(mint) * 8), "48 hours",
            [f"{len(moves)} threshold market move(s) in 48h",
             f"{len(mint)} mint/issuance event(s) in 48h"],
        ))

        return preds

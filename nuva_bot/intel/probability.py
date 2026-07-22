"""Probability Engine — Layer 6: odds, not adjectives.

Instead of a bare "bullish", produce an explicit short-term probability split
(bull / neutral / bear, summing to 100) with a separate confidence figure that
says how much the underlying data can be trusted. Deterministic and explained:
every shift away from the neutral prior is attributed to a named driver.

Never emits trading calls — it quantifies evidence, the human decides.
"""

import math
from dataclasses import dataclass, field

from .analogs import AnalogReport
from .quant import QuantSnapshot


@dataclass
class ProbabilityMatrix:
    bull: int              # % chance the next 24h leans positive
    neutral: int
    bear: int
    confidence: int        # 0-100: how trustworthy this split is
    horizon: str = "next 24h"
    drivers: list = field(default_factory=list)   # plain sentences

    def line(self) -> str:
        return (f"Bull {self.bull}% · Neutral {self.neutral}% · Bear {self.bear}% "
                f"({self.horizon}) — confidence {self.confidence}%")


def _logistic(x: float, scale: float = 30.0) -> float:
    """Map a -100..100 evidence score to 0..1 smoothly."""
    return 1.0 / (1.0 + math.exp(-x / scale))


def assess(blend_score: int, quant: QuantSnapshot, analogs: AnalogReport,
           hit_rates: dict | None = None, risk_hot: bool = False) -> ProbabilityMatrix:
    drivers: list[str] = []

    # ---- neutral mass: how range-bound / unreadable is the tape? ----------
    if not quant.ok:
        neutral = 0.60
        drivers.append("price tape is still warming up, so most weight stays on 'no clear direction'")
    elif quant.regime == "ranging":
        neutral = 0.45
        drivers.append("market is ranging — sideways continuation is the base case")
    elif quant.regime == "turbulent":
        neutral = 0.30
        drivers.append("volatility is unusually high — outcomes are wider in both directions")
    else:
        neutral = 0.30

    # ---- split the rest between bull and bear from the evidence score -----
    p_bull_raw = _logistic(float(blend_score))
    if blend_score:
        drivers.append(f"blended quant + news-flow score of {blend_score:+d} tilts the remaining odds")

    # ---- historical analogs nudge the tilt (measured, capped influence) ---
    if analogs.ok and analogs.up_rate_24h is not None:
        w = min(analogs.n_measured / 10.0, 0.35)
        p_bull_raw = (1 - w) * p_bull_raw + w * analogs.up_rate_24h
        drivers.append(
            f"{analogs.n_measured} measured historical analog(s) had positive follow-through "
            f"{int(analogs.up_rate_24h * 100)}% of the time")

    # ---- a live security/liquidity fire caps the upside -------------------
    if risk_hot:
        p_bull_raw = min(p_bull_raw, 0.35)
        drivers.append("an active security/liquidity risk caps the bullish share")

    directional = 1.0 - neutral
    bull = directional * p_bull_raw

    # ---- integers that always sum to exactly 100 (bear takes the remainder)
    b, n = int(round(bull * 100)), int(round(neutral * 100))
    r = 100 - b - n

    # ---- confidence: richness of the underlying data ----------------------
    conf = 25.0
    if quant.ok:
        conf += 25
    if analogs.ok:
        conf += min(analogs.n_measured * 4, 20)
    sample = sum(s["n"] for s in (hit_rates or {}).values())
    conf += min(sample, 20)
    if quant.ok and quant.regime == "turbulent":
        conf *= 0.75
    conf = int(max(5, min(conf, 95)))

    return ProbabilityMatrix(bull=b, neutral=n, bear=r, confidence=conf,
                             drivers=drivers[:4])

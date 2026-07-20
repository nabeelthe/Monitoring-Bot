"""Quant Signal Engine — statistical read of the market from our own tape.

Pure-python math (no numpy/pandas — runs happily on a free-tier VM) over the
5-minute price/volume ticks: momentum, RSI, EMA trend, z-score anomaly
detection, volatility regime, and a composite Quant Score with a per-factor
breakdown so every number can be explained in one sentence.
"""

import math
import time
from dataclasses import dataclass, field

MIN_HOURS = 12  # minimum history before we publish a score


@dataclass
class QuantSnapshot:
    ok: bool
    note: str = ""
    ts: float = 0.0
    price: float = 0.0
    ret_1h: float | None = None
    ret_6h: float | None = None
    ret_24h: float | None = None
    ret_7d: float | None = None
    rsi: float | None = None
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    trend: str = "flat"            # "up" | "down" | "flat"  (EMA cross state)
    price_z: float | None = None   # last price vs 24h rolling mean, in std-devs
    vol_z: float | None = None     # last hour's volume vs 7d hourly baseline
    volatility_pct: float | None = None  # current vol vs its own 30d history (0-100)
    regime: str = "unknown"        # trending_up | trending_down | ranging | turbulent
    score: int = 0                 # -100 … +100
    factors: list = field(default_factory=list)  # (name, contribution, plain sentence)


# ---- math helpers ------------------------------------------------------------

def resample_hourly(ticks: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    """5-min ticks → hourly bars (ts, close, volume_sum). Oldest-first in/out."""
    bars: dict[int, list] = {}
    for ts, price, volume in ticks:
        if not price:
            continue
        hour = int(ts // 3600)
        bar = bars.setdefault(hour, [ts, price, 0.0])
        if ts >= bar[0]:
            bar[0], bar[1] = ts, price  # latest tick in the hour = close
        bar[2] += volume or 0.0
    return [(h * 3600.0, b[1], b[2]) for h, b in sorted(bars.items())]


def ema(values: list[float], span: int) -> float | None:
    if len(values) < span:
        return None
    k = 2.0 / (span + 1)
    e = sum(values[:span]) / span
    for v in values[span:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes[-(period + 1):-1], closes[-period:]):
        change = cur - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def pct_return(closes: list[float], bars_back: int) -> float | None:
    if len(closes) <= bars_back or closes[-1 - bars_back] == 0:
        return None
    return (closes[-1] - closes[-1 - bars_back]) / closes[-1 - bars_back] * 100.0


def zscore(series: list[float], value: float) -> float | None:
    if len(series) < 8:
        return None
    mean = sum(series) / len(series)
    var = sum((x - mean) ** 2 for x in series) / len(series)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (value - mean) / std


def realized_vol(closes: list[float], window: int = 24) -> float | None:
    """Std-dev of hourly log returns over the window (a plain volatility gauge)."""
    if len(closes) < window + 1:
        return None
    rets = []
    for prev, cur in zip(closes[-window - 1:-1], closes[-window:]):
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    if len(rets) < 4:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))


def _percentile_rank(history: list[float], value: float) -> float:
    if not history:
        return 50.0
    below = sum(1 for h in history if h <= value)
    return below / len(history) * 100.0


# ---- the engine ----------------------------------------------------------------

def compute(ticks: list[tuple[float, float, float]]) -> QuantSnapshot:
    bars = resample_hourly(ticks)
    if len(bars) < MIN_HOURS:
        need = MIN_HOURS - len(bars)
        return QuantSnapshot(
            ok=False,
            note=f"Warming up — about {need}h more price history needed before quant signals are reliable.",
        )
    closes = [b[1] for b in bars]
    vols = [b[2] for b in bars]
    snap = QuantSnapshot(ok=True, ts=bars[-1][0], price=closes[-1])

    snap.ret_1h = pct_return(closes, 1)
    snap.ret_6h = pct_return(closes, 6)
    snap.ret_24h = pct_return(closes, 24)
    snap.ret_7d = pct_return(closes, 24 * 7)
    snap.rsi = rsi(closes)

    fast, slow = ema(closes, 6), ema(closes, 24)
    if fast and slow:
        snap.ema_fast, snap.ema_slow = fast, slow
        gap = (fast - slow) / slow * 100.0 if slow else 0.0
        snap.trend = "up" if gap > 0.15 else ("down" if gap < -0.15 else "flat")

    snap.price_z = zscore(closes[-24:], closes[-1])
    if any(vols):
        base = [v for v in vols[-24 * 7:-1] if v > 0]
        if base and vols[-1] > 0:
            snap.vol_z = zscore(base, vols[-1])

    vol_now = realized_vol(closes)
    if vol_now is not None:
        history = []
        for i in range(25, len(closes)):
            v = realized_vol(closes[:i])
            if v is not None:
                history.append(v)
        snap.volatility_pct = _percentile_rank(history[-24 * 30:], vol_now) if history else 50.0

    # regime
    if snap.volatility_pct is not None and snap.volatility_pct >= 85:
        snap.regime = "turbulent"
    elif snap.trend == "up":
        snap.regime = "trending_up"
    elif snap.trend == "down":
        snap.regime = "trending_down"
    else:
        snap.regime = "ranging"

    _score(snap)
    return snap


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(x, hi))


def _score(snap: QuantSnapshot) -> None:
    factors: list[tuple[str, int, str]] = []

    if snap.ret_24h is not None:
        c = int(_clamp(snap.ret_24h * 3, -30, 30))
        factors.append(("24h momentum", c, f"price is {'up' if snap.ret_24h >= 0 else 'down'} {abs(snap.ret_24h):.1f}% over the last day"))
    if snap.ret_6h is not None:
        c = int(_clamp(snap.ret_6h * 2.5, -15, 15))
        factors.append(("6h momentum", c, f"short-term move of {snap.ret_6h:+.1f}% in 6h"))

    if snap.trend == "up":
        factors.append(("trend", 15, "the short-term average price is above the long-term one (an uptrend pattern)"))
    elif snap.trend == "down":
        factors.append(("trend", -15, "the short-term average price is below the long-term one (a downtrend pattern)"))

    if snap.rsi is not None:
        if snap.rsi >= 75:
            factors.append(("RSI", -8, f"RSI {snap.rsi:.0f}: the rally looks stretched (overbought territory)"))
        elif 55 <= snap.rsi < 75:
            factors.append(("RSI", 8, f"RSI {snap.rsi:.0f}: buyers are in control but not overextended"))
        elif 25 < snap.rsi <= 45:
            factors.append(("RSI", -8, f"RSI {snap.rsi:.0f}: sellers currently have the upper hand"))
        elif snap.rsi <= 25:
            factors.append(("RSI", 8, f"RSI {snap.rsi:.0f}: heavily sold off — bounces often start near here"))

    if snap.vol_z is not None and snap.ret_24h is not None and abs(snap.vol_z) >= 1.0:
        if snap.ret_24h >= 0 and snap.vol_z > 0:
            factors.append(("volume", 10, "trading volume is well above normal and confirms the up-move"))
        elif snap.ret_24h < 0 and snap.vol_z > 0:
            factors.append(("volume", -10, "heavy volume on a down-move — sellers mean it"))

    score = float(sum(c for _, c, _ in factors))
    if snap.regime == "turbulent":
        score *= 0.8
        factors.append(("volatility", 0, "market is unusually turbulent — signals are less reliable right now"))

    snap.score = int(_clamp(score, -100, 100))
    snap.factors = factors


class QuantEngine:
    """Caches the snapshot so /terminal, alerts and the copilot share one compute."""

    def __init__(self, store, max_age: float = 300):
        self.store = store
        self.max_age = max_age
        self._cache: tuple[float, QuantSnapshot] | None = None

    def snapshot(self) -> QuantSnapshot:
        now = time.time()
        if self._cache and now - self._cache[0] < self.max_age:
            return self._cache[1]
        snap = compute(self.store.ticks(24 * 30, limit=10000))
        self._cache = (now, snap)
        return snap

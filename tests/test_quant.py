"""Tests for the Quant Brain (v3.0): indicators, regime, score, outcome
tracking with self-measured hit rates, and the decision engine."""

import asyncio
import math
import time

from nuva_bot.alerts import Alert
from nuva_bot.config import Config
from nuva_bot.intel.decision import decide, format_stance
from nuva_bot.intel.events import Event, EventStore
from nuva_bot.intel.outcomes import OutcomeTracker, signal_kind
from nuva_bot.intel.pipeline import IntelligencePipeline
from nuva_bot.intel.quant import (QuantEngine, compute, ema, resample_hourly,
                                  rsi, zscore)
from nuva_bot.intel.risk import RiskDimension
from nuva_bot.state import State


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def broadcast(self, text, loud=False):
        self.sent.append((text, loud))


def make_ticks(hours: float, price_fn, vol_fn=lambda i: 1000.0, step: int = 300):
    """Synthetic 5-min tape ending now. price_fn/vol_fn take the tick index."""
    now = time.time()
    n = int(hours * 3600 / step)
    return [(now - (n - i) * step, price_fn(i), vol_fn(i)) for i in range(n)]


# ---- indicator math ------------------------------------------------------------

def test_resample_hourly_takes_last_price_and_sums_volume():
    now = time.time()
    base = int(now // 3600) * 3600
    ticks = [(base + 60, 1.0, 10.0), (base + 3000, 2.0, 15.0), (base + 3660, 3.0, 5.0)]
    bars = resample_hourly(ticks)
    assert len(bars) == 2
    assert bars[0][1] == 2.0 and bars[0][2] == 25.0   # close=last, vol=sum
    assert bars[1][1] == 3.0


def test_ema_and_rsi_directionality():
    rising = [float(i) for i in range(1, 60)]
    falling = list(reversed(rising))
    assert ema(rising, 6) > ema(rising, 24)      # fast above slow in an uptrend
    assert rsi(rising) > 70
    assert rsi(falling) < 30


def test_zscore_flags_outlier():
    series = [10.0] * 20
    assert abs(zscore(series, 10.0)) < 0.01
    assert zscore([10.0] * 19 + [11.0], 20.0) > 3


# ---- compute() end-to-end -----------------------------------------------------

def test_compute_warming_up_with_thin_tape():
    snap = compute(make_ticks(3, lambda i: 1.0))
    assert not snap.ok
    assert "Warming up" in snap.note


def test_compute_uptrend_scores_positive():
    snap = compute(make_ticks(72, lambda i: 1.0 + i * 0.001))
    assert snap.ok
    assert snap.trend == "up"
    assert snap.score > 20
    assert snap.regime in ("trending_up", "turbulent")
    assert any("momentum" in name for name, _, _ in snap.factors)


def test_compute_downtrend_scores_negative():
    snap = compute(make_ticks(72, lambda i: 3.0 - i * 0.002))
    assert snap.ok
    assert snap.trend == "down"
    assert snap.score < -20


def test_compute_flat_market_is_neutralish():
    snap = compute(make_ticks(72, lambda i: 1.0 + 0.0001 * math.sin(i / 5)))
    assert snap.ok
    assert -20 <= snap.score <= 20


def test_quant_engine_caches(tmp_path):
    store = EventStore(tmp_path / "q.db")
    now = time.time()
    for i in range(200):
        store.add_tick(1.0 + i * 0.001, 500, ts=now - (200 - i) * 300)
    eng = QuantEngine(store)
    s1 = eng.snapshot()
    s2 = eng.snapshot()
    assert s1 is s2  # cached object identity within max_age
    store.close()


# ---- outcome tracking -----------------------------------------------------------

def _store_with_tape(tmp_path, hours=30, start_price=1.0, end_price=1.10):
    store = EventStore(tmp_path / "o.db")
    now = time.time()
    n = int(hours * 12)
    for i in range(n):
        frac = i / max(n - 1, 1)
        price = start_price + (end_price - start_price) * frac
        store.add_tick(price, 100, ts=now - (n - i) * 300)
    return store, now


def test_outcome_measured_after_horizon(tmp_path):
    store, now = _store_with_tape(tmp_path)     # steadily rising tape
    tracker = OutcomeTracker(store, positive_pct=2.0)
    ev = Event(monitor="m", layer="onchain", title="big mint",
               tags=["mint", "onchain"], priority="high", ts=now - 26 * 3600)
    tracker.record(ev)
    written = tracker.annotate(now=now)
    assert written == 2  # both 1h and 24h horizons measured
    rates = tracker.hit_rates(min_n=1)
    assert "mint" in rates
    assert rates["mint"]["n"] == 1
    assert rates["mint"]["avg_24h"] > 0        # rising tape ⇒ positive forward return
    store.close()


def test_outcome_not_measured_before_horizon(tmp_path):
    store, now = _store_with_tape(tmp_path)
    tracker = OutcomeTracker(store)
    ev = Event(monitor="m", layer="market", title="move", tags=["price", "market"],
               priority="high", ts=now - 600)   # only 10 minutes old
    tracker.record(ev)
    assert tracker.annotate(now=now) == 0
    store.close()


def test_outcome_low_priority_not_recorded(tmp_path):
    store, now = _store_with_tape(tmp_path)
    tracker = OutcomeTracker(store)
    ev = Event(monitor="m", layer="social", title="chatter", priority="ignore", ts=now - 90000)
    tracker.record(ev)
    assert tracker.annotate(now=now) == 0
    store.close()


def test_signal_kind_prefers_tag_over_layer():
    ev = Event(monitor="m", layer="onchain", title="t", tags=["mint", "onchain"])
    assert signal_kind(ev) == "mint"
    ev2 = Event(monitor="m", layer="news", title="t", tags=["news"])
    assert signal_kind(ev2) == "news"


def test_evidence_line_reports_sample_size(tmp_path):
    store, now = _store_with_tape(tmp_path, end_price=1.2)
    tracker = OutcomeTracker(store, positive_pct=2.0)
    for k in range(3):
        ev = Event(monitor="m", layer="onchain", title=f"mint {k}",
                   tags=["mint", "onchain"], priority="high", ts=now - 26 * 3600 - k * 60)
        tracker.record(ev)
    tracker.annotate(now=now)
    line = tracker.evidence_line("mint")
    assert line and "3 measured cases" in line
    store.close()


# ---- decision engine ------------------------------------------------------------

def _dims(**scores):
    base = {"security": 10, "market": 15, "liquidity": 10, "governance": 10,
            "developer": 20, "reputation": 10, "operational": 5}
    base.update(scores)
    return [RiskDimension(name, s, "flat", "detail", "rec") for name, s in base.items()]


def test_decision_bullish_on_strong_quant():
    snap = compute(make_ticks(72, lambda i: 1.0 + i * 0.001))
    st = decide(snap, [], _dims(), {})
    assert st.stance == "bullish"
    assert st.reasons


def test_decision_security_fire_forces_non_bullish():
    snap = compute(make_ticks(72, lambda i: 1.0 + i * 0.001))
    st = decide(snap, [], _dims(security=80), {})
    assert st.stance in ("bearish", "neutral")
    assert any("security" in r for r in st.reasons)


def test_decision_low_conviction_when_warming_up():
    snap = compute(make_ticks(2, lambda i: 1.0))
    st = decide(snap, [], _dims(), {})
    assert st.conviction == "low"
    assert st.caveats


def test_decision_event_flow_moves_needle():
    snap = compute(make_ticks(72, lambda i: 1.0))  # flat quant
    bull_events = [Event(monitor="m", layer="token", title=f"tge {i}",
                         tags=["tge", "token"], confidence=90, priority="high")
                   for i in range(4)]
    st_bull = decide(snap, bull_events, _dims(), {})
    st_flat = decide(snap, [], _dims(), {})
    assert st_bull.score > st_flat.score


def test_format_stance_is_plain_and_disclaimed():
    snap = compute(make_ticks(72, lambda i: 1.0 + i * 0.001))
    out = format_stance(decide(snap, [], _dims(), {}))
    assert "Quant read" in out and "not financial advice" in out


# ---- pipeline integration ---------------------------------------------------------

def test_pipeline_records_outcomes_and_stance_works(tmp_path):
    cfg = Config({"intelligence": {"db_path": str(tmp_path / "i.db"), "ai": {"enabled": False}},
                  "alerting": {"digest_interval_seconds": 9999}})
    pipe = IntelligencePipeline(cfg, State(tmp_path / "s.json"), FakeNotifier())
    now = time.time()
    for i in range(200):
        pipe.store.add_tick(1.0 + i * 0.0005, 800, ts=now - (200 - i) * 300)
    alert = Alert(monitor="Provenance Explorer", layer="onchain",
                  title="LARGE mint detected", priority="always")
    ev = run(pipe.process(alert, []))
    assert ev.priority in ("critical", "high", "medium")
    st = pipe.stance()
    assert st.stance in ("bullish", "bearish", "neutral")
    pipe.store.close()


def test_terminal_shows_quant_line(tmp_path):
    from nuva_bot.intel.terminal import build_terminal
    cfg = Config({"intelligence": {"db_path": str(tmp_path / "i.db"), "ai": {"enabled": False}},
                  "alerting": {"digest_interval_seconds": 9999}})
    pipe = IntelligencePipeline(cfg, State(tmp_path / "s.json"), FakeNotifier())
    now = time.time()
    for i in range(300):
        pipe.store.add_tick(1.0 + i * 0.0004, 900, ts=now - (300 - i) * 300)
    pipe.state.kv_set("cg:last_price", 1.12)
    out = build_terminal(pipe)
    assert "QUANT" in out and "/quant for why" in out
    pipe.store.close()

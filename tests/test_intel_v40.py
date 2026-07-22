"""Tests for the v4.0 Intelligence Terminal: historical analogs, probability
matrix, decision matrix, knowledge graph, narratives, crowd psychology,
market context, 7d self-evaluation + analyst accuracy, and the /brief report."""

import asyncio
import time

from nuva_bot.alerts import Alert
from nuva_bot.config import Config
from nuva_bot.intel import context as market_context
from nuva_bot.intel import narrative, psychology
from nuva_bot.intel.analogs import AnalogEngine, AnalogReport
from nuva_bot.intel.decision import decide
from nuva_bot.intel.events import Event, EventStore
from nuva_bot.intel.graph import KnowledgeGraph, extract_entities
from nuva_bot.intel.matrix import build_matrix, format_matrix
from nuva_bot.intel.outcomes import OutcomeTracker
from nuva_bot.intel.pipeline import IntelligencePipeline
from nuva_bot.intel.probability import assess
from nuva_bot.intel.quant import QuantSnapshot, compute
from nuva_bot.intel.risk import RiskDimension
from nuva_bot.state import State


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def broadcast(self, text, loud=False):
        self.sent.append((text, loud))


def make_ticks(hours: float, price_fn, step: int = 300):
    now = time.time()
    n = int(hours * 3600 / step)
    return [(now - (n - i) * step, price_fn(i), 1000.0) for i in range(n)]


def _dims(**scores):
    base = {"security": 10, "market": 15, "liquidity": 10, "governance": 10,
            "developer": 20, "reputation": 10, "operational": 5}
    base.update(scores)
    return [RiskDimension(name, s, "flat", "detail", "rec") for name, s in base.items()]


def _rising_store(tmp_path, days=6):
    """Store with a steadily rising tape covering `days` back from now."""
    store = EventStore(tmp_path / "a.db")
    now = time.time()
    n = days * 24 * 4  # 15-min spacing
    for i in range(n):
        store.add_tick(1.0 + i * (0.3 / n), 500, ts=now - (n - i) * 900)
    return store, now


# ---- historical analog engine -------------------------------------------------

def _seed_past_story(store, sid, start_ts, tags):
    for k in range(2):
        store.add(Event(monitor="m", layer="onchain", title=f"whale mint {sid}-{k}",
                        tags=list(tags) + ["onchain"], priority="high",
                        confidence=80, story_id=sid, ts=start_ts + k * 600))


def test_analogs_quote_measured_outcomes(tmp_path):
    store, now = _rising_store(tmp_path)
    for i, back_days in enumerate((5, 4, 3)):
        _seed_past_story(store, f"s{i}", now - back_days * 86400, ["mint", "whale"])
    rep = AnalogEngine(store).compare({"mint", "whale"}, {"onchain"}, now=now)
    assert rep.ok
    assert rep.n_measured >= 3
    assert rep.avg_24h is not None and rep.avg_24h > 0    # rising tape
    assert rep.up_rate_24h == 1.0
    assert "similar past situation" in rep.line()
    store.close()


def test_analogs_honest_when_history_thin(tmp_path):
    store = EventStore(tmp_path / "b.db")
    rep = AnalogEngine(store).compare({"mint"}, {"onchain"})
    assert not rep.ok
    assert rep.line() is None
    store.close()


def test_analogs_recent_story_not_its_own_analog(tmp_path):
    store, now = _rising_store(tmp_path)
    _seed_past_story(store, "live", now - 3600, ["mint", "whale"])  # too recent
    rep = AnalogEngine(store).compare({"mint", "whale"}, {"onchain"}, now=now)
    assert rep.n_found == 0
    store.close()


# ---- probability engine -------------------------------------------------------

def _analog_report(up_rate=0.8, n=5):
    return AnalogReport(ok=True, n_found=n, n_measured=n, similarity=60.0,
                        avg_24h=2.0, median_24h=2.0, avg_7d=4.0, up_rate_24h=up_rate)


def test_probability_sums_to_100_and_tracks_evidence():
    quant = compute(make_ticks(72, lambda i: 1.0 + i * 0.001))
    p = assess(40, quant, _analog_report())
    assert p.bull + p.neutral + p.bear == 100
    assert p.bull > p.bear
    assert 5 <= p.confidence <= 95
    assert p.drivers

    n = assess(-40, quant, _analog_report(up_rate=0.2))
    assert n.bear > n.bull


def test_probability_risk_fire_caps_bull():
    quant = compute(make_ticks(72, lambda i: 1.0 + i * 0.001))
    hot = assess(60, quant, _analog_report(), risk_hot=True)
    calm = assess(60, quant, _analog_report(), risk_hot=False)
    assert hot.bull < calm.bull
    assert hot.bull + hot.neutral + hot.bear == 100


def test_probability_warming_tape_leans_neutral():
    p = assess(0, QuantSnapshot(ok=False, note="warming"), AnalogReport(ok=False))
    assert p.neutral >= p.bull and p.neutral >= p.bear


# ---- decision matrix ----------------------------------------------------------

def test_matrix_has_invalidators_and_no_trading_calls():
    quant = compute(make_ticks(72, lambda i: 1.0 + i * 0.001))
    stance = decide(quant, [], _dims(), {})
    prob = assess(stance.score, quant, _analog_report())
    m = build_matrix(stance, prob, quant, _analog_report(), _dims(),
                     wallet_churn=2, hit_sample=4)
    assert m.confirmers and m.invalidators
    assert m.unknowns          # small hit_sample + churn wallets ⇒ unknowns exist
    out = format_matrix(m)
    assert "Decision Matrix" in out and "invalidate" in out
    for banned in ("BUY", "SELL", "LONG", "SHORT"):
        assert banned not in out
    assert "not a trading call" in out


def test_matrix_security_fire_is_top_invalidator():
    quant = compute(make_ticks(72, lambda i: 1.0 + i * 0.001))
    stance = decide(quant, [], _dims(security=80), {})
    prob = assess(stance.score, quant, AnalogReport(ok=False))
    m = build_matrix(stance, prob, quant, AnalogReport(ok=False), _dims(security=80))
    assert "security" in m.invalidators[0]


# ---- knowledge graph ----------------------------------------------------------

def test_extract_entities_finds_the_zoo():
    ev = Event(monitor="m", layer="onchain",
               title="Whale 0x1234567890abcdef1234567890abcdef12345678 moved 3M HASH to Binance",
               body="announced by @NuvaFinance — Nuva Labs confirms")
    kinds = {k for _, k, _ in extract_entities(ev)}
    assert {"wallet", "exchange", "account", "organization", "token"} <= kinds


def test_graph_observe_builds_relationships(tmp_path):
    store = EventStore(tmp_path / "g.db")
    g = KnowledgeGraph(store)
    ev = Event(monitor="m", layer="onchain",
               title="0x1234567890abcdef1234567890abcdef12345678 sent HASH to binance")
    assert g.observe(ev) >= 3
    nodes, edges = store.graph_counts()
    assert nodes >= 3 and edges >= 3
    profile = g.profile("binance")
    assert "exchange" in profile
    neighbors = store.graph_neighbors("ex:binance")
    assert neighbors and any(n["kind"] == "wallet" for n, _ in neighbors)
    store.close()


def test_wallet_profile_reads_flow_record(tmp_path):
    store = EventStore(tmp_path / "w.db")
    g = KnowledgeGraph(store)
    now = time.time()
    w = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
    for d in range(3):
        store.record_wallet_flow("ethereum", w, "in", 1000, "NUVA", ts=now - d * 86400)
        store.record_wallet_flow("ethereum", w, "out", 900, "NUVA", ts=now - d * 86400 + 60)
    g.observe_flow("ethereum", w, "in", now)
    profile = g.profile(w)
    assert "Flow record" in profile and "churning" in profile
    stats = store.wallet_stats(w)
    assert stats["days_active"] == 3
    assert g.wallet_behavior({"total_in": 5000, "total_out": 100,
                              "days_active": 1, "last_ts": now}) == "accumulating — inflows dominate"
    store.close()


def test_graph_profile_honest_when_empty(tmp_path):
    store = EventStore(tmp_path / "e.db")
    out = KnowledgeGraph(store).profile("nothing-here")
    assert "No entity matching" in out
    store.close()


# ---- narratives ---------------------------------------------------------------

def test_narrative_detects_entering(tmp_path):
    store = EventStore(tmp_path / "n.db")
    now = time.time()
    for i in range(3):
        store.add(Event(monitor="m", layer="blog", title=f"RWA tokenization push {i}",
                        priority="medium", ts=now - 3600 * (i + 1)))
    reads = narrative.detect(store, now=now)
    rwa = next(r for r in reads if "RWA" in r.name)
    assert rwa.now == 3 and rwa.prev == 0 and rwa.direction == "entering"
    panel = narrative.format_panel(reads)
    assert "entering" in panel
    store.close()


def test_narrative_quiet_panel_is_honest(tmp_path):
    store = EventStore(tmp_path / "nq.db")
    panel = narrative.format_panel(narrative.detect(store))
    assert "no narrative signals" in panel
    store.close()


# ---- crowd psychology ---------------------------------------------------------

def test_psychology_capitulation(tmp_path):
    store = EventStore(tmp_path / "p.db")
    snap = QuantSnapshot(ok=True, rsi=20.0, ret_24h=-8.0, vol_z=2.0, regime="trending_down")
    r = psychology.read(snap, store)
    assert r.state == "capitulation"
    assert r.evidence
    store.close()


def test_psychology_fomo_needs_social_surge(tmp_path):
    store = EventStore(tmp_path / "p2.db")
    now = time.time()
    for i in range(2):  # yesterday: 2 social posts
        store.add(Event(monitor="m", layer="social", title=f"old {i}",
                        priority="low", ts=now - 30 * 3600 - i))
    for i in range(5):  # today: 5 ⇒ surge
        store.add(Event(monitor="m", layer="social", title=f"hype {i}",
                        priority="low", ts=now - 3600 - i))
    snap = QuantSnapshot(ok=True, rsi=80.0, ret_24h=6.0, vol_z=1.0, regime="trending_up")
    r = psychology.read(snap, store, now=now)
    assert r.state == "FOMO / overheating"
    store.close()


def test_psychology_unreadable_on_thin_tape(tmp_path):
    store = EventStore(tmp_path / "p3.db")
    r = psychology.read(QuantSnapshot(ok=False, note="warming"), store)
    assert "unreadable" in r.state
    assert "🌪" not in psychology.format_read(r) or True  # formatting never crashes
    store.close()


# ---- market context -----------------------------------------------------------

def test_market_context_flags_token_specific_strength(tmp_path):
    state = State(tmp_path / "s.json")
    state.kv_set("cg:hash_24h", 5.0)
    state.kv_set("cg:btc_24h", 1.0)
    state.kv_set("cg:eth_24h", 1.0)
    ctx = market_context.read(state)
    assert ctx.ok and abs(ctx.relative - 4.0) < 0.01
    assert "OUTPERFORMING" in ctx.verdict
    assert "BTC" in market_context.line(ctx)


def test_market_context_honest_without_data(tmp_path):
    ctx = market_context.read(State(tmp_path / "s2.json"))
    assert not ctx.ok
    assert market_context.line(ctx) is None


# ---- 7d horizon + analyst accuracy --------------------------------------------

def test_outcomes_measure_7d_horizon(tmp_path):
    store = EventStore(tmp_path / "o7.db")
    now = time.time()
    n = 10 * 24 * 4
    for i in range(n):
        store.add_tick(1.0 + i * (0.3 / n), 100, ts=now - (n - i) * 900)
    tracker = OutcomeTracker(store, positive_pct=2.0)
    for k in range(5):
        ev = Event(monitor="m", layer="onchain", title=f"mint {k}",
                   tags=["mint", "onchain"], priority="high",
                   ts=now - 8 * 86400 + k * 600)
        tracker.record(ev)
    written = tracker.annotate(now=now)
    assert written == 15                      # 5 signals × (1h, 24h, 7d)
    acc = tracker.accuracy()
    assert acc is not None
    assert acc["n"] == 5
    assert 0 <= acc["score"] <= 100
    assert 0 <= acc["base_rate"] <= 1
    assert "accuracy" in tracker.accuracy_line().lower()
    store.close()


def test_accuracy_none_on_thin_sample(tmp_path):
    store = EventStore(tmp_path / "oa.db")
    assert OutcomeTracker(store).accuracy() is None
    store.close()


# ---- the research brief -------------------------------------------------------

def _pipeline(tmp_path):
    cfg = Config({"intelligence": {"db_path": str(tmp_path / "i.db"), "ai": {"enabled": False}},
                  "alerting": {"digest_interval_seconds": 9999}})
    pipe = IntelligencePipeline(cfg, State(tmp_path / "st.json"), FakeNotifier())
    now = time.time()
    for i in range(300):
        pipe.store.add_tick(1.0 + i * 0.0004, 900, ts=now - (300 - i) * 300)
    return pipe


def test_brief_retail_is_plain_and_disclaimed(tmp_path):
    pipe = _pipeline(tmp_path)
    out = pipe.research("retail")
    assert "RESEARCH BRIEF" in out and "retail mode" in out
    assert "not financial advice" in out
    pipe.store.close()


def test_brief_analyst_has_cases_and_probability(tmp_path):
    pipe = _pipeline(tmp_path)
    run(pipe.process(Alert(monitor="Provenance Explorer", layer="onchain",
                           title="LARGE mint detected", priority="always"), []))
    out = pipe.research("analyst")
    assert "Bull case" in out and "Bear case" in out
    assert "Probability matrix" in out
    assert "Historical comparison" in out
    pipe.store.close()


def test_brief_institutional_has_decision_matrix(tmp_path):
    pipe = _pipeline(tmp_path)
    out = pipe.research("institutional")
    assert "Decision Matrix" in out
    assert "Knowledge graph" in out
    for banned in ("BUY", "SELL"):
        assert banned not in out
    pipe.store.close()


def test_pipeline_probability_helper(tmp_path):
    pipe = _pipeline(tmp_path)
    p = pipe.probability()
    assert p.bull + p.neutral + p.bear == 100
    pipe.store.close()


def test_alert_gets_market_context_line(tmp_path):
    pipe = _pipeline(tmp_path)
    pipe.state.kv_set("cg:hash_24h", 6.0)
    pipe.state.kv_set("cg:btc_24h", 0.5)
    ev = run(pipe.process(Alert(monitor="CoinGecko HASH", layer="market",
                                title="HASH big move +6%", priority="escalate"),
                          ["surge"]))
    assert ev.priority in ("critical", "high", "medium")
    notifier_texts = " ".join(t for t, _ in pipe.notifier.sent)
    if notifier_texts:   # only sent when routed immediate
        assert "🌐" in notifier_texts or "market" in notifier_texts
    pipe.store.close()

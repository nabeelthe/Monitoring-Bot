import asyncio
import time

import pytest

from nuva_bot.alerts import Alert
from nuva_bot.config import Config
from nuva_bot.intel.analyst import Analyst
from nuva_bot.intel.correlator import Correlator
from nuva_bot.intel.events import Event, EventStore
from nuva_bot.intel.pipeline import IntelligencePipeline
from nuva_bot.intel.predict import Predictor
from nuva_bot.intel.reports import ReportGenerator
from nuva_bot.intel.risk import RiskEngine
from nuva_bot.intel.scoring import ConfidenceScorer, extract_tags
from nuva_bot.state import State


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "intel.db")
    yield s
    s.close()


def make_event(**kw):
    defaults = dict(monitor="Test", layer="onchain", title="NUVA mint detected")
    defaults.update(kw)
    return Event(**defaults)


# ---- event store -------------------------------------------------------

def test_store_roundtrip(store):
    ev = make_event(escalation_hits=["tge"], tags=["mint", "onchain"], confidence=88, priority="high")
    store.add(ev)
    got = store.recent(1)[0]
    assert got.id == ev.id and got.escalation_hits == ["tge"] and got.confidence == 88


def test_store_search_and_counts(store):
    store.add(make_event(title="Big TVL swing on Provenance", layer="market", priority="medium"))
    store.add(make_event(title="Governance proposal #7", layer="onchain", priority="high"))
    assert len(store.search("TVL")) == 1
    assert store.counts_by_layer(1) == {"market": 1, "onchain": 1}
    assert store.counts_by_priority(1)["high"] == 1


def test_store_min_priority_filter(store):
    store.add(make_event(title="a", priority="critical"))
    store.add(make_event(title="b", priority="low"))
    assert len(store.recent(1, min_priority="high")) == 1


def test_similar_past(store):
    old = make_event(title="NUVA airdrop snapshot announced", ts=time.time() - 3 * 86400)
    store.add(old)
    now = make_event(title="NUVA airdrop terms updated")
    assert store.similar_past(now)[0].id == old.id


def test_escalation_hits_count(store):
    store.add(make_event(escalation_hits=["exploit", "hack"]))
    assert store.escalation_hits("exploit") == 1
    assert store.escalation_hits("depeg") == 0


# ---- scoring -------------------------------------------------------------

def test_tags_extraction():
    tags = extract_tags("NUVA TGE date and airdrop terms", "genesis pass mint", "token")
    assert {"tge", "airdrop", "genesis", "mint", "token"} <= set(tags)


def test_scoring_onchain_beats_social():
    scorer = ConfidenceScorer(Config({}))
    on = scorer.score(make_event(layer="onchain"))
    so = scorer.score(make_event(layer="social"))
    assert on.confidence > so.confidence


def test_scoring_escalation_and_corroboration_raise_priority():
    scorer = ConfidenceScorer(Config({}))
    plain = scorer.score(make_event(layer="news"))
    hot = scorer.score(make_event(layer="news", escalation_hits=["tge", "listing"]), corroborating=3)
    assert hot.confidence > plain.confidence
    assert hot.priority in ("critical", "high")


def test_scoring_social_unconfirmed_never_critical():
    scorer = ConfidenceScorer(Config({}))
    ev = scorer.score(make_event(layer="social", escalation_hits=["listing"]))
    assert ev.priority != "critical"


def test_scoring_always_class_floor():
    scorer = ConfidenceScorer(Config({}))
    ev = scorer.score(make_event(layer="token", priority_class="always"))
    assert ev.priority in ("critical", "high", "medium")
    assert ev.priority != "ignore"


# ---- correlation ------------------------------------------------------------

def test_correlator_builds_cross_layer_story(store):
    corr = Correlator(Config({}), store)
    e1 = corr.assign(make_event(layer="onchain", title="NUVA mint on marker"))
    store.add(e1)
    e2 = corr.assign(make_event(layer="social", title="Whales talking about the NUVA mint"))
    store.add(e2)
    assert e1.story_id == e2.story_id
    cross = corr.corroboration(e2)
    assert len(cross) == 1 and cross[0].layer == "onchain"


def test_correlator_unrelated_events_get_new_story(store):
    corr = Correlator(Config({}), store)
    e1 = corr.assign(make_event(layer="market", title="price moved a bit"))
    e2 = corr.assign(make_event(layer="dev", title="fixed typo in readme"))
    assert e1.story_id != e2.story_id


# ---- analyst (rule-based path) ---------------------------------------------

def _analyst_no_ai():
    return Analyst(Config({"intelligence": {"ai": {"enabled": False}}}))


def test_rule_brief_incident_is_bearish(store):
    a = _analyst_no_ai()
    ev = make_event(title="Exploit drained vault", tags=["exploit", "onchain"],
                    escalation_hits=["exploit"], priority="critical")
    brief = a.rule_brief(ev, {"cross_layer": [], "history": []})
    assert brief.assessment == "bearish"
    assert "incident" in brief.suggested_action.lower()
    assert brief.needs_human


def test_rule_brief_flags_unconfirmed_social():
    a = _analyst_no_ai()
    ev = make_event(layer="social", title="NUVA listing rumor!!", escalation_hits=["listing"], tags=["listing", "social"])
    brief = a.rule_brief(ev, {"cross_layer": [], "history": []})
    assert brief.assessment == "suspicious"
    assert "manipulation" in brief.risk_note.lower()


def test_analyst_falls_back_without_key():
    a = _analyst_no_ai()
    ev = make_event(tags=["mint"])
    brief = run(a.analyze(ev, {"cross_layer": [], "history": []}))
    assert brief.source == "rules"


# ---- risk + predictions -----------------------------------------------------

def test_risk_security_reacts_to_exploit_events(store):
    risk = RiskEngine(store)
    calm = {d.name: d.score for d in risk._compute({})}
    store.add(make_event(title="exploit detected", escalation_hits=["exploit"]))
    hot = {d.name: d.score for d in risk._compute({})}
    assert hot["security"] > calm["security"]


def test_predictions_have_evidence(store):
    store.add(make_event(layer="token", title="TGE date announced on genesis page", escalation_hits=["tge"]))
    preds = Predictor(store).all()
    tge = next(p for p in preds if "TGE" in p.name)
    assert 0 < tge.probability <= 95 and tge.evidence


# ---- reports ----------------------------------------------------------------

def test_report_contains_sections(store, tmp_path):
    state = State(tmp_path / "s.json")
    store.add(make_event(title="Something big", priority="high", confidence=90))
    risk = RiskEngine(store)
    rep = ReportGenerator(store, risk, Predictor(store), state)
    text = rep.brief("daily")
    assert "Executive summary" in text and "Risk panel" in text and "Something big" in text


def test_report_scheduling_once_per_day(store, tmp_path):
    state = State(tmp_path / "s.json")
    rep = ReportGenerator(store, RiskEngine(store), Predictor(store), state)
    schedule = {"morning": 0}  # always due
    assert rep.due_scheduled_report(schedule) == "morning"
    assert rep.due_scheduled_report(schedule) is None  # not twice


# ---- pipeline routing ---------------------------------------------------------

class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def broadcast(self, text, loud=False):
        self.sent.append((text, loud))


def _pipeline(tmp_path):
    cfg = Config({
        "intelligence": {"db_path": str(tmp_path / "i.db"), "ai": {"enabled": False}},
        "alerting": {"digest_interval_seconds": 9999},
    })
    state = State(tmp_path / "s.json")
    notifier = FakeNotifier()
    return IntelligencePipeline(cfg, state, notifier), notifier


def test_pipeline_critical_sends_immediately(tmp_path):
    pipe, notifier = _pipeline(tmp_path)
    alert = Alert(monitor="Provenance Explorer", layer="onchain",
                  title="Nuva-denom MINT: exploit pattern", priority="always")
    ev = run(pipe.process(alert, ["exploit"]))
    assert ev.priority in ("critical", "high")
    assert len(notifier.sent) == 1
    text, loud = notifier.sent[0]
    assert "PRIORITY" in text and "Suggested action" in text
    pipe.store.close()


def test_pipeline_low_value_social_is_not_sent(tmp_path):
    pipe, notifier = _pipeline(tmp_path)
    alert = Alert(monitor="Reddit", layer="social", title="someone mentioned provenance", priority="kw")
    ev = run(pipe.process(alert, []))
    assert ev.priority in ("low", "ignore")
    assert notifier.sent == []
    pipe.store.close()


def test_pipeline_medium_goes_to_digest_and_flushes(tmp_path):
    pipe, notifier = _pipeline(tmp_path)

    async def flow():
        alert = Alert(monitor="Blog & Docs", layer="blog", title="Provenance blog updated", priority="escalate")
        ev = await pipe.process(alert, [])
        assert ev.priority == "medium"
        assert notifier.sent == []          # buffered, not sent
        await pipe._flush_digest()
        assert len(notifier.sent) == 1
        assert "Digest" in notifier.sent[0][0]

    run(flow())
    pipe.store.close()


def test_pipeline_record_only_stores_without_sending(tmp_path):
    pipe, notifier = _pipeline(tmp_path)
    alert = Alert(monitor="Provenance Governance", layer="onchain",
                  title="New governance proposal #1: mainnet upgrade", priority="always")
    ev = run(pipe.process(alert, ["mainnet"], record_only=True))
    assert notifier.sent == []
    assert pipe.store.total() == 1
    pipe.store.close()


def test_pipeline_quiet_hours_demotes_high(tmp_path):
    pipe, notifier = _pipeline(tmp_path)
    pipe.quiet_start, pipe.quiet_end = 0, 24  # always quiet
    alert = Alert(monitor="GitHub dev activity", layer="dev",
                  title="Release v2.0 mainnet vault", priority="escalate")
    ev = run(pipe.process(alert, ["mainnet"]))
    if ev.priority == "high":               # demoted to digest
        assert notifier.sent == []
    elif ev.priority == "critical":         # critical always breaks through
        assert len(notifier.sent) == 1
    pipe.store.close()


def test_pipeline_correlation_appears_in_alert(tmp_path):
    pipe, notifier = _pipeline(tmp_path)

    async def flow():
        await pipe.process(Alert(monitor="Provenance Explorer", layer="onchain",
                                 title="NUVA marker mint", priority="always"), [])
        await pipe.process(Alert(monitor="X (Twitter)", layer="social",
                                 title="@NUVALabs: mint is live, TGE next", priority="always"), ["tge"])

    run(flow())
    assert any("Correlated signals" in t for t, _ in notifier.sent)
    pipe.store.close()

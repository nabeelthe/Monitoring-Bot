from nuva_bot.alerts import Alert, AlertEngine, ALWAYS, ESCALATE, KW
from nuva_bot.config import Config

CFG = Config({
    "keywords": {
        "escalation": ["TGE", "airdrop", "mainnet", "exploit"],
        "relevance": ["nuva", "provenance"],
        "negative": ["nuvalab.ai", "game studio"],
    }
})


def make(priority=ESCALATE, title="hello", body="", **kw):
    return Alert(monitor="m", layer="news", title=title, body=body, priority=priority, **kw)


def test_always_is_loud_and_sent():
    v = AlertEngine(CFG).evaluate(make(priority=ALWAYS, title="anything at all"))
    assert v.send and v.loud


def test_escalate_normal_without_keyword():
    v = AlertEngine(CFG).evaluate(make(title="regular market update"))
    assert v.send and not v.loud


def test_escalate_goes_loud_on_keyword():
    v = AlertEngine(CFG).evaluate(make(title="NUVA TGE date announced"))
    assert v.send and v.loud and "tge" in v.escalation_hits


def test_escalation_keyword_case_insensitive_in_body():
    v = AlertEngine(CFG).evaluate(make(body="massive AIRDROP coming"))
    assert v.loud


def test_kw_gated_dropped_without_relevance():
    v = AlertEngine(CFG).evaluate(make(priority=KW, title="bitcoin hits new high"))
    assert not v.send


def test_kw_gated_sent_with_relevance():
    v = AlertEngine(CFG).evaluate(make(priority=KW, title="Provenance ships new module"))
    assert v.send and not v.loud


def test_kw_gated_escalation_keyword_forces_through():
    v = AlertEngine(CFG).evaluate(make(priority=KW, title="huge exploit found somewhere"))
    assert v.send and v.loud


def test_negative_filter_drops_gaming_namesake():
    v = AlertEngine(CFG).evaluate(make(title="nuvalab.ai raises seed for game studio"))
    assert not v.send


def test_negative_filter_skipped_when_not_filterable():
    v = AlertEngine(CFG).evaluate(make(title="nuvalab.ai something", filterable=False))
    assert v.send


def test_negative_filter_overridden_by_escalation():
    v = AlertEngine(CFG).evaluate(make(title="nuvalab.ai mentions NUVA TGE"))
    assert v.send and v.loud


def test_gate_keywords_override():
    a = make(priority=KW, title="fix: bump vault version for mainnet upgrade")
    a.gate_keywords = ["vault", "upgrade"]
    v = AlertEngine(CFG).evaluate(a)
    assert v.send and "vault" in v.gate_hits


def test_format_escapes_html_and_includes_url():
    eng = AlertEngine(CFG)
    a = make(title="<b>bold</b> & TGE", body="body <i>x</i>")
    a.url = "https://example.com/a?b=1&c=2"
    v = eng.evaluate(a)
    out = eng.format(a, v)
    assert "&lt;b&gt;" in out and "<b>[NEWS]" in out
    assert 'href="https://example.com/a?b=1&amp;c=2"' in out
    assert out.startswith("🚨 ")

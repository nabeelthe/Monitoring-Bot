import asyncio

import pytest

from nuva_bot.config import Config
from nuva_bot.monitors.base import Context
from nuva_bot.monitors.webwatch import WebWatchMonitor, normalize
from nuva_bot.monitors.provenance import ProvenanceGovMonitor
from nuva_bot.monitors.feeds import FeedMonitor, _entry_summary
from nuva_bot.state import State


class FakeMonitorFetch:
    """Patches Monitor.fetch to serve canned (status, body) responses per URL prefix."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls = []

    async def __call__(self, ctx, url, **kwargs):
        self.calls.append(url)
        for prefix, resp in self.responses.items():
            if url.startswith(prefix):
                return resp
        return 404, None


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture
def ctx(tmp_path):
    return Context(session=None, state=State(tmp_path / "s.json"), config=Config({}))


# --- webwatch ---------------------------------------------------------------

def test_normalize_strips_noise():
    a = normalize("<html><script>var x=12345678901;</script><p>Hello   World</p></html>")
    b = normalize("<html><script>var y=99999999999;</script><p>Hello World</p></html>")
    assert a == b == "hello world"


def _webwatch(pages):
    return WebWatchMonitor(Config({"webwatch": {"enabled": True, "cadence": 600, "pages": pages}}))


def test_webwatch_change_detection(ctx, monkeypatch):
    mon = _webwatch([{"name": "P", "url": "https://x.test/page", "mode": "change", "layer": "token"}])
    body1 = "<p>" + "original content here " * 20 + "</p>"
    fake = FakeMonitorFetch({"https://x.test/page": (200, body1)})
    monkeypatch.setattr(mon, "fetch", fake)

    assert run(mon.poll(ctx)) == []            # baseline
    assert run(mon.poll(ctx)) == []            # unchanged
    fake.responses["https://x.test/page"] = (200, "<p>" + "totally new TGE announcement " * 20 + "</p>")
    alerts = run(mon.poll(ctx))
    assert len(alerts) == 1
    assert "Page changed" in alerts[0].title
    assert alerts[0].priority == "always"


def test_webwatch_listing_transition(ctx, monkeypatch):
    mon = _webwatch([{"name": "CG NUVA", "url": "https://cg.test/nuva", "mode": "listing", "layer": "token"}])
    fake = FakeMonitorFetch({"https://cg.test/nuva": (404, None)})
    monkeypatch.setattr(mon, "fetch", fake)

    assert run(mon.poll(ctx)) == []            # 404 baseline
    assert run(mon.poll(ctx)) == []            # still 404
    fake.responses["https://cg.test/nuva"] = (200, "<p>" + "NUVA price market cap listing data " * 10 + "</p>")
    alerts = run(mon.poll(ctx))
    assert len(alerts) == 1
    assert "LISTING PAGE LIVE" in alerts[0].title
    assert run(mon.poll(ctx)) == []            # no repeat


def test_webwatch_listing_live_at_baseline_is_silent(ctx, monkeypatch):
    mon = _webwatch([{"name": "L", "url": "https://l.test/", "mode": "listing"}])
    fake = FakeMonitorFetch({"https://l.test/": (200, "<p>" + "already live content " * 10 + "</p>")})
    monkeypatch.setattr(mon, "fetch", fake)
    assert run(mon.poll(ctx)) == []
    assert run(mon.poll(ctx)) == []


# --- governance ---------------------------------------------------------------

def test_gov_new_proposal_and_upgrade(ctx, monkeypatch):
    mon = ProvenanceGovMonitor(Config({}))
    fake = FakeMonitorFetch({
        "https://api.provenance.io/cosmos/gov/v1/proposals": (200, {
            "proposals": [{"id": "99", "title": "Upgrade to v1.20", "status": "PROPOSAL_STATUS_VOTING_PERIOD"}],
        }),
        "https://api.provenance.io/cosmos/upgrade/v1beta1/current_plan": (200, {
            "plan": {"name": "mango", "height": "123456", "info": ""},
        }),
    })
    monkeypatch.setattr(mon, "fetch", fake)
    alerts = run(mon.poll(ctx))
    titles = [a.title for a in alerts]
    assert any("proposal #99" in t for t in titles)
    assert any("mango" in t for t in titles)
    assert all(a.priority == "always" for a in alerts)
    assert run(mon.poll(ctx)) == []            # dedupe


# --- feeds ---------------------------------------------------------------

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>NUVA airdrop announced</title><link>https://n.test/1</link><guid>g1</guid>
<description>&lt;b&gt;Big&lt;/b&gt; news about the airdrop</description></item>
<item><title>Unrelated post</title><link>https://n.test/2</link><guid>g2</guid></item>
</channel></rss>"""


def test_feed_monitor_parses_and_dedupes(ctx, monkeypatch):
    mon = FeedMonitor(Config({}), name="News", layer="news", cadence=900,
                      feeds=[{"name": "t", "url": "https://n.test/rss"}], priority="kw")
    monkeypatch.setattr(mon, "fetch", FakeMonitorFetch({"https://n.test/rss": (200, RSS)}))
    alerts = run(mon.poll(ctx))
    assert len(alerts) == 2
    assert alerts[0].title == "NUVA airdrop announced"
    assert "Big news about the airdrop" in alerts[0].body
    assert run(mon.poll(ctx)) == []            # dedupe on second poll


def test_feed_monitor_survives_broken_feed(ctx, monkeypatch):
    mon = FeedMonitor(Config({}), name="News", layer="news", cadence=900,
                      feeds=[{"name": "bad", "url": "https://bad.test/rss"},
                             {"name": "good", "url": "https://n.test/rss"}], priority="kw")
    monkeypatch.setattr(mon, "fetch", FakeMonitorFetch({
        "https://bad.test/rss": (500, None),
        "https://n.test/rss": (200, RSS),
    }))
    alerts = run(mon.poll(ctx))
    assert len(alerts) == 2                    # good feed still processed


def test_entry_summary_strips_tags():
    assert _entry_summary({"summary": "<p>Hello <b>world</b></p>"}) == "Hello world"

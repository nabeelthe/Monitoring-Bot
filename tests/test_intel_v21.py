"""Tests for the v2.1 intelligence upgrades: significance filtering, plain
language, media expansion, repost detection, per-source alert budget."""

import asyncio
import time

import pytest

from nuva_bot.alerts import Alert
from nuva_bot.config import Config
from nuva_bot.intel.events import Event
from nuva_bot.intel.pipeline import IntelligencePipeline
from nuva_bot.intel.plain import humanize
from nuva_bot.monitors.base import Context
from nuva_bot.monitors.provenance import ProvenanceExplorerMonitor, _extract_hash_amount
from nuva_bot.monitors.telegram_watch import TelegramChannelMonitor, _clean
from nuva_bot.state import State


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---- plain language --------------------------------------------------------

def test_humanize_maps_jargon_to_plain():
    assert "created" in humanize("onchain", "NUVA MsgMint 50000 nhash").lower()
    assert "destroyed" in humanize("onchain", "burn executed").lower()
    assert "exchange" in humanize("token", "NUVA listing on Binance").lower()
    assert "security" in humanize("news", "possible exploit drains vault").lower()


def test_humanize_security_takes_precedence():
    # a message with both 'mint' and 'exploit' should surface the security warning
    out = humanize("onchain", "attacker used fake mint to exploit vault")
    assert "⚠️" in out


def test_humanize_always_returns_something():
    assert humanize("weirdlayer", "??? unknown") != ""


# ---- explorer significance filtering ---------------------------------------

def test_extract_hash_amount_from_nhash():
    tx = {"amount": [{"denom": "nhash", "amount": "50000000000000"}]}  # 50,000 HASH
    assert _extract_hash_amount(tx) == pytest.approx(50000)


def test_extract_hash_amount_none_when_absent():
    assert _extract_hash_amount({"msg": {"displayMsgType": "send"}}) == 0.0


def _explorer(monkeypatch, txs):
    mon = ProvenanceExplorerMonitor(Config({"provenance": {"explorer": {
        "min_hash_amount": 25000, "summary_interval": 0,
        "msg_keywords": ["send", "mint", "marker"], "priority_denoms": ["nuva"]}}}))

    async def fake_fetch(ctx, url, **kw):
        return 200, {"results": txs}
    monkeypatch.setattr(mon, "fetch", fake_fetch)
    return mon


def test_explorer_big_transfer_alerts_individually(tmp_path, monkeypatch):
    ctx = Context(None, State(tmp_path / "s.json"), Config({}))
    mon = _explorer(monkeypatch, [
        {"txHash": "big1", "msg": {"displayMsgType": "send"},
         "amount": [{"denom": "nhash", "amount": "90000000000000"}]},  # 90k HASH
    ])
    alerts = run(mon.poll(ctx))
    assert any("LARGE" in a.title or "90,000" in a.title for a in alerts)


def test_explorer_small_transfers_are_summarized_not_spammed(tmp_path, monkeypatch):
    ctx = Context(None, State(tmp_path / "s.json"), Config({}))
    txs = [
        {"txHash": f"small{i}", "msg": {"displayMsgType": "send"},
         "amount": [{"denom": "nhash", "amount": "1000000000000"}]}  # 1k HASH each
        for i in range(6)
    ]
    mon = _explorer(monkeypatch, txs)
    alerts = run(mon.poll(ctx))
    # no per-tx alerts; exactly one summary (summary_interval=0 forces immediate)
    assert len(alerts) == 1
    assert "smaller on-chain transactions" in alerts[0].title


def test_explorer_nuva_mint_alerts_even_if_small(tmp_path, monkeypatch):
    ctx = Context(None, State(tmp_path / "s.json"), Config({}))
    mon = _explorer(monkeypatch, [
        {"txHash": "m1", "msg": {"displayMsgType": "mint"}, "monikers": "nuva",
         "amount": [{"denom": "nhash", "amount": "1000000000"}]},  # tiny, but structural + nuva
    ])
    alerts = run(mon.poll(ctx))
    assert any("mint" in a.title.lower() for a in alerts)


# ---- telegram public channel monitor ---------------------------------------

def test_telegram_clean_strips_html():
    assert _clean("Hello <b>NUVA</b><br/>world &amp; more") == "Hello NUVA world & more"


def test_telegram_parses_and_dedupes(tmp_path, monkeypatch):
    ctx = Context(None, State(tmp_path / "s.json"), Config({}))
    mon = TelegramChannelMonitor(Config({"social": {"telegram_channels": {"channels": ["nuvalabs"]}}}))
    html = (
        '<div class="tgme_widget_message" data-post="nuvalabs/10">'
        '<div class="tgme_widget_message_text">NUVA airdrop snapshot next week</div></div>'
    )

    async def fake_fetch(ctx, url, **kw):
        return 200, html
    monkeypatch.setattr(mon, "fetch", fake_fetch)
    alerts = run(mon.poll(ctx))
    assert len(alerts) == 1 and "airdrop" in alerts[0].title
    assert alerts[0].url == "https://t.me/nuvalabs/10"
    assert run(mon.poll(ctx)) == []  # dedupe


# ---- pipeline noise reduction ----------------------------------------------

class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def broadcast(self, text, loud=False):
        self.sent.append((text, loud))


def _pipeline(tmp_path, **cfg):
    base = {"intelligence": {"db_path": str(tmp_path / "i.db"), "ai": {"enabled": False}},
            "alerting": {"digest_interval_seconds": 9999}}
    base["alerting"].update(cfg)
    return IntelligencePipeline(Config(base), State(tmp_path / "s.json"), FakeNotifier())


def test_repost_detection_suppresses_duplicates(tmp_path):
    pipe = _pipeline(tmp_path)

    async def flow():
        a = Alert(monitor="News", layer="news", title="NUVA lists on major exchange today", priority="always")
        b = Alert(monitor="X (Twitter)", layer="social", title="NUVA lists on major exchange today!!", priority="always")
        await pipe.process(a, ["listing"])
        first = len(pipe.notifier.sent)
        await pipe.process(b, ["listing"])
        return first, len(pipe.notifier.sent)

    first, second = run(flow())
    assert first == 1
    assert second == 1  # duplicate suppressed
    assert pipe.counters.get("reposts", 0) == 1
    pipe.store.close()


def test_plain_terms_line_in_alert(tmp_path):
    pipe = _pipeline(tmp_path)
    alert = Alert(monitor="Provenance Explorer", layer="onchain",
                  title="LARGE Nuva-denom mint — 90,000 HASH", priority="always")
    run(pipe.process(alert, []))
    text = pipe.notifier.sent[0][0]
    assert "In plain terms" in text
    pipe.store.close()


def test_per_source_budget_demotes_after_cap(tmp_path):
    pipe = _pipeline(tmp_path, per_source_hourly_cap=3)

    topics = ["mainnet upgrade shipped", "validator rewards changed", "vault module audited",
              "explorer redesign launched", "staking parameters revised", "bridge integration added"]

    async def flow():
        for topic in topics:
            # genuinely distinct titles (not reposts of each other) from one source
            a = Alert(monitor="Blog & Docs", layer="blog",
                      title=f"Provenance {topic} mainnet", priority="escalate")
            await pipe.process(a, ["mainnet"])

    run(flow())
    # only the first few high alerts go out immediately; the rest are demoted to digest
    immediate = len(pipe.notifier.sent)
    assert immediate <= 3
    assert pipe.counters["digested"] >= 1
    pipe.store.close()

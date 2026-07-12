"""Tests for Wallet Intelligence: daily buy+sell (churn) wallet detection,
monitor wiring, alert formatting, and output readability."""

import asyncio
import time

from nuva_bot.config import Config
from nuva_bot.intel.events import EventStore
from nuva_bot.intel.pipeline import IntelligencePipeline
from nuva_bot.intel.plain import confidence_word, humanize
from nuva_bot.intel.wallets import WalletIntel, format_wallet
from nuva_bot.monitors.base import Context
from nuva_bot.monitors.ethereum import EtherscanMonitor
from nuva_bot.monitors.provenance import ProvenanceExplorerMonitor, _extract_addresses
from nuva_bot.state import State


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def broadcast(self, text, loud=False):
        self.sent.append((text, loud))


# ---- wallet_flows storage ------------------------------------------------------

def test_record_and_query_wallet_flows(tmp_path):
    store = EventStore(tmp_path / "w.db")
    now = time.time()
    store.record_wallet_flow("ethereum", "0xAAA", "in", 500, "NUVA", ts=now)
    store.record_wallet_flow("ethereum", "0xAAA", "out", 300, "NUVA", ts=now + 10)
    flows = store.wallet_daily_flows("ethereum", 7)
    assert "0xaaa" in flows  # lowercased
    day = next(iter(flows["0xaaa"].values()))
    assert day["in"] == 500 and day["out"] == 300
    store.close()


def test_wallet_flow_ignores_bad_input(tmp_path):
    store = EventStore(tmp_path / "w2.db")
    store.record_wallet_flow("ethereum", "", "in", 100)       # no wallet
    store.record_wallet_flow("ethereum", "0xB", "in", 0)      # zero amount
    store.record_wallet_flow("ethereum", "0xB", "sideways", 5)  # bad direction
    assert store.wallet_daily_flows("ethereum", 7) == {}
    store.close()


# ---- WalletIntel detection logic ------------------------------------------------

def _intel(tmp_path, **overrides):
    store = EventStore(tmp_path / "i.db")
    cfg = {"intelligence": {"wallets": {"min_days_both": 3, "min_daily_amount": 100, **overrides}}}
    return WalletIntel(Config(cfg), store), store


def test_flags_wallet_with_daily_buy_and_sell(tmp_path):
    intel, store = _intel(tmp_path)
    now = time.time()
    for day_offset in range(4):  # 4 distinct days, both sides each day
        ts = now - day_offset * 86400
        store.record_wallet_flow("ethereum", "0xDay", "in", 1000, "NUVA", ts=ts)
        store.record_wallet_flow("ethereum", "0xDay", "out", 800, "NUVA", ts=ts)
    traders = intel.active_traders("ethereum")
    assert len(traders) == 1
    w = traders[0]
    assert w.wallet == "0xday"
    assert w.days_active >= 3
    assert w.total_in == 4000 and w.total_out == 3200
    store.close()


def test_does_not_flag_one_directional_wallet(tmp_path):
    intel, store = _intel(tmp_path)
    now = time.time()
    for day_offset in range(5):
        store.record_wallet_flow("ethereum", "0xHodler", "in", 5000, "NUVA", ts=now - day_offset * 86400)
        # never sells
    assert intel.active_traders("ethereum") == []
    store.close()


def test_does_not_flag_below_min_days(tmp_path):
    intel, store = _intel(tmp_path)
    now = time.time()
    for day_offset in range(2):  # only 2 days of both-sides activity, need 3
        ts = now - day_offset * 86400
        store.record_wallet_flow("ethereum", "0xTwoDays", "in", 500, "NUVA", ts=ts)
        store.record_wallet_flow("ethereum", "0xTwoDays", "out", 500, "NUVA", ts=ts)
    assert intel.active_traders("ethereum") == []
    store.close()


def test_does_not_flag_below_min_daily_amount(tmp_path):
    intel, store = _intel(tmp_path)
    now = time.time()
    for day_offset in range(4):
        ts = now - day_offset * 86400
        store.record_wallet_flow("ethereum", "0xDust", "in", 5, "NUVA", ts=ts)   # below min_daily_amount=100
        store.record_wallet_flow("ethereum", "0xDust", "out", 5, "NUVA", ts=ts)
    assert intel.active_traders("ethereum") == []
    store.close()


def test_sorted_by_total_volume_descending(tmp_path):
    intel, store = _intel(tmp_path)
    now = time.time()
    for wallet, amt in (("0xBig", 10000), ("0xSmall", 500)):
        for day_offset in range(3):
            ts = now - day_offset * 86400
            store.record_wallet_flow("ethereum", wallet, "in", amt, "NUVA", ts=ts)
            store.record_wallet_flow("ethereum", wallet, "out", amt, "NUVA", ts=ts)
    traders = intel.active_traders("ethereum")
    assert traders[0].wallet == "0xbig"
    store.close()


def test_known_label_applied(tmp_path):
    intel, store = _intel(tmp_path, known_labels={"0xknown": "Market maker"})
    now = time.time()
    for day_offset in range(3):
        ts = now - day_offset * 86400
        store.record_wallet_flow("ethereum", "0xKnown", "in", 1000, "NUVA", ts=ts)
        store.record_wallet_flow("ethereum", "0xKnown", "out", 1000, "NUVA", ts=ts)
    w = intel.active_traders("ethereum")[0]
    assert "Market maker" in w.label
    store.close()


def test_whale_label_by_volume(tmp_path):
    intel, store = _intel(tmp_path)
    now = time.time()
    for day_offset in range(3):
        ts = now - day_offset * 86400
        store.record_wallet_flow("ethereum", "0xWhale", "in", 500000, "NUVA", ts=ts)
        store.record_wallet_flow("ethereum", "0xWhale", "out", 500000, "NUVA", ts=ts)
    w = intel.active_traders("ethereum")[0]
    assert "Whale" in w.label
    store.close()


# ---- output formatting -----------------------------------------------------------

def test_format_wallet_is_readable(tmp_path):
    intel, store = _intel(tmp_path)
    now = time.time()
    for day_offset in range(4):
        ts = now - day_offset * 86400
        store.record_wallet_flow("ethereum", "0x1234567890abcdef", "in", 1000, "NUVA", ts=ts)
        store.record_wallet_flow("ethereum", "0x1234567890abcdef", "out", 400, "NUVA", ts=ts)
    w = intel.active_traders("ethereum")[0]
    text = format_wallet(w)
    assert "Bought and sold NUVA" in text
    assert "4 of the last 7 days" in text
    assert "4,000 NUVA" in text or "4,000" in text  # total bought
    assert "buying more than selling" in text  # net positive, plain wording
    assert "etherscan.io/address/" in text
    store.close()


def test_format_wallet_shows_usd_when_price_given(tmp_path):
    intel, store = _intel(tmp_path)
    now = time.time()
    for day_offset in range(3):
        ts = now - day_offset * 86400
        store.record_wallet_flow("provenance", "cosmos1abc", "in", 1000, "HASH", ts=ts)
        store.record_wallet_flow("provenance", "cosmos1abc", "out", 1000, "HASH", ts=ts)
    w = intel.active_traders("provenance")[0]
    text = format_wallet(w, price_usd=0.03)
    assert "$" in text and "total volume at current price" in text
    store.close()


def test_confidence_word_ranges():
    assert confidence_word(95) == "very high"
    assert confidence_word(75) == "high"
    assert confidence_word(55) == "moderate"
    assert confidence_word(35) == "low"
    assert confidence_word(10) == "very low"


def test_humanize_wallet_churn():
    out = humanize("ethereum", "Active trader wallet: 0xabc bought and sold NUVA 4/7 days")
    assert "trading" in out.lower() or "bot pattern" in out.lower()


# ---- monitor wiring -------------------------------------------------------------

def test_provenance_extract_addresses():
    tx = {"msg": {"fromAddress": "cosmos1a", "toAddress": "cosmos1b"}}
    assert _extract_addresses(tx) == ("cosmos1a", "cosmos1b")
    assert _extract_addresses({}) == (None, None)


def test_provenance_monitor_records_transfer_flows(tmp_path, monkeypatch):
    store = EventStore(tmp_path / "pm.db")
    intel = WalletIntel(Config({}), store)
    ctx = Context(None, State(tmp_path / "s.json"), Config({}), wallets=intel)
    mon = ProvenanceExplorerMonitor(Config({"provenance": {"explorer": {
        "msg_keywords": ["mint"], "priority_denoms": []}}}))

    async def fake_fetch(ctx, url, **kw):
        return 200, {"results": [{
            "txHash": "tx1",
            "msg": {"displayMsgType": "MsgSend", "fromAddress": "cosmos1sender", "toAddress": "cosmos1receiver"},
            "amount": [{"denom": "nhash", "amount": "5000000000"}],  # 5 HASH
        }]}
    monkeypatch.setattr(mon, "fetch", fake_fetch)
    run(mon.poll(ctx))
    flows = store.wallet_daily_flows("provenance", 7)
    assert "cosmos1sender" in flows and "cosmos1receiver" in flows
    store.close()


def test_provenance_monitor_skips_mints_for_wallet_flow(tmp_path, monkeypatch):
    store = EventStore(tmp_path / "pm2.db")
    intel = WalletIntel(Config({}), store)
    ctx = Context(None, State(tmp_path / "s.json"), Config({}), wallets=intel)
    mon = ProvenanceExplorerMonitor(Config({"provenance": {"explorer": {
        "msg_keywords": ["mint"], "priority_denoms": []}}}))

    async def fake_fetch(ctx, url, **kw):
        return 200, {"results": [{
            "txHash": "tx1", "msg": {"displayMsgType": "MsgMint", "fromAddress": "a", "toAddress": "b"},
            "amount": [{"denom": "nhash", "amount": "5000000000"}],
        }]}
    monkeypatch.setattr(mon, "fetch", fake_fetch)
    run(mon.poll(ctx))
    assert store.wallet_daily_flows("provenance", 7) == {}  # mints aren't trading activity
    store.close()


def test_ethereum_monitor_records_wallet_flows(tmp_path, monkeypatch):
    store = EventStore(tmp_path / "em.db")
    intel = WalletIntel(Config({}), store)
    ctx = Context(None, State(tmp_path / "s.json"), Config({}), wallets=intel)
    mon = EtherscanMonitor(Config({"ethereum": {
        "api_key": "k", "token_contracts": ["0xToken"], "min_token_amount": 999999999}}))

    async def fake_etherscan(ctx, **params):
        if params.get("action") == "tokentx":
            return [{"hash": "h1", "from": "0xSender", "to": "0xReceiver",
                    "value": "1000000000000000000", "tokenDecimal": "18", "tokenSymbol": "NUVA"}]
        return []
    monkeypatch.setattr(mon, "_etherscan", fake_etherscan)
    run(mon.poll(ctx))
    flows = store.wallet_daily_flows("ethereum", 7)
    assert "0xsender" in flows and "0xreceiver" in flows
    store.close()


def test_ethereum_monitor_skips_mint_burn_for_wallet_flow(tmp_path, monkeypatch):
    from nuva_bot.monitors.ethereum import ZERO
    store = EventStore(tmp_path / "em2.db")
    intel = WalletIntel(Config({}), store)
    ctx = Context(None, State(tmp_path / "s.json"), Config({}), wallets=intel)
    mon = EtherscanMonitor(Config({"ethereum": {"api_key": "k", "token_contracts": ["0xToken"]}}))

    async def fake_etherscan(ctx, **params):
        if params.get("action") == "tokentx":
            return [{"hash": "h1", "from": ZERO, "to": "0xReceiver",
                    "value": "1000000000000000000", "tokenDecimal": "18", "tokenSymbol": "NUVA"}]
        return []
    monkeypatch.setattr(mon, "_etherscan", fake_etherscan)
    run(mon.poll(ctx))
    assert store.wallet_daily_flows("ethereum", 7) == {}
    store.close()


# ---- pipeline integration ----------------------------------------------------------

def test_pipeline_wallet_scan_sends_clear_alert(tmp_path):
    cfg = Config({"intelligence": {"db_path": str(tmp_path / "p.db"), "ai": {"enabled": False},
                                   "wallets": {"min_days_both": 3, "min_daily_amount": 100}},
                  "alerting": {"digest_interval_seconds": 9999}})
    pipe = IntelligencePipeline(cfg, State(tmp_path / "s.json"), FakeNotifier())
    now = time.time()
    for day_offset in range(4):
        ts = now - day_offset * 86400
        pipe.store.record_wallet_flow("ethereum", "0xTrader", "in", 2000, "NUVA", ts=ts)
        pipe.store.record_wallet_flow("ethereum", "0xTrader", "out", 1500, "NUVA", ts=ts)

    run(pipe._wallet_scan())
    assert len(pipe.notifier.sent) == 1
    text, loud = pipe.notifier.sent[0]
    assert loud is True
    assert "In plain terms" in text
    assert "Wallet Intelligence" in text
    assert "0xtr" in text  # shortened address shown (addresses are stored lowercase)

    # re-scanning within the cooldown should not re-alert
    run(pipe._wallet_scan())
    assert len(pipe.notifier.sent) == 1
    pipe.store.close()


def test_wallets_command_shows_no_data_message(tmp_path):
    cfg = Config({"intelligence": {"db_path": str(tmp_path / "c.db"), "ai": {"enabled": False}}})
    pipe = IntelligencePipeline(cfg, State(tmp_path / "s.json"), FakeNotifier())
    assert pipe.wallets.active_traders("ethereum") == []
    pipe.store.close()

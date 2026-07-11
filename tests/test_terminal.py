"""Tests for v2.3 'Bloomberg terminal' features: ticks, sparklines, /terminal,
/chart, watchlist escalation."""

import asyncio
import time

from nuva_bot.alerts import Alert
from nuva_bot.config import Config
from nuva_bot.intel.events import EventStore
from nuva_bot.intel.pipeline import IntelligencePipeline
from nuva_bot.intel.terminal import sparkline, pct_change, build_terminal, build_chart_text
from nuva_bot.state import State


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def broadcast(self, text, loud=False):
        self.sent.append((text, loud))


def _pipeline(tmp_path):
    cfg = Config({"intelligence": {"db_path": str(tmp_path / "i.db"), "ai": {"enabled": False}},
                  "alerting": {"digest_interval_seconds": 9999}})
    return IntelligencePipeline(cfg, State(tmp_path / "s.json"), FakeNotifier())


# ---- sparkline ---------------------------------------------------------------

def test_sparkline_shape_and_range():
    s = sparkline([1, 2, 3, 4, 5, 6, 7, 8], width=8)
    assert len(s) == 8
    assert s[0] == "▁" and s[-1] == "█"


def test_sparkline_downsamples_long_series():
    s = sparkline(list(range(1000)), width=24)
    assert len(s) == 24


def test_sparkline_flat_and_empty():
    assert set(sparkline([5, 5, 5, 5])) == {"▄"}
    assert sparkline([]) == "···"


def test_pct_change():
    assert pct_change([100, 110]) == 10.0
    assert pct_change([100]) is None


# ---- tick store ----------------------------------------------------------------

def test_ticks_roundtrip_and_min_gap(tmp_path):
    store = EventStore(tmp_path / "t.db")
    now = time.time()
    store.add_tick(1.0, 100.0, ts=now - 600)
    store.add_tick(1.1, 110.0, ts=now - 300)
    store.add_tick(1.2, 120.0, ts=now - 299)  # within min_gap of previous → skipped
    rows = store.ticks(1)
    assert len(rows) == 2
    assert rows[0][1] == 1.0 and rows[-1][1] == 1.1
    store.close()


# ---- terminal screens ------------------------------------------------------------

def test_terminal_renders_with_data(tmp_path):
    pipe = _pipeline(tmp_path)
    now = time.time()
    for i in range(20):
        pipe.store.add_tick(0.03 + i * 0.0001, 1000 + i, ts=now - (20 - i) * 300)
    pipe.state.kv_set("cg:last_price", 0.032)
    pipe.watch_add("tge")
    out = build_terminal(pipe)
    assert "NUVA TERMINAL" in out and "HASH" in out
    assert "RISK" in out and "PREDICT" in out and "WATCH" in out
    assert any(c in out for c in "▁▂▃▄▅▆▇█")
    pipe.store.close()


def test_terminal_renders_without_data(tmp_path):
    pipe = _pipeline(tmp_path)
    out = build_terminal(pipe)
    assert "warming up" in out
    pipe.store.close()


def test_chart_text(tmp_path):
    pipe = _pipeline(tmp_path)
    now = time.time()
    for i in range(30):
        pipe.store.add_tick(1.0 + i * 0.01, 500, ts=now - (30 - i) * 300)
    out = build_chart_text(pipe, 24)
    assert "HASH" in out and "high $" in out and "low $" in out
    pipe.store.close()


def test_chart_text_empty(tmp_path):
    pipe = _pipeline(tmp_path)
    assert "Not enough price history" in build_chart_text(pipe, 24)
    pipe.store.close()


# ---- watchlist ---------------------------------------------------------------------

def test_watchlist_escalates_matching_event(tmp_path):
    pipe = _pipeline(tmp_path)
    pipe.watch_add("genesis pass")
    alert = Alert(monitor="Reddit", layer="social",
                  title="someone mentioned the genesis pass program", priority="kw")
    ev = run(pipe.process(alert, []))
    assert ev.priority == "high"
    assert any(t.startswith("watch:") for t in ev.tags)
    # and the outgoing alert shows the star
    assert any("Watchlist match" in t for t, _ in pipe.notifier.sent)
    pipe.store.close()


def test_watchlist_add_remove(tmp_path):
    pipe = _pipeline(tmp_path)
    assert pipe.watch_add("TGE ") == ["tge"]
    assert pipe.watch_add("tge") == ["tge"]          # no dup
    assert pipe.watch_remove("tge") == []
    pipe.store.close()

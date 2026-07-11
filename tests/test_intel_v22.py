"""Tests for v2.2: multi-user access, AI copilot fallback, activity radar."""

import asyncio
import time

from nuva_bot.alerts import Alert
from nuva_bot.config import Config
from nuva_bot.intel.copilot import Copilot
from nuva_bot.intel.events import Event
from nuva_bot.intel.pipeline import IntelligencePipeline
from nuva_bot.state import State


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def broadcast(self, text, loud=False):
        self.sent.append((text, loud))


def _pipeline(tmp_path):
    cfg = Config({"intelligence": {"db_path": str(tmp_path / "i.db"),
                                   "ai": {"enabled": False},
                                   "copilot": {"enabled": False}},
                  "alerting": {"digest_interval_seconds": 9999}})
    return IntelligencePipeline(cfg, State(tmp_path / "s.json"), FakeNotifier())


# ---- allowed chat ids parsing (comma-separated env string) --------------------

def test_allowed_ids_parse_comma_separated():
    from nuva_bot.commands import CommandBot

    class FakeScheduler:
        statuses = {}
        started_at = time.time()

    cfg = Config({"telegram": {"allowed_chat_ids": "123, 8762044154 ,-100999"}})
    bot = CommandBot(client=None, state=State("/tmp/none-x.json"),
                     scheduler=FakeScheduler(), config=cfg)
    assert bot.allowed_ids == {123, 8762044154, -100999}


def test_allowed_ids_empty_string_means_none():
    from nuva_bot.commands import CommandBot

    class FakeScheduler:
        statuses = {}
        started_at = time.time()

    cfg = Config({"telegram": {"allowed_chat_ids": ""}})
    bot = CommandBot(client=None, state=State("/tmp/none-y.json"),
                     scheduler=FakeScheduler(), config=cfg)
    assert bot.allowed_ids == set()


# ---- copilot -----------------------------------------------------------------

def test_copilot_fallback_answers_from_data(tmp_path):
    pipe = _pipeline(tmp_path)
    pipe.store.add(Event(monitor="News", layer="news",
                         title="NUVA listing rumor gains steam", confidence=80, priority="high"))
    cop = Copilot(Config({"intelligence": {"copilot": {"enabled": False}}}), pipe)
    answer = run(cop.answer("what happened today?"))
    assert "listing rumor" in answer.lower() or "signal" in answer.lower()
    pipe.store.close()


def test_copilot_empty_question_prompts(tmp_path):
    pipe = _pipeline(tmp_path)
    cop = Copilot(Config({"intelligence": {"copilot": {"enabled": False}}}), pipe)
    answer = run(cop.answer("   "))
    assert "Ask me" in answer
    pipe.store.close()


def test_copilot_context_includes_risk_and_predictions(tmp_path):
    pipe = _pipeline(tmp_path)
    cop = Copilot(Config({}), pipe)
    ctx = cop._context()
    assert "Risk panel" in ctx and "Predictions" in ctx
    pipe.store.close()


# ---- activity radar -----------------------------------------------------------

def test_radar_flags_surge(tmp_path):
    pipe = _pipeline(tmp_path)
    now = time.time()
    # baseline: 40 social events spread over the past week (≈0.24/hour)
    for i in range(40):
        pipe.store.add(Event(monitor="X (Twitter)", layer="social",
                             title=f"old chatter {i}", ts=now - 3600 * (10 + i * 4)))
    # surge: 8 events within the last hour
    for i in range(8):
        pipe.store.add(Event(monitor="X (Twitter)", layer="social",
                             title=f"burst {i}", ts=now - 60 * i))
    run(pipe._radar_scan(min_events=5, multiplier=4.0))
    assert any("Unusual surge" in t for t, _ in pipe.notifier.sent)
    # and it doesn't double-fire within the cooldown
    sent_before = len(pipe.notifier.sent)
    run(pipe._radar_scan(min_events=5, multiplier=4.0))
    assert len(pipe.notifier.sent) == sent_before
    pipe.store.close()


def test_radar_quiet_without_baseline(tmp_path):
    pipe = _pipeline(tmp_path)
    now = time.time()
    for i in range(6):  # events exist but no week-long history (n_week < 30)
        pipe.store.add(Event(monitor="News", layer="news", title=f"x{i}", ts=now - 60 * i))
    run(pipe._radar_scan(min_events=5, multiplier=4.0))
    assert pipe.notifier.sent == []
    pipe.store.close()

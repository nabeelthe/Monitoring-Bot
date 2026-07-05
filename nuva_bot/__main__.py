"""Entrypoint.

  python -m nuva_bot                  run the bot (needs TELEGRAM_BOT_TOKEN)
  python -m nuva_bot --check-config   validate config + list monitors, no network
  python -m nuva_bot --once           poll every monitor once, print to stdout
"""

import argparse
import asyncio
import logging
import os
import signal
import sys

import aiohttp

from . import __version__
from .alerts import AlertEngine
from .commands import CommandBot
from .config import Config
from .monitors import build_monitors
from .scheduler import Scheduler
from .state import State
from .telegram import TelegramClient, TelegramError, Notifier

log = logging.getLogger("nuva")


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="nuva_bot", description="Nuva Labs Telegram monitoring bot")
    p.add_argument("--config", default=os.environ.get("NUVA_CONFIG", "config.yaml"))
    p.add_argument("--state", default=os.environ.get("NUVA_STATE", "data/state.json"))
    p.add_argument("--check-config", action="store_true", help="validate config and exit")
    p.add_argument("--once", action="store_true", help="poll all monitors once, print alerts to stdout, exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def check_config(config: Config) -> int:
    monitors = build_monitors(config)
    engine = AlertEngine(config)
    print(f"nuva_bot v{__version__} — config OK")
    print(f"escalation keywords: {', '.join(engine.escalation) or '(none!)'}")
    print(f"relevance keywords:  {', '.join(engine.relevance) or '(none!)'}")
    print(f"\n{len(monitors)} monitors enabled:")
    for m in monitors:
        print(f"  🟢 {m.name:<28} [{m.layer}] every {m.cadence}s")
    token = str(config.get("telegram.bot_token", "") or "").strip()
    print(f"\ntelegram token: {'✅ set' if token else '❌ MISSING (set TELEGRAM_BOT_TOKEN)'}")
    return 0


async def run_once(config: Config, state_path: str) -> int:
    from .monitors.base import Context

    state = State(state_path)
    monitors = build_monitors(config)
    engine = AlertEngine(config)
    failures = 0
    async with aiohttp.ClientSession() as session:
        ctx = Context(session, state, config)
        for m in monitors:
            try:
                alerts = await m.poll(ctx)
            except Exception as exc:
                failures += 1
                print(f"❌ {m.name}: {exc}")
                continue
            kept = []
            for a in alerts:
                v = engine.evaluate(a)
                if v.send:
                    kept.append((a, v))
            print(f"🟢 {m.name}: {len(alerts)} new item(s), {len(kept)} would alert")
            for a, v in kept[:5]:
                print(f"    {'🚨' if v.loud else '·'} {a.title}")
    state.save(force=True)
    return 0 if failures == 0 else 1


async def run_bot(config: Config, state_path: str) -> int:
    token = str(config.get("telegram.bot_token", "") or "").strip()
    if not token:
        log.error("TELEGRAM_BOT_TOKEN is not set — cannot start. Use --once for a dry run.")
        return 2

    state = State(state_path)

    default_chat = str(config.get("telegram.chat_id", "") or "").strip()
    if default_chat.lstrip("-").isdigit():
        state.add_chat(int(default_chat), "configured chat")

    monitors = build_monitors(config)
    if not monitors:
        log.error("no monitors enabled — check config.yaml")
        return 2
    engine = AlertEngine(config)

    async with aiohttp.ClientSession() as session:
        client = TelegramClient(token, session)
        notifier = Notifier(client, state)

        pipeline = None
        if config.getbool("intelligence.enabled", True):
            from .intel.pipeline import IntelligencePipeline
            pipeline = IntelligencePipeline(config, state, notifier)

        scheduler = Scheduler(monitors, engine, notifier, state, session, config, pipeline=pipeline)
        if pipeline is not None:
            pipeline.monitor_statuses = scheduler.statuses
        commands = CommandBot(client, state, scheduler, config, pipeline=pipeline)

        dashboard = None
        if pipeline is not None and config.getbool("dashboard.enabled", True):
            from .dashboard import Dashboard
            dashboard = Dashboard(config, pipeline, scheduler, state)

        try:
            await commands.start()  # also validates the token via getMe
        except TelegramError as exc:
            log.error("Telegram rejected the bot token: %s — check TELEGRAM_BOT_TOKEN.", exc)
            return 2
        scheduler.start()
        if pipeline is not None:
            pipeline.start()
        if dashboard is not None:
            try:
                await dashboard.start()
            except OSError as exc:
                log.warning("dashboard could not start (%s) — continuing without it", exc)
                dashboard = None
        log.info("platform running: %d monitors, intelligence=%s, %d chat(s)",
                 len(monitors), "on" if pipeline else "off", len(state.chats))

        if state.chats:
            await notifier.broadcast(
                f"🤖 <b>Nuva Intelligence Platform v{__version__} online</b>\n"
                f"{len(monitors)} collectors · intelligence pipeline {'active' if pipeline else 'off'}"
                + (f" · dashboard :{dashboard.port}" if dashboard else "")
                + "\n/intelligence /risk /report — /help for everything.",
                loud=False,
            )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        await stop.wait()
        log.info("shutting down…")
        await commands.stop()
        await scheduler.stop()
        if dashboard is not None:
            await dashboard.stop()
        if pipeline is not None:
            await pipeline.stop()
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    try:
        config = Config.load(args.config)
    except (OSError, ValueError) as exc:
        print(f"cannot load config {args.config}: {exc}", file=sys.stderr)
        return 2
    if args.check_config:
        return check_config(config)
    if args.once:
        return asyncio.run(run_once(config, args.state))
    return asyncio.run(run_bot(config, args.state))


if __name__ == "__main__":
    raise SystemExit(main())

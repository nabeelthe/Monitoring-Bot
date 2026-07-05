"""Telegram command handling: /start /help /status /sources /price /mute /unmute /check /test."""

import asyncio
import html
import logging
import time

from . import __version__
from .telegram import TelegramClient, TelegramError

log = logging.getLogger("nuva.commands")

HELP = """<b>Nuva Intelligence Platform</b> — commands

<b>Intelligence</b>
/intelligence — top signals + stories right now
/risk — 7-dimension risk panel
/predict — probability estimates with evidence
/report <i>[morning|daily|weekly]</i> — executive brief
/history <i>[hours]</i> — recent event memory
/search <i>text</i> — query the full event history
/digest — flush pending medium-priority digest now

<b>Market & ops</b>
/status — health of every collector
/sources — the monitoring route map
/price — HASH price right now
/check <i>name</i> — poll one collector immediately
/mute <i>minutes</i> · /unmute — notification sound off/on
/test — test alert · /help — this message"""


def _fmt_ago(ts: float) -> str:
    if not ts:
        return "never"
    delta = int(time.time() - ts)
    if delta < 90:
        return f"{delta}s ago"
    if delta < 5400:
        return f"{delta // 60}m ago"
    return f"{delta // 3600}h ago"


class CommandBot:
    def __init__(self, client: TelegramClient, state, scheduler, config, pipeline=None):
        self.client = client
        self.state = state
        self.scheduler = scheduler
        self.config = config
        self.pipeline = pipeline
        self.username = ""
        allowed = config.getlist("telegram.allowed_chat_ids")
        self.allowed_ids = {int(x) for x in allowed if str(x).strip().lstrip("-").isdigit()}
        self._task: asyncio.Task | None = None

    async def start(self):
        me = await self.client.get_me()
        self.username = me.get("username", "")
        self._task = asyncio.create_task(self._loop(), name="tg-commands")
        log.info("command bot online as @%s", self.username)

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        offset = None
        while True:
            try:
                updates = await self.client.get_updates(offset)
            except TelegramError as exc:
                log.warning("getUpdates failed: %s", exc)
                await asyncio.sleep(5)
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("getUpdates crashed")
                await asyncio.sleep(5)
                continue
            for update in updates or []:
                try:
                    offset = update["update_id"] + 1
                    await self._handle(update.get("message") or {})
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("command handling failed")

    def _authorized(self, chat_id: int) -> bool:
        if any(c["id"] == chat_id for c in self.state.chats):
            return True
        if self.allowed_ids:
            return chat_id in self.allowed_ids
        return not self.state.chats  # first chat to talk to the bot becomes the owner

    async def _handle(self, message: dict):
        text = str(message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not text.startswith("/") or chat_id is None:
            return

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        if "@" in cmd:
            cmd, target = cmd.split("@", 1)
            if self.username and target.lower() != self.username.lower():
                return  # command aimed at another bot in a group
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not self._authorized(chat_id):
            await self._reply(chat_id, "⛔ This bot is private. Ask the owner to add your chat id to <code>telegram.allowed_chat_ids</code>.")
            return

        if cmd == "/start":
            title = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
            if self.state.add_chat(chat_id, title):
                self.state.save()
                await self._reply(chat_id, f"✅ Registered. All Nuva Labs monitoring alerts will arrive here.\n\n{HELP}")
            else:
                await self._reply(chat_id, "Already registered. " + HELP)
        elif cmd == "/help":
            await self._reply(chat_id, HELP)
        elif cmd == "/status":
            await self._reply(chat_id, self._status_text())
        elif cmd == "/sources":
            await self._reply(chat_id, self._sources_text())
        elif cmd == "/price":
            await self._price(chat_id)
        elif cmd == "/mute":
            minutes = 60.0
            try:
                if arg:
                    minutes = float(arg)
            except ValueError:
                pass
            self.state.set_mute(minutes)
            self.state.save()
            await self._reply(chat_id, f"🔇 Muted for {minutes:g} min — alerts keep arriving silently.")
        elif cmd == "/unmute":
            self.state.set_mute(0)
            self.state.save()
            await self._reply(chat_id, "🔊 Notifications back on.")
        elif cmd == "/check":
            await self._check(chat_id, arg)
        elif cmd in ("/intelligence", "/intel"):
            await self._intelligence(chat_id)
        elif cmd == "/risk":
            await self._risk(chat_id)
        elif cmd == "/predict":
            await self._predict(chat_id)
        elif cmd == "/report":
            await self._report(chat_id, arg or "daily")
        elif cmd == "/history":
            await self._history(chat_id, arg)
        elif cmd == "/search":
            await self._search(chat_id, arg)
        elif cmd == "/digest":
            await self._digest(chat_id)
        elif cmd == "/test":
            await self._reply(chat_id, "🧪 <b>Test alert</b> — delivery works. This is what monitoring alerts look like.")
        else:
            await self._reply(chat_id, "Unknown command. " + HELP)

    async def _reply(self, chat_id: int, text: str):
        try:
            await self.client.send_message(chat_id, text)
        except TelegramError as exc:
            log.error("reply to %s failed: %s", chat_id, exc)

    def _status_text(self) -> str:
        sch = self.scheduler
        up = int(time.time() - sch.started_at)
        lines = [
            f"<b>Nuva Monitoring Bot v{__version__}</b>",
            f"Uptime: {up // 3600}h {(up % 3600) // 60}m · "
            f"{'🔇 muted' if self.state.is_muted() else '🔔 live'} · "
            f"chats: {len(self.state.chats)}",
            "",
        ]
        for st in sch.statuses.values():
            m = st.monitor
            if st.consecutive_failures >= 5:
                icon = "🔴"
            elif st.consecutive_failures > 0:
                icon = "🟡"
            else:
                icon = "🟢"
            line = f"{icon} <b>{html.escape(m.name)}</b> · every {m.cadence // 60 or 1}m · last ok {_fmt_ago(st.last_ok)} · sent {st.alerts_sent}"
            if st.last_error:
                line += f"\n   ↳ <i>{html.escape(st.last_error[:120])}</i>"
            lines.append(line)
        return "\n".join(lines)

    def _sources_text(self) -> str:
        lines = ["<b>Monitoring route map</b> (config.yaml is source of truth)", ""]
        for st in self.scheduler.statuses.values():
            m = st.monitor
            lines.append(f"• <b>{html.escape(m.name)}</b> [{m.layer}] — every {m.cadence // 60 or 1} min")
        lines.append("")
        lines.append("⚡ Escalation keywords: " + html.escape(", ".join(self.scheduler.engine.escalation)))
        return "\n".join(lines)

    async def _price(self, chat_id: int):
        cg = next((st.monitor for st in self.scheduler.statuses.values() if st.monitor.name == "CoinGecko HASH"), None)
        if cg is None:
            await self._reply(chat_id, "CoinGecko monitor is disabled.")
            return
        try:
            q = await cg.fetch_quote(self.scheduler.ctx)
        except Exception as exc:
            await self._reply(chat_id, f"Couldn't fetch price: {html.escape(str(exc)[:200])}")
            return
        change = q.get("price_change_percentage_24h") or 0.0
        await self._reply(
            chat_id,
            f"📈 <b>HASH</b>: ${q.get('current_price') or 0:,.6f} ({change:+.2f}% 24h)\n"
            f"Vol 24h: ${q.get('total_volume') or 0:,.0f} · MCap: ${q.get('market_cap') or 0:,.0f}",
        )

    # ---- intelligence commands -------------------------------------------
    def _need_pipeline(self) -> bool:
        return self.pipeline is None

    async def _intelligence(self, chat_id: int):
        if self._need_pipeline():
            return await self._reply(chat_id, "Intelligence layer disabled.")
        p = self.pipeline
        events = [e for e in p.store.recent(24, limit=200) if e.priority != "ignore"]
        events.sort(key=lambda e: (e.confidence, e.ts), reverse=True)
        lines = ["🧠 <b>Intelligence — top signals (24h)</b>", ""]
        if not events:
            lines.append("No signals in the last 24h.")
        for e in events[:8]:
            t = time.strftime("%d %H:%M", time.gmtime(e.ts))
            lines.append(f"• {t} [{e.layer}] <b>{html.escape(e.title[:100])}</b> — {e.confidence}% {e.priority}")
        stories = {}
        for e in events:
            stories.setdefault(e.story_id, []).append(e)
        multi = [(sid, evs) for sid, evs in stories.items() if len({x.layer for x in evs}) >= 2]
        if multi:
            lines.append("")
            lines.append("<b>Active cross-layer stories</b>")
            for sid, evs in sorted(multi, key=lambda kv: -len(kv[1]))[:3]:
                layers = ", ".join(sorted({x.layer for x in evs}))
                lines.append(f"📖 story <code>{sid}</code>: {len(evs)} events across {layers}")
        await self._reply(chat_id, "\n".join(lines))

    async def _risk(self, chat_id: int):
        if self._need_pipeline():
            return await self._reply(chat_id, "Intelligence layer disabled.")
        lines = ["🛡 <b>Risk panel</b>", ""]
        for d in self.pipeline.risk.snapshot(self.scheduler.statuses):
            arrow = {"rising": "↑", "falling": "↓", "flat": "→"}[d.trend]
            icon = "🔴" if d.score >= 60 else ("🟡" if d.score >= 35 else "🟢")
            lines.append(f"{icon} <b>{d.name}</b> {d.score}/100 {arrow}\n   {html.escape(d.detail)}\n   <i>{html.escape(d.recommendation)}</i>")
        await self._reply(chat_id, "\n".join(lines))

    async def _predict(self, chat_id: int):
        if self._need_pipeline():
            return await self._reply(chat_id, "Intelligence layer disabled.")
        lines = ["🔮 <b>Predictions</b> (heuristic, evidence-based)", ""]
        for p in self.pipeline.predictor.all():
            lines.append(f"<b>{p.probability}%</b> — {html.escape(p.name)}")
            for ev in p.evidence:
                lines.append(f"   · {html.escape(ev)}")
        await self._reply(chat_id, "\n".join(lines))

    async def _report(self, chat_id: int, kind: str):
        if self._need_pipeline():
            return await self._reply(chat_id, "Intelligence layer disabled.")
        kind = kind.strip().lower()
        if kind not in ("morning", "afternoon", "evening", "daily", "weekly"):
            kind = "daily"
        await self._reply(chat_id, self.pipeline.reports.brief(kind))

    async def _history(self, chat_id: int, arg: str):
        if self._need_pipeline():
            return await self._reply(chat_id, "Intelligence layer disabled.")
        try:
            hours = float(arg) if arg else 24.0
        except ValueError:
            hours = 24.0
        events = self.pipeline.store.recent(hours, limit=25)
        lines = [f"🗄 <b>Event memory — last {hours:g}h</b> ({len(events)} shown)", ""]
        for e in events:
            t = time.strftime("%d %H:%M", time.gmtime(e.ts))
            lines.append(f"• {t} [{e.layer}/{e.priority}] {html.escape(e.title[:100])}")
        await self._reply(chat_id, "\n".join(lines) if events else "No events in that window.")

    async def _search(self, chat_id: int, arg: str):
        if self._need_pipeline():
            return await self._reply(chat_id, "Intelligence layer disabled.")
        if not arg:
            return await self._reply(chat_id, "Usage: /search <i>text</i>")
        events = self.pipeline.store.search(arg, limit=15)
        lines = [f"🔎 <b>Search: {html.escape(arg)}</b> — {len(events)} hit(s)", ""]
        for e in events:
            t = time.strftime("%Y-%m-%d %H:%M", time.gmtime(e.ts))
            lines.append(f"• {t} [{e.layer}] {html.escape(e.title[:100])}")
        await self._reply(chat_id, "\n".join(lines))

    async def _digest(self, chat_id: int):
        if self._need_pipeline():
            return await self._reply(chat_id, "Intelligence layer disabled.")
        pending = len(self.pipeline._digest)
        await self.pipeline._flush_digest()
        await self._reply(chat_id, f"🗂 Digest flushed ({pending} pending signal(s)).")

    async def _check(self, chat_id: int, arg: str):
        if not arg:
            names = ", ".join(st.monitor.name for st in self.scheduler.statuses.values())
            await self._reply(chat_id, f"Usage: /check <i>name</i>\nMonitors: {html.escape(names)}")
            return
        needle = arg.lower()
        target = next(
            (st.monitor for st in self.scheduler.statuses.values() if needle in st.monitor.name.lower()),
            None,
        )
        if target is None:
            await self._reply(chat_id, f"No monitor matching “{html.escape(arg)}”.")
            return
        await self._reply(chat_id, f"⏳ Polling <b>{html.escape(target.name)}</b>…")
        found, sent, error = await self.scheduler.run_once(target)
        if error:
            await self._reply(chat_id, f"❌ {html.escape(target.name)}: {html.escape(error[:300])}")
        else:
            await self._reply(chat_id, f"✅ {html.escape(target.name)}: {found} new item(s), {sent} alert(s) sent.")

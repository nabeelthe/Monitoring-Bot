"""Intelligence pipeline — every collector alert flows through here.

  Alert → Event → correlate (story) → score (confidence/priority) → store →
  route:
    critical  → AI analyst brief → immediate LOUD alert
    high      → AI analyst brief → immediate alert
    medium    → digest buffer (flushed on an interval, merged by monitor)
    low       → memory only (surfaces in reports)
    ignore    → memory only
  Quiet hours demote high → digest (critical always breaks through).
"""

import asyncio
import html
import logging
import time
from datetime import datetime, timezone

from ..alerts import Alert
from .analyst import Analyst, Brief
from .correlator import Correlator
from .events import Event, EventStore
from .predict import Predictor
from .reports import ReportGenerator, PRIORITY_EMOJI
from .risk import RiskEngine
from .scoring import ConfidenceScorer

log = logging.getLogger("nuva.pipeline")

LAYER_EMOJI = {
    "onchain": "⛓️", "ethereum": "🪙", "market": "📈", "token": "🚀",
    "dev": "🛠️", "blog": "📰", "social": "💬", "news": "🗞️", "system": "⚙️",
}


class IntelligencePipeline:
    def __init__(self, config, state, notifier):
        self.config = config
        self.state = state
        self.notifier = notifier
        db_path = config.get("intelligence.db_path", "data/intel.db")
        self.store = EventStore(db_path)
        self.scorer = ConfidenceScorer(config)
        self.correlator = Correlator(config, self.store)
        self.analyst = Analyst(config)
        self.risk = RiskEngine(self.store)
        self.predictor = Predictor(self.store)
        self.reports = ReportGenerator(self.store, self.risk, self.predictor, state)

        self.digest_interval = config.getint("alerting.digest_interval_seconds", 1800)
        self.quiet_start = config.getint("alerting.quiet_hours_utc.start", -1)
        self.quiet_end = config.getint("alerting.quiet_hours_utc.end", -1)
        # noise reduction
        self.per_source_hourly_cap = config.getint("alerting.per_source_hourly_cap", 8)
        self._repost_window = config.getint("alerting.repost_window_seconds", 21600)  # 6h
        self._recent_fingerprints: dict[str, float] = {}
        self._budget: dict[str, list] = {}
        self._digest: list[Event] = []
        self._digest_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self.counters = {"events": 0, "sent": 0, "digested": 0, "ignored": 0, "ai_briefs": 0}
        # scheduler injects its statuses so the risk engine sees monitor health
        self.monitor_statuses: dict = {}

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        self._tasks.append(asyncio.create_task(self._digest_loop(), name="digest"))
        self._tasks.append(asyncio.create_task(self._report_loop(), name="reports"))
        if self.config.getbool("intelligence.radar.enabled", True):
            self._tasks.append(asyncio.create_task(self._radar_loop(), name="radar"))

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._flush_digest()
        self.store.close()

    # ---- ingestion -------------------------------------------------------
    def in_quiet_hours(self) -> bool:
        if self.quiet_start < 0 or self.quiet_end < 0:
            return False
        h = datetime.now(timezone.utc).hour
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= h < self.quiet_end
        return h >= self.quiet_start or h < self.quiet_end

    async def process(self, alert: Alert, escalation_hits: list, *, record_only: bool = False) -> Event:
        """Full pipeline for one alert. record_only stores without notifying
        (used for baseline runs so history still accumulates)."""
        ev = Event(
            monitor=alert.monitor, layer=alert.layer, title=alert.title,
            body=alert.body, url=alert.url,
            priority_class=alert.priority, escalation_hits=list(escalation_hits),
        )
        self.correlator.assign(ev)
        corroborating = self.correlator.corroboration(ev)
        self.scorer.score(ev, corroborating=len(corroborating))
        self.store.add(ev)
        self.counters["events"] += 1

        if record_only:
            return ev

        # --- noise reduction: collapse reposts / copied announcements ---
        if self._is_repost(ev):
            self.counters["reposts"] = self.counters.get("reposts", 0) + 1
            return ev  # already told the user this story; stay silent

        route = ev.priority
        if route == "high" and self.in_quiet_hours():
            route = "medium"  # digest it; critical still goes out at night

        # --- per-source alert budget: no single source can flood ---
        # critical always passes; high demotes to digest once a source is over budget
        if route == "high" and not self._budget_ok(ev.monitor):
            route = "medium"

        if route == "critical" or route == "high":
            await self._send_immediate(ev)
        elif route == "medium":
            async with self._digest_lock:
                self._digest.append(ev)
            self.counters["digested"] += 1
        else:
            self.counters["ignored"] += 1
        return ev

    # ---- noise reduction helpers -----------------------------------------
    @staticmethod
    def _fingerprint(title: str) -> str:
        import re
        words = re.findall(r"[a-z0-9$]+", title.lower())
        stop = {"the", "a", "an", "is", "to", "of", "on", "in", "for", "and", "new", "now"}
        keep = sorted(w for w in words if w not in stop and len(w) > 2)
        return " ".join(keep[:12])

    def _is_repost(self, ev: Event) -> bool:
        """Suppress near-duplicate content already alerted recently (copied
        announcements, cross-posted news, retweets of the same story)."""
        fp = self._fingerprint(ev.title)
        if not fp:
            return False
        seen = self._recent_fingerprints
        now = ev.ts
        # drop expired entries
        for k in [k for k, t in seen.items() if now - t > self._repost_window]:
            del seen[k]
        if fp in seen:
            seen[fp] = now
            return True
        seen[fp] = now
        return False

    def _budget_ok(self, monitor: str) -> bool:
        now = time.time()
        stamps = [t for t in self._budget.get(monitor, []) if now - t < 3600]
        if len(stamps) >= self.per_source_hourly_cap:
            self._budget[monitor] = stamps
            return False
        stamps.append(now)
        self._budget[monitor] = stamps
        return True

    # ---- immediate intelligence alert -------------------------------------
    async def _send_immediate(self, ev: Event):
        context = self.correlator.story_context(ev)
        brief = await self.analyst.analyze(ev, context)
        if brief.source == "ai":
            self.counters["ai_briefs"] += 1
        text = self.format_alert(ev, brief, context)
        await self.notifier.broadcast(text, loud=(ev.priority == "critical"))
        self.store.mark_sent(ev.id)
        self.counters["sent"] += 1

    def format_alert(self, ev: Event, brief: Brief, context: dict) -> str:
        emoji = LAYER_EMOJI.get(ev.layer, "🔔")
        prio = ev.priority.upper()
        head = "🚨 " if ev.priority == "critical" else ""
        lines = [
            f"{head}{PRIORITY_EMOJI.get(ev.priority, '')} <b>{prio} PRIORITY</b> · {emoji} {html.escape(ev.monitor)}",
            "",
            f"<b>{html.escape(brief.summary[:300])}</b>",
        ]
        if brief.plain_summary:
            lines.append(f"🗣 <b>In plain terms:</b> {html.escape(brief.plain_summary[:350])}")
        lines += [
            html.escape(brief.why_it_matters[:400]),
            "",
            f"Confidence: <b>{ev.confidence}%</b> · Assessment: <b>{brief.assessment}</b>"
            + (" · 🧑‍💻 investigate" if brief.needs_human else ""),
        ]
        cross = context.get("cross_layer") or []
        if cross:
            lines.append("")
            lines.append(f"<b>Correlated signals</b> (story across {len({e.layer for e in cross}) + 1} layers):")
            for e in cross[:4]:
                t = time.strftime("%H:%M", time.gmtime(e.ts))
                lines.append(f"• {t} [{e.layer}] {html.escape(e.title[:90])}")
        if brief.risk_note:
            lines.append("")
            lines.append(f"<b>Risk:</b> {html.escape(brief.risk_note[:300])}")
        lines.append(f"<b>Suggested action:</b> {html.escape(brief.suggested_action[:300])}")
        if brief.monitor_next:
            lines.append("<b>Monitor next:</b> " + html.escape(", ".join(brief.monitor_next[:3])))
        history = context.get("history") or []
        if history:
            dates = ", ".join(time.strftime("%b %d", time.gmtime(e.ts)) for e in history[:3])
            lines.append(f"<b>Historical comparison:</b> similar events on {html.escape(dates)}")
        if ev.escalation_hits:
            lines.append("⚡ " + html.escape(", ".join(ev.escalation_hits)))
        if ev.url:
            lines.append(f'<a href="{html.escape(ev.url, quote=True)}">Open source ↗</a>')
        return "\n".join(lines)

    # ---- digest ------------------------------------------------------------
    async def _digest_loop(self):
        while True:
            await asyncio.sleep(self.digest_interval)
            try:
                await self._flush_digest()
            except Exception:
                log.exception("digest flush failed")

    async def _flush_digest(self):
        async with self._digest_lock:
            batch, self._digest = self._digest, []
        if not batch:
            return
        by_monitor: dict[str, list[Event]] = {}
        for ev in batch:
            by_monitor.setdefault(ev.monitor, []).append(ev)
        lines = [f"🗂 <b>Digest</b> — {len(batch)} medium-priority signal(s)", ""]
        for monitor, events in sorted(by_monitor.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"<b>{html.escape(monitor)}</b> ({len(events)})")
            for ev in events[:4]:
                t = time.strftime("%H:%M", time.gmtime(ev.ts))
                lines.append(f"• {t} {html.escape(ev.title[:100])} ({ev.confidence}%)")
            if len(events) > 4:
                lines.append(f"  …and {len(events) - 4} more")
        await self.notifier.broadcast("\n".join(lines), loud=False)
        for ev in batch:
            self.store.mark_sent(ev.id)

    # ---- activity radar: unusual-surge detection across every layer -----------
    async def _radar_loop(self):
        min_events = self.config.getint("intelligence.radar.min_events", 5)
        multiplier = self.config.getfloat("intelligence.radar.multiplier", 4.0)
        while True:
            await asyncio.sleep(900)
            try:
                await self._radar_scan(min_events, multiplier)
            except Exception:
                log.exception("radar scan failed")

    async def _radar_scan(self, min_events: int, multiplier: float):
        week = self.store.counts_by_layer(24 * 7)
        hour = self.store.counts_by_layer(1)
        for layer, n_hour in hour.items():
            n_week = week.get(layer, 0)
            if n_week < 30:
                continue  # not enough baseline history to judge "unusual" yet
            avg_hourly = n_week / (24 * 7)
            if n_hour < max(min_events, avg_hourly * multiplier):
                continue
            last = float(self.state.kv_get(f"radar:last:{layer}", 0) or 0)
            if time.time() - last < 6 * 3600:
                continue  # already flagged this surge
            self.state.kv_set(f"radar:last:{layer}", time.time())
            factor = n_hour / avg_hourly if avg_hourly > 0 else float(n_hour)
            # A radar hit is a computed cross-source anomaly, not a raw post from
            # this layer — so it skips layer-credibility scoring and goes out
            # directly at high priority.
            ev = Event(
                monitor="Activity Radar",
                layer=layer,
                title=f"Unusual surge: {n_hour} {layer} signals in the last hour (≈{factor:.0f}× normal)",
                body=(f"Typical rate for this layer is about {avg_hourly:.1f} signal(s)/hour "
                      f"over the past week. Sudden surges like this are often the earliest "
                      f"sign that something is happening before any official announcement."),
                priority_class="always",
                confidence=85,
                priority="high",
            )
            self.correlator.assign(ev)
            ev.confidence, ev.priority = 85, "high"  # assign() doesn't score; keep ours
            self.store.add(ev)
            self.counters["events"] += 1
            await self._send_immediate(ev)
            log.info("radar: surge flagged in %s (%d/hr vs %.1f avg)", layer, n_hour, avg_hourly)

    # ---- scheduled reports ---------------------------------------------------
    async def _report_loop(self):
        schedule = self.config.section("reports.schedule")
        if not schedule:
            return
        while True:
            await asyncio.sleep(300)
            try:
                self.risk._cache = None  # refresh with latest monitor health
                self.risk.snapshot(self.monitor_statuses)
                kind = self.reports.due_scheduled_report(schedule)
                if kind:
                    await self.notifier.broadcast(self.reports.brief(kind), loud=False)
                    self.state.save()
            except Exception:
                log.exception("scheduled report failed")

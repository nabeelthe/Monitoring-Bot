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
from . import context as market_context
from .analogs import AnalogEngine
from .analyst import Analyst, Brief
from .correlator import Correlator
from .events import Event, EventStore
from .graph import KnowledgeGraph
from .predict import Predictor
from .reports import ReportGenerator, PRIORITY_EMOJI
from .plain import confidence_word
from .probability import assess
from .risk import RiskEngine
from .scoring import ConfidenceScorer
from .decision import decide
from .outcomes import OutcomeTracker, signal_kind
from .quant import QuantEngine
from .wallets import WalletIntel, format_wallet

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
        self.wallets = WalletIntel(config, self.store)
        self.reports = ReportGenerator(self.store, self.risk, self.predictor, state)
        # quant brain
        self.quant = QuantEngine(self.store)
        self.outcomes = OutcomeTracker(
            self.store, positive_pct=config.getfloat("intelligence.quant.positive_move_pct", 2.0))
        # v4.0 intelligence terminal: analogs, knowledge graph
        self.analogs = AnalogEngine(self.store)
        self.graph = KnowledgeGraph(self.store)

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
        self._tasks.append(asyncio.create_task(self._tick_loop(), name="ticks"))
        if self.wallets.enabled:
            self._tasks.append(asyncio.create_task(self._wallet_loop(), name="wallets"))
        self._tasks.append(asyncio.create_task(self._outcome_loop(), name="outcomes"))

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

        # user watchlist: matching events jump the queue
        watch_hit = self._watch_hit(ev)
        if watch_hit:
            ev.tags.append(f"watch:{watch_hit}")
            if ev.priority in ("medium", "low", "ignore"):
                ev.priority = "high"

        self.store.add(ev)
        self.counters["events"] += 1
        # stamp for forward-return measurement (the bot keeps score on itself)
        self.outcomes.record(ev)
        # mine entities into the knowledge graph (never let it break the pipeline)
        try:
            self.graph.observe(ev)
        except Exception:
            log.exception("graph observe failed")

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

    # ---- watchlist -----------------------------------------------------------
    def _watch_hit(self, ev: Event) -> str | None:
        text = f"{ev.title} {ev.body}".lower()
        for word in self.state.kv_get("watchlist", []):
            if str(word).lower() in text:
                return str(word)
        return None

    def watch_add(self, word: str) -> list:
        wl = [str(w) for w in self.state.kv_get("watchlist", [])]
        word = word.strip().lower()[:40]
        if word and word not in wl:
            wl.append(word)
            self.state.kv_set("watchlist", wl[:20])  # cap at 20 terms
            self.state.save()
        return wl

    def watch_remove(self, word: str) -> list:
        wl = [w for w in self.state.kv_get("watchlist", []) if w != word.strip().lower()]
        self.state.kv_set("watchlist", wl)
        self.state.save()
        return wl

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
            f"📊 Confidence: <b>{ev.confidence}%</b> ({confidence_word(ev.confidence)}) "
            f"· Outlook: <b>{brief.assessment}</b>"
            + (" · 🧑‍💻 worth a human look" if brief.needs_human else ""),
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
        # historical analogs: pattern-matched past situations with MEASURED outcomes
        try:
            report = self.analogs.for_event(ev)
        except Exception:
            report = None
            log.exception("analog lookup failed")
        if report is not None and report.ok:
            lines.append(f"📚 <b>Historical similarity:</b> {html.escape(report.line())}")
        else:
            history = context.get("history") or []
            if history:
                dates = ", ".join(time.strftime("%b %d", time.gmtime(e.ts)) for e in history[:3])
                lines.append(f"<b>Historical comparison:</b> similar events on {html.escape(dates)}")
        # self-measured evidence: what price actually did after signals like this
        evidence = self.outcomes.evidence_line(signal_kind(ev))
        if evidence:
            lines.append(f"📐 {html.escape(evidence)}")
        # broad-market context: is this HASH-specific or just the market moving?
        if ev.layer in ("market", "onchain", "ethereum"):
            mline = market_context.line(market_context.read(self.state))
            if mline:
                lines.append(f"🌐 {html.escape(mline)}")
        # quant read + probability split when the alert is critical
        if ev.priority == "critical" and ev.layer in ("market", "onchain", "ethereum"):
            snap = self.quant.snapshot()
            if snap.ok:
                arrow = {"trending_up": "📈", "trending_down": "📉",
                         "ranging": "➡️", "turbulent": "🌪"}.get(snap.regime, "")
                lines.append(f"{arrow} <b>Quant read:</b> score {snap.score:+d}, market is {snap.regime.replace('_', ' ')}")
            try:
                prob = self.probability()
                lines.append(f"🎲 {html.escape(prob.line())}")
            except Exception:
                log.exception("probability compute failed")
        watch_tags = [t.split(":", 1)[1] for t in ev.tags if t.startswith("watch:")]
        if watch_tags:
            lines.append(f"⭐ <b>Watchlist match:</b> {html.escape(', '.join(watch_tags))}")
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

    # ---- quant brain -----------------------------------------------------------
    def stance(self):
        """Current market stance from the decision engine (quant + events + risk)."""
        return decide(
            self.quant.snapshot(),
            self.store.recent(24, limit=300),
            self.risk.snapshot(self.monitor_statuses),
            self.outcomes.hit_rates(),
        )

    def probability(self):
        """Bull/neutral/bear probability split for the live situation."""
        risk_dims = self.risk.snapshot(self.monitor_statuses)
        risk_hot = any(d.name in ("security", "liquidity") and d.score >= 60 for d in risk_dims)
        return assess(self.stance().score, self.quant.snapshot(), self.analogs.current(),
                      self.outcomes.hit_rates(), risk_hot=risk_hot)

    def research(self, mode: str = "analyst") -> str:
        """The /brief research report at the requested depth."""
        from .brief import build_research  # noqa: PLC0415 (lazy: brief pulls several layers)
        return build_research(self, mode)

    async def _outcome_loop(self):
        while True:
            await asyncio.sleep(900)
            try:
                written = self.outcomes.annotate()
                if written:
                    log.info("outcomes: measured %d forward return(s)", written)
            except Exception:
                log.exception("outcome annotation failed")

    # ---- market tape: sample collector quotes into the time-series store ------
    async def _tick_loop(self):
        while True:
            await asyncio.sleep(300)
            try:
                price = self.state.kv_get("cg:last_price")
                volume = self.state.kv_get("cg:last_volume") or 0.0
                if price:
                    self.store.add_tick(float(price), float(volume))
            except Exception:
                log.exception("tick sampling failed")

    # ---- wallet intelligence: flag daily buy+sell (churn) wallets -------------
    async def _wallet_loop(self):
        while True:
            await asyncio.sleep(self.wallets.scan_interval)
            try:
                await self._wallet_scan()
            except Exception:
                log.exception("wallet scan failed")

    async def _wallet_scan(self):
        """Wallet alerts are deterministic (built straight from real volume
        numbers) and skip the AI analyst overlay — the numbers already ARE
        the analysis, and this keeps the output crisp and jargon-free."""
        price = self.state.kv_get("cg:last_price")
        for chain in ("ethereum", "provenance"):
            for w in self.wallets.active_traders(chain):
                key = f"wallet:flagged:{chain}:{w.wallet}"
                last = float(self.state.kv_get(key, 0) or 0)
                if time.time() - last < 24 * 3600:  # re-flag a given wallet at most once/day
                    continue
                self.state.kv_set(key, time.time())
                # flagged traders enter the knowledge graph as wallet entities
                try:
                    self.graph.observe_flow(chain, w.wallet, "in", time.time())
                except Exception:
                    log.exception("graph flow observe failed")
                price_hint = price if chain == "provenance" else None  # no NUVA market price pre-TGE
                await self._send_wallet_alert(chain, w, price_hint)
        self.state.save()

    async def _send_wallet_alert(self, chain: str, w, price_hint: float | None):
        title = f"🔁 Active trader wallet: {w.short} — {w.days_active}/{w.window_days} days"
        ev = Event(
            monitor="Wallet Intelligence", layer=("ethereum" if chain == "ethereum" else "onchain"),
            title=title, body=f"bought and sold {w.denom} daily; net {w.net:+,.0f} {w.denom}",
            priority_class="always", confidence=90, priority="high",
        )
        self.correlator.assign(ev)
        self.store.add(ev)
        self.counters["events"] += 1

        text = (f"🔁 <b>HIGH PRIORITY</b> · 👛 Wallet Intelligence\n\n"
                f"<b>{html.escape(title)}</b>\n"
                f"🗣 <b>In plain terms:</b> This wallet keeps buying and selling the "
                f"same token, day after day — that's a trading/bot pattern, not "
                f"someone holding long-term.\n\n"
                f"{format_wallet(w, price_hint, explain=False)}")
        await self.notifier.broadcast(text, loud=True)
        self.store.mark_sent(ev.id)
        self.counters["sent"] += 1

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

"""Executive reports: morning/evening briefs, daily summary, weekly intelligence."""

import html
import time
from datetime import datetime, timezone

from .events import EventStore
from .predict import Predictor
from .risk import RiskEngine

PRIORITY_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪", "ignore": "▫️"}


class ReportGenerator:
    def __init__(self, store: EventStore, risk: RiskEngine, predictor: Predictor, state):
        self.store = store
        self.risk = risk
        self.predictor = predictor
        self.state = state

    def _top_events(self, hours: float, n: int = 6) -> list:
        events = self.store.recent(hours, limit=500)
        events.sort(key=lambda e: (e.confidence, e.ts), reverse=True)
        seen_titles = set()
        top = []
        for e in events:
            key = e.title[:60]
            if key in seen_titles or e.priority == "ignore":
                continue
            seen_titles.add(key)
            top.append(e)
            if len(top) >= n:
                break
        return top

    def _market_line(self) -> str:
        price = self.state.kv_get("cg:last_price")
        if price:
            return f"HASH last observed price: ${price:,.6f}"
        return "No market snapshot yet."

    def brief(self, kind: str) -> str:
        hours = {"morning": 12, "afternoon": 6, "evening": 12, "daily": 24, "weekly": 24 * 7}.get(kind, 24)
        now = datetime.now(timezone.utc)
        by_layer = self.store.counts_by_layer(hours)
        by_prio = self.store.counts_by_priority(hours)
        total = sum(by_layer.values())

        lines = [f"📋 <b>{kind.capitalize()} intelligence brief</b> — {now:%Y-%m-%d %H:%M} UTC", ""]

        crit = by_prio.get("critical", 0)
        high = by_prio.get("high", 0)
        headline = f"{total} signal(s) in {hours}h"
        if crit or high:
            headline += f" · 🔴 {crit} critical · 🟠 {high} high"
        lines.append(f"<b>Executive summary:</b> {headline}.")
        lines.append(html.escape(self._market_line()))
        lines.append("")

        top = self._top_events(hours)
        if top:
            lines.append("<b>Biggest events</b>")
            for e in top:
                t = time.strftime("%d %H:%M", time.gmtime(e.ts))
                lines.append(f"{PRIORITY_EMOJI.get(e.priority, '·')} {t} [{e.layer}] {html.escape(e.title[:110])} ({e.confidence}%)")
            lines.append("")

        if by_layer:
            layer_bits = " · ".join(f"{k}: {v}" for k, v in sorted(by_layer.items(), key=lambda kv: -kv[1]))
            lines.append(f"<b>Signal mix:</b> {html.escape(layer_bits)}")
            lines.append("")

        lines.append("<b>Risk panel</b>")
        for d in self.risk.snapshot():
            arrow = {"rising": "↑", "falling": "↓", "flat": "→"}[d.trend]
            bar = "█" * (d.score // 20 + 1)
            lines.append(f"{arrow} {d.name:<12} {d.score:>3} {bar}  <i>{html.escape(d.detail)}</i>")
        lines.append("")

        if kind in ("daily", "weekly", "morning"):
            lines.append("<b>Predictions</b>")
            for p in self.predictor.all():
                lines.append(f"• {p.probability:>2}% — {html.escape(p.name)}")
            lines.append("")

        actions = [d.recommendation for d in self.risk.snapshot() if d.score >= 40]
        if actions:
            lines.append("<b>Action items</b>")
            lines.extend(f"→ {html.escape(a)}" for a in actions[:4])

        return "\n".join(lines)

    # ---- scheduling ------------------------------------------------------
    def due_scheduled_report(self, schedule: dict) -> str | None:
        """schedule: {"morning": 7, "evening": 19, "weekly_day": 0, ...} hours UTC.
        Returns the report kind due now (once per slot per day), else None."""
        now = datetime.now(timezone.utc)
        today = f"{now:%Y-%m-%d}"
        for kind in ("morning", "afternoon", "evening"):
            hour = schedule.get(kind)
            if hour is None:
                continue
            key = f"report:{kind}"
            if now.hour >= int(hour) and self.state.kv_get(key) != today:
                self.state.kv_set(key, today)
                return kind
        weekly_day = schedule.get("weekly_day")
        if weekly_day is not None and now.weekday() == int(weekly_day):
            week = f"{now:%Y-%W}"
            if now.hour >= int(schedule.get("weekly_hour", 8)) and self.state.kv_get("report:weekly") != week:
                self.state.kv_set("report:weekly", week)
                return "weekly"
        return None

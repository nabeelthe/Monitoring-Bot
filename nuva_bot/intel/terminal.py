"""Terminal screen — one dense, Bloomberg-style view of everything, on demand.

Sparklines are Unicode block characters so they render in any Telegram client
with zero image dependencies.
"""

import html
import time
from datetime import datetime, timezone

_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 24) -> str:
    """Compress a series into a fixed-width Unicode sparkline."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return "·" * min(width, 3)
    # downsample to `width` buckets (mean per bucket)
    if len(vals) > width:
        bucket = len(vals) / width
        vals = [
            sum(vals[int(i * bucket):max(int((i + 1) * bucket), int(i * bucket) + 1)])
            / max(len(vals[int(i * bucket):max(int((i + 1) * bucket), int(i * bucket) + 1)]), 1)
            for i in range(width)
        ]
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return _BLOCKS[3] * len(vals)
    return "".join(_BLOCKS[min(int((v - lo) / (hi - lo) * 7.999), 7)] for v in vals)


def pct_change(series: list[float]) -> float | None:
    vals = [v for v in series if v]
    if len(vals) < 2 or vals[0] == 0:
        return None
    return (vals[-1] - vals[0]) / vals[0] * 100


def _arrow(x: float | None) -> str:
    if x is None:
        return "→"
    return "▲" if x > 0.05 else ("▼" if x < -0.05 else "→")


def build_terminal(pipeline, monitor_statuses: dict | None = None) -> str:
    """The /terminal screen: market tape, risk strip, signal flow, stories,
    predictions, watchlist — everything on one screen."""
    p = pipeline
    now = datetime.now(timezone.utc)
    lines = [f"📟 <b>NUVA TERMINAL</b> · {now:%Y-%m-%d %H:%M} UTC", ""]

    # ---- market tape ------------------------------------------------------
    t24 = p.store.ticks(24)
    t7d = p.store.ticks(24 * 7)
    price = p.state.kv_get("cg:last_price")
    prices24 = [x[1] for x in t24]
    vols24 = [x[2] for x in t24]
    chg24 = pct_change(prices24)
    chg7d = pct_change([x[1] for x in t7d])
    if price:
        chg_txt = f" {_arrow(chg24)} {chg24:+.2f}% 24h" if chg24 is not None else ""
        chg7_txt = f" · {chg7d:+.1f}% 7d" if chg7d is not None else ""
        lines.append(f"<b>HASH  ${price:,.6f}</b>{chg_txt}{chg7_txt}")
        if len(prices24) >= 2:
            lo, hi = min(v for v in prices24 if v), max(prices24)
            lines.append(f"24h <code>{sparkline(prices24)}</code>  lo ${lo:,.6f} hi ${hi:,.6f}")
        if len(t7d) >= 2:
            lines.append(f"7d  <code>{sparkline([x[1] for x in t7d])}</code>")
        if any(vols24):
            lines.append(f"vol <code>{sparkline(vols24)}</code>  now ${vols24[-1]:,.0f}" if vols24 and vols24[-1] else
                         f"vol <code>{sparkline(vols24)}</code>")
    else:
        lines.append("<i>Market tape warming up — first ticks arrive within ~10 minutes of startup.</i>")
    lines.append("")

    # ---- risk strip ---------------------------------------------------------
    dims = p.risk.snapshot(monitor_statuses or p.monitor_statuses)
    arrows = {"rising": "↑", "falling": "↓", "flat": "→"}
    strip = " · ".join(f"{d.name[:3]} {d.score}{arrows[d.trend]}" for d in dims)
    worst = max(dims, key=lambda d: d.score, default=None)
    lines.append(f"<b>RISK</b>  {strip}")
    if worst and worst.score >= 40:
        lines.append(f"⚠️ {worst.name}: {html.escape(worst.recommendation)}")
    lines.append("")

    # ---- signal flow ---------------------------------------------------------
    by_prio = p.store.counts_by_priority(24)
    total = sum(v for k, v in by_prio.items() if k != "ignore")
    crit, high = by_prio.get("critical", 0), by_prio.get("high", 0)
    lines.append(f"<b>SIGNALS 24h</b>  {total} total · 🔴 {crit} · 🟠 {high}")
    events = [e for e in p.store.recent(24, limit=200) if e.priority in ("critical", "high")]
    events.sort(key=lambda e: e.ts, reverse=True)
    for e in events[:4]:
        t = time.strftime("%H:%M", time.gmtime(e.ts))
        lines.append(f"• {t} [{e.layer}] {html.escape(e.title[:80])}")
    lines.append("")

    # ---- stories + predictions -----------------------------------------------
    recent = [e for e in p.store.recent(24, limit=300) if e.priority != "ignore"]
    stories: dict[str, set] = {}
    for e in recent:
        stories.setdefault(e.story_id, set()).add(e.layer)
    cross = sum(1 for layers in stories.values() if len(layers) >= 2)
    if cross:
        lines.append(f"<b>STORIES</b>  {cross} active cross-layer narrative(s) — /intelligence for detail")
    preds = sorted(p.predictor.all(), key=lambda x: -x.probability)[:3]
    lines.append("<b>PREDICT</b>  " + " · ".join(f"{x.probability}% {x.name.split(' within')[0][:28]}" for x in preds))

    # ---- watchlist --------------------------------------------------------------
    watch = p.state.kv_get("watchlist", [])
    if watch:
        hits = sum(1 for e in recent for w in watch if w in f"{e.title} {e.body}".lower())
        lines.append(f"<b>WATCH</b>  {', '.join(watch)} — {hits} hit(s) 24h")

    lines.append("")
    lines.append("<i>/chart /risk /predict /intelligence /report — /help for all</i>")
    return "\n".join(lines)


def build_chart_text(pipeline, hours: float = 24) -> str:
    """A taller text chart for /chart — price focus."""
    ticks = pipeline.store.ticks(hours)
    if len(ticks) < 2:
        return ("📉 Not enough price history yet — the tape records a point every "
                "~5 minutes, so charts appear shortly after startup.")
    prices = [t[1] for t in ticks]
    lo, hi = min(p for p in prices if p), max(prices)
    chg = pct_change(prices)
    first_ts, last_ts = ticks[0][0], ticks[-1][0]
    span_h = (last_ts - first_ts) / 3600
    lines = [
        f"📈 <b>HASH — last {span_h:.0f}h</b> ({len(ticks)} points)",
        "",
        f"<code>{sparkline(prices, width=32)}</code>",
        f"<code>{sparkline([t[2] or 0 for t in ticks], width=32)}</code> volume",
        "",
        f"now  ${prices[-1]:,.6f}" + (f"  ({chg:+.2f}%)" if chg is not None else ""),
        f"high ${hi:,.6f} · low ${lo:,.6f}",
    ]
    return "\n".join(lines)

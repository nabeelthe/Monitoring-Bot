"""The Research Brief — Layer 10: everything the desk knows, on one page.

/brief assembles every intelligence layer into one institutional-style read:
Executive Summary → Key Findings → Evidence → Historical Comparison →
Bull/Bear cases → Probability Matrix → Risks → Crowd → Narratives →
Decision Matrix → Confidence. Three depths:

  retail        — plain language, the takeaway only
  analyst       — the full reasoned view (default)
  institutional — maximum depth incl. the Decision Matrix and self-audit

Everything is generated from measured data. Where data is thin the brief says
so instead of filling the gap with confident-sounding noise.
"""

import html
import time

from . import context as market_context
from . import narrative, psychology
from .decision import BULLISH_TAGS, BEARISH_TAGS
from .matrix import build_matrix, format_matrix
from .plain import confidence_word
from .probability import assess

MODES = ("retail", "analyst", "institutional")


def _exec_summary(stance, prob, psych, n_signals: int) -> str:
    tilt = {"bullish": "leans constructive", "bearish": "leans cautious",
            "neutral": "shows no decisive direction"}[stance.stance]
    return (f"The evidence currently {tilt} (bull {prob.bull}% / neutral {prob.neutral}% "
            f"/ bear {prob.bear}% over the next 24h). Crowd behavior reads as "
            f"{psych.state}. {n_signals} signal(s) crossed the desk in the last 24h.")


def _key_findings(events, limit: int = 4) -> list[str]:
    ranked = sorted((e for e in events if e.priority in ("critical", "high", "medium")),
                    key=lambda e: (-e.confidence, -e.ts))
    out = []
    for e in ranked[:limit]:
        t = time.strftime("%H:%M", time.gmtime(e.ts))
        out.append(f"{t} [{e.layer}] {e.title[:90]} — {e.confidence}% ({confidence_word(e.confidence)})")
    return out


def _cases(quant, events, risk_dims, analogs) -> tuple[list[str], list[str]]:
    """Bull case / bear case, each built only from live measured evidence."""
    bull, bear = [], []
    if quant.ok:
        for _name, contrib, text in sorted(quant.factors, key=lambda f: -f[1]):
            if contrib > 0 and len(bull) < 3:
                bull.append(text)
        for _name, contrib, text in sorted(quant.factors, key=lambda f: f[1]):
            if contrib < 0 and len(bear) < 3:
                bear.append(text)
    tags = {t for e in events for t in e.tags}
    bull_hits = sorted(tags & BULLISH_TAGS)
    bear_hits = sorted(tags & BEARISH_TAGS)
    if bull_hits:
        bull.append(f"live catalyst signals in the flow: {', '.join(bull_hits[:4])}")
    if bear_hits:
        bear.append(f"active threat signals in the flow: {', '.join(bear_hits[:3])}")
    if analogs.ok and analogs.avg_24h is not None:
        (bull if analogs.avg_24h >= 0 else bear).append(
            f"historical analogs averaged {analogs.avg_24h:+.1f}% the next day")
    for d in risk_dims:
        if d.score >= 50 and len(bear) < 4:
            bear.append(f"{d.name} risk elevated at {d.score}/100 ({d.detail})")
    if not bull:
        bull.append("no measured bullish evidence right now — that absence is itself information")
    if not bear:
        bear.append("no measured bearish evidence right now")
    return bull[:4], bear[:4]


def build_research(p, mode: str = "analyst") -> str:
    """The /brief screen. `p` is the IntelligencePipeline."""
    mode = mode if mode in MODES else "analyst"
    now = time.strftime("%Y-%m-%d %H:%M", time.gmtime())

    quant = p.quant.snapshot()
    stance = p.stance()
    events = [e for e in p.store.recent(24, limit=300) if e.priority != "ignore"]
    risk_dims = p.risk.snapshot(p.monitor_statuses)
    analogs = p.analogs.current()
    hit_rates = p.outcomes.hit_rates()
    risk_hot = any(d.name in ("security", "liquidity") and d.score >= 60 for d in risk_dims)
    prob = assess(stance.score, quant, analogs, hit_rates, risk_hot=risk_hot)
    psych = psychology.read(quant, p.store)
    mctx = market_context.read(p.state)
    churn = len(p.wallets.active_traders("ethereum")) + len(p.wallets.active_traders("provenance"))

    E = html.escape
    lines = [f"📑 <b>RESEARCH BRIEF</b> · {now} UTC · {mode} mode", ""]

    # ---- executive summary (all modes) ------------------------------------
    lines.append("<b>Executive summary</b>")
    lines.append(E(_exec_summary(stance, prob, psych, len(events))))
    if mctx.ok:
        lines.append(E(market_context.line(mctx)))
    lines.append("")

    # ---- key findings (all modes) -----------------------------------------
    findings = _key_findings(events)
    lines.append("<b>Key findings (24h)</b>")
    if findings:
        lines += [f"• {E(f)}" for f in findings]
    else:
        lines.append("• quiet tape — no material signals in the window")
    lines.append("")

    if mode == "retail":
        lines.append("<b>What it means</b>")
        lines.append(E(f"{psych.emoji} Crowd read: {psych.state}."))
        for r in stance.reasons[:2]:
            lines.append(f"• {E(r)}")
        lines.append(f"<b>Odds:</b> {E(prob.line())}")
        lines.append("")
        lines.append("<i>Data read, not financial advice. /brief analyst for the full view.</i>")
        return "\n".join(lines)

    # ---- analyst depth ------------------------------------------------------
    bull, bear = _cases(quant, events, risk_dims, analogs)
    lines.append("<b>Bull case</b>")
    lines += [f"🟢 {E(b)}" for b in bull]
    lines.append("<b>Bear case</b>")
    lines += [f"🔴 {E(b)}" for b in bear]
    lines.append("")

    lines.append("<b>Historical comparison</b>")
    if analogs.ok:
        lines.append(E(analogs.line()))
        for a in analogs.analogs[:2]:
            d = time.strftime("%b %d", time.gmtime(a.ts))
            ret = f" → {a.ret_24h:+.1f}% next 24h" if a.ret_24h is not None else ""
            lines.append(f"• {d}: {E(a.headline[:80])}{ret}")
    else:
        lines.append(f"<i>{E(analogs.note)}</i>")
    lines.append("")

    lines.append(f"<b>Probability matrix</b>  {E(prob.line())}")
    for d in prob.drivers[:3]:
        lines.append(f"• {E(d)}")
    lines.append("")

    worst = sorted(risk_dims, key=lambda d: -d.score)[:3]
    lines.append("<b>Risks</b>  " + " · ".join(f"{d.name} {d.score}" for d in worst))
    lines.append(psychology.format_read(psych))
    lines.append("")

    if mode == "analyst":
        acc = p.outcomes.accuracy_line()
        if acc:
            lines.append(f"📐 {E(acc)}")
        lines.append("<i>Data read, not financial advice. /brief institutional for the Decision Matrix.</i>")
        return "\n".join(lines)

    # ---- institutional depth ------------------------------------------------
    lines.append(narrative.format_panel(narrative.detect(p.store)))
    lines.append("")
    m = build_matrix(stance, prob, quant, analogs, risk_dims,
                     wallet_churn=churn, hit_sample=sum(s["n"] for s in hit_rates.values()))
    lines.append(format_matrix(m))
    lines.append("")
    acc = p.outcomes.accuracy_line()
    if acc:
        lines.append(f"📐 <b>Self-audit</b>  {E(acc)}")
    lines.append(f"🕸 <b>Knowledge graph</b>  {E(p.graph.summary_line())} — /entity to query")
    return "\n".join(lines)

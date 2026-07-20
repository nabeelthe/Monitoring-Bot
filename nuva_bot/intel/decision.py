"""Decision Engine — "should we move or not?"

Blends the quant score, the last 24h of tagged event flow, the risk panel and
the self-measured hit rates into one stance with a conviction level and the
top reasons in plain language. Always data-not-advice.
"""

import html
from dataclasses import dataclass, field

from .quant import QuantSnapshot

BULLISH_TAGS = {"tge", "airdrop", "listing", "mainnet", "genesis", "mint",
                "release", "partnership", "upgrade", "sale"}
BEARISH_TAGS = {"exploit", "hack", "depeg", "burn"}


@dataclass
class Stance:
    stance: str            # "bullish" | "bearish" | "neutral"
    conviction: str        # "low" | "medium" | "high"
    score: int             # blended -100..100
    reasons: list = field(default_factory=list)
    caveats: list = field(default_factory=list)


def _event_flow_score(events_24h: list) -> tuple[int, str | None]:
    """Net bullish-vs-bearish pressure from tagged signals, confidence-weighted."""
    bull = bear = 0.0
    for e in events_24h:
        if e.priority == "ignore":
            continue
        weight = e.confidence / 100.0
        tags = set(e.tags)
        if tags & BEARISH_TAGS:
            bear += weight          # bad news outranks good in the same event
        elif tags & BULLISH_TAGS:
            bull += weight
    net = bull - bear
    score = int(max(-25, min(net * 8, 25)))
    if bull or bear:
        reason = (f"news flow: {bull:.0f} bullish-leaning vs {bear:.0f} bearish-leaning "
                  f"signal(s) in the last 24h")
        return score, reason
    return 0, None


def decide(quant: QuantSnapshot, events_24h: list, risk_dims: list,
           hit_rates: dict | None = None) -> Stance:
    reasons: list[str] = []
    caveats: list[str] = []
    score = 0.0

    # 1) quant score (60% of the blend)
    if quant.ok:
        score += quant.score * 0.6
        top = sorted(quant.factors, key=lambda f: -abs(f[1]))[:2]
        for _name, contrib, text in top:
            if contrib != 0:
                reasons.append(text)
    else:
        caveats.append(quant.note)

    # 2) event flow (25%)
    flow, flow_reason = _event_flow_score(events_24h)
    score += flow
    if flow_reason:
        reasons.append(flow_reason)

    # 3) risk overrides — a live security/liquidity fire caps the upside
    hot = [d for d in risk_dims if d.name in ("security", "liquidity") and d.score >= 60]
    if hot:
        score = min(score, -10.0)
        reasons.insert(0, f"⚠️ elevated {hot[0].name} risk: {hot[0].detail}")

    # 4) historical evidence tempers conviction, never creates it
    sample = sum(s["n"] for s in (hit_rates or {}).values())
    if sample < 10:
        caveats.append("the bot has measured few past signals so far — conviction is capped until its own track record grows")

    final = int(max(-100, min(score, 100)))
    stance = "bullish" if final >= 15 else ("bearish" if final <= -15 else "neutral")

    if not quant.ok:
        conviction = "low"
    elif abs(final) >= 45 and sample >= 10 and quant.regime != "turbulent":
        conviction = "high"
    elif abs(final) >= 25:
        conviction = "medium"
    else:
        conviction = "low"

    if quant.ok and quant.regime == "turbulent":
        caveats.append("volatility is unusually high, which makes every signal less reliable")

    return Stance(stance=stance, conviction=conviction, score=final,
                  reasons=reasons[:3], caveats=caveats[:2])


def format_stance(st: Stance) -> str:
    """Plain-language, Telegram-ready rendering."""
    icon = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}[st.stance]
    lines = [f"{icon} <b>Quant read: {st.stance.upper()}</b> · conviction {st.conviction} · score {st.score:+d}"]
    for r in st.reasons:
        lines.append(f"• {html.escape(r)}")
    for c in st.caveats:
        lines.append(f"<i>{html.escape(c)}</i>")
    lines.append("<i>This is a data read, not financial advice.</i>")
    return "\n".join(lines)

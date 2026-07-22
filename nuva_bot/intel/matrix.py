"""Decision Intelligence Engine — how a research desk ends a report.

Not "buy/sell" and not even "watch next" — a Decision Matrix: the assessed
current state, its probability, how strong the evidence is, the concrete
conditions that would STRENGTHEN the view, the ones that would INVALIDATE it,
and what remains unknown. Every entry is generated from live measured
conditions (quant factors, risk dimensions, analog stats, wallet flows), so
the matrix changes as reality does.
"""

import html
from dataclasses import dataclass, field

from .analogs import AnalogReport
from .decision import Stance
from .probability import ProbabilityMatrix
from .quant import QuantSnapshot


@dataclass
class DecisionMatrix:
    current_state: str          # e.g. "Accumulation phase", "Distribution risk"
    probability: ProbabilityMatrix
    evidence_strength: str      # "weak" | "moderate" | "strong"
    confirmers: list = field(default_factory=list)
    invalidators: list = field(default_factory=list)
    unknowns: list = field(default_factory=list)


def _phase_label(stance: Stance, quant: QuantSnapshot, wallet_churn: int) -> str:
    """A named market phase from the live reads — always explainable."""
    if quant.ok and quant.regime == "turbulent":
        return "Turbulent — signal quality degraded"
    if stance.stance == "bullish":
        if quant.ok and quant.rsi is not None and quant.rsi >= 75:
            return "Late-stage rally (stretched)"
        return "Accumulation / early uptrend"
    if stance.stance == "bearish":
        if quant.ok and quant.rsi is not None and quant.rsi <= 25:
            return "Capitulation zone"
        return "Distribution / downtrend pressure"
    if wallet_churn >= 2:
        return "Range-bound, trader-dominated tape"
    return "Consolidation / no clear phase"


def build_matrix(stance: Stance, prob: ProbabilityMatrix, quant: QuantSnapshot,
                 analogs: AnalogReport, risk_dims: list,
                 wallet_churn: int = 0, hit_sample: int = 0) -> DecisionMatrix:
    confirmers: list[str] = []
    invalidators: list[str] = []
    unknowns: list[str] = []

    bullish = stance.stance == "bullish"
    bearish = stance.stance == "bearish"

    # ---- confirmers / invalidators from the live quant read ---------------
    if quant.ok:
        if bullish:
            confirmers.append("volume stays above its recent baseline on up-moves")
            confirmers.append("the short-term price average holds above the long-term one")
            invalidators.append("a heavy-volume down day (sellers stepping in with size)")
            if quant.rsi is not None and quant.rsi >= 70:
                invalidators.append(f"RSI is already {quant.rsi:.0f} — a stall here would signal exhaustion")
        elif bearish:
            confirmers.append("bounces keep failing at the short-term price average")
            invalidators.append("a high-volume up day reclaiming the trend averages")
            if quant.rsi is not None and quant.rsi <= 30:
                confirmers.append(f"RSI at {quant.rsi:.0f} — continued selling despite oversold readings")
        else:
            confirmers.append("a decisive break of the recent range on above-normal volume would set the new direction")
            invalidators.append("(neutral view) any confirmed breakout in either direction replaces it")
    else:
        unknowns.append("price tape is still building — quant confirmation unavailable yet")

    # ---- cross-source corroboration ---------------------------------------
    if bullish or bearish:
        confirmers.append("a second independent layer (on-chain, dev, or official) corroborates within 24h")
        invalidators.append("the originating source retracts, or no other layer confirms within 48h")

    # ---- risk dimensions as standing invalidators -------------------------
    for d in risk_dims:
        if d.name in ("security", "liquidity") and d.score >= 60:
            invalidators.insert(0, f"live {d.name} risk stays elevated ({d.detail})")
        elif d.name in ("security", "liquidity"):
            invalidators.append(f"any {d.name} incident (score currently calm at {d.score}/100)")
            break  # one standing risk line is enough when calm

    # ---- analogs ----------------------------------------------------------
    if analogs.ok:
        confirmers.append(
            f"price tracks the analog path (historically {analogs.avg_24h:+.1f}% avg over the next day)")
    else:
        unknowns.append("few measured historical analogs — pattern odds are not yet quotable")

    # ---- wallet intelligence ----------------------------------------------
    if wallet_churn:
        unknowns.append(
            f"{wallet_churn} wallet(s) buying AND selling daily — organic demand vs market-maker flow unresolved")

    # ---- self-measured track record ---------------------------------------
    if hit_sample < 10:
        unknowns.append("the bot's own measured track record is still small — treat conviction as capped")

    # ---- evidence strength -------------------------------------------------
    n_evidence = (2 if quant.ok else 0) + (2 if analogs.ok else 0) + min(hit_sample // 5, 2)
    if stance.conviction == "high" and n_evidence >= 4:
        strength = "strong"
    elif n_evidence >= 2:
        strength = "moderate"
    else:
        strength = "weak"

    return DecisionMatrix(
        current_state=_phase_label(stance, quant, wallet_churn),
        probability=prob,
        evidence_strength=strength,
        confirmers=confirmers[:4],
        invalidators=invalidators[:4],
        unknowns=unknowns[:3],
    )


def format_matrix(m: DecisionMatrix) -> str:
    """Telegram-ready Decision Matrix block."""
    lines = [
        "🎯 <b>Decision Matrix</b>",
        f"State: <b>{html.escape(m.current_state)}</b>",
        f"Odds: {html.escape(m.probability.line())}",
        f"Evidence strength: <b>{m.evidence_strength}</b>",
    ]
    if m.confirmers:
        lines.append("<b>Would strengthen this view:</b>")
        lines += [f"• {html.escape(c)}" for c in m.confirmers]
    if m.invalidators:
        lines.append("<b>Would invalidate it:</b>")
        lines += [f"• {html.escape(c)}" for c in m.invalidators]
    if m.unknowns:
        lines.append("<b>Key unknowns:</b>")
        lines += [f"• {html.escape(c)}" for c in m.unknowns]
    lines.append("<i>Evidence and uncertainty, not a trading call.</i>")
    return "\n".join(lines)

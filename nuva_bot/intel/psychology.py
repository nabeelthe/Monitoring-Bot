"""Market Psychology — Layer 9: what the crowd is doing, inferred from data.

Combines the quant tape (RSI, volume anomaly, trend, volatility), the wallet
flow record, and the social signal rate into ONE named crowd state with the
evidence for it. Deterministic heuristics — each label is only claimed when
its specific measured conditions hold.
"""

import html
import time
from dataclasses import dataclass, field

from .events import EventStore
from .quant import QuantSnapshot


@dataclass
class PsychRead:
    state: str
    emoji: str
    evidence: list = field(default_factory=list)


def _wallet_bias(store: EventStore) -> tuple[float, int]:
    """(net_flow_ratio -1..1, wallets_seen) across all chains, last 7d."""
    total_in = total_out = 0.0
    wallets = set()
    for chain in ("ethereum", "provenance"):
        for wallet, days in store.wallet_daily_flows(chain, days=7).items():
            wallets.add(wallet)
            for d in days.values():
                total_in += d["in"]
                total_out += d["out"]
    turnover = total_in + total_out
    if turnover == 0:
        return 0.0, 0
    return (total_in - total_out) / turnover, len(wallets)


def read(quant: QuantSnapshot, store: EventStore, now: float | None = None) -> PsychRead:
    now = now or time.time()
    social_24h = len(store.recent(24, layer="social")) + len(store.recent(24, layer="news"))
    social_prev = len([e for e in store.recent(48, layer="social") if e.ts < now - 86400])
    social_surge = social_prev > 0 and social_24h >= social_prev * 2
    flow_bias, wallets_seen = _wallet_bias(store)

    ev: list[str] = []

    if not quant.ok:
        return PsychRead(
            state="unreadable — tape too short",
            emoji="⏳",
            evidence=["not enough price history yet to infer crowd behavior"],
        )

    rsi, ret24, volz = quant.rsi, quant.ret_24h, quant.vol_z

    # ordered from most specific to most generic — first match wins
    if rsi is not None and rsi >= 75 and (ret24 or 0) > 0 and social_surge:
        state, emoji = "FOMO / overheating", "🔥"
        ev.append(f"RSI {rsi:.0f} (stretched) while price is up {ret24:+.1f}% and social chatter doubled")
    elif rsi is not None and rsi <= 25 and (ret24 or 0) < 0 and (volz or 0) > 1:
        state, emoji = "capitulation", "🩸"
        ev.append(f"RSI {rsi:.0f} with heavy-volume selling — holders giving up is the classic read")
    elif (ret24 or 0) > 2 and volz is not None and volz < -0.5:
        state, emoji = "exhaustion risk", "😮‍💨"
        ev.append(f"price up {ret24:+.1f}% but volume is fading — fewer buyers behind each leg")
    elif flow_bias > 0.3 and abs(ret24 or 0) < 2 and wallets_seen >= 2:
        state, emoji = "quiet accumulation", "🤫"
        ev.append(f"wallet inflows dominate ({flow_bias:+.0%} net bias across {wallets_seen} tracked wallets) while price stays flat")
    elif flow_bias < -0.3 and wallets_seen >= 2:
        state, emoji = "distribution", "📤"
        ev.append(f"wallet outflows dominate ({flow_bias:+.0%} net bias) — supply is moving out, often toward exchanges")
    elif quant.regime == "turbulent":
        state, emoji = "fear / instability", "🌪"
        ev.append(f"volatility sits in its {quant.volatility_pct:.0f}th percentile — crowds behave erratically here")
    elif quant.regime == "trending_up":
        state, emoji = "constructive momentum", "🙂"
        ev.append("steady uptrend without overheating markers")
    elif quant.regime == "trending_down":
        state, emoji = "risk-off drift", "🙁"
        ev.append("steady downtrend — sellers patient, no capitulation spike yet")
    else:
        state, emoji = "apathy / indecision", "😐"
        ev.append("range-bound tape with no dominant crowd behavior")

    if social_surge and state not in ("FOMO / overheating",):
        ev.append(f"social/news mentions jumped to {social_24h} in 24h (vs {social_prev} the day before)")
    if wallets_seen and state not in ("quiet accumulation", "distribution"):
        ev.append(f"wallet flow bias {flow_bias:+.0%} across {wallets_seen} tracked wallet(s)")

    return PsychRead(state=state, emoji=emoji, evidence=ev[:3])


def format_read(p: PsychRead) -> str:
    lines = [f"{p.emoji} <b>Crowd read: {html.escape(p.state)}</b>"]
    lines += [f"• {html.escape(e)}" for e in p.evidence]
    return "\n".join(lines)

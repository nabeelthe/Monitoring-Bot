"""Narrative Detection — Layer 8: which stories the market is telling.

Counts narrative-family keyword hits across the event memory (trailing 7 days
vs the 7 days before that) to detect which narratives the Nuva/Provenance
ecosystem is entering or leaving. Momentum is a measured ratio, not a feeling.
"""

import html
import time
from dataclasses import dataclass

from .events import EventStore

NARRATIVES: dict[str, tuple[str, ...]] = {
    "RWA / tokenization": ("rwa", "real-world asset", "real world asset", "tokeniz", "tokenis"),
    "Institutional adoption": ("institutional", "custody", "etf", "fund", "asset manager", "bank"),
    "Stablecoins / yield-dollars": ("stablecoin", "usdc", "usdt", "nvusd", "yield-bearing"),
    "DeFi yield": ("yield", "apy", "vault", "staking", "liquidity pool"),
    "Payments": ("payment", "remittance", "settlement"),
    "AI": (" ai ", "artificial intelligence", "ai-powered", "agent"),
    "Regulation": ("sec ", "regulat", "compliance", "license", "mica"),
}


@dataclass
class NarrativeRead:
    name: str
    now: int          # mentions, trailing 7d
    prev: int         # mentions, prior 7d
    direction: str    # "entering" | "leaving" | "steady" | "quiet"


def _mentions(store: EventStore, keywords: tuple, events) -> int:
    n = 0
    for e in events:
        text = f" {e.title} {e.body} ".lower()
        if any(k in text for k in keywords):
            n += 1
    return n


def detect(store: EventStore, now: float | None = None) -> list[NarrativeRead]:
    now = now or time.time()
    recent = store.recent(7 * 24, limit=1500)
    older_all = store.recent(14 * 24, limit=3000)
    cutoff = now - 7 * 86400
    older = [e for e in older_all if e.ts < cutoff]

    reads = []
    for name, kws in NARRATIVES.items():
        cur = _mentions(store, kws, recent)
        prev = _mentions(store, kws, older)
        if cur == 0 and prev == 0:
            direction = "quiet"
        elif cur >= max(prev * 1.5, prev + 2):
            direction = "entering"
        elif prev >= max(cur * 1.5, cur + 2):
            direction = "leaving"
        else:
            direction = "steady"
        reads.append(NarrativeRead(name=name, now=cur, prev=prev, direction=direction))
    reads.sort(key=lambda r: (-r.now, r.name))
    return reads


def format_panel(reads: list[NarrativeRead]) -> str:
    icons = {"entering": "📈 entering", "leaving": "📉 fading",
             "steady": "➡️ steady", "quiet": "·"}
    active = [r for r in reads if r.direction != "quiet"]
    if not active:
        return ("🧭 <b>Narratives</b> — no narrative signals in the last two weeks; "
                "the panel fills as blog/social/news mentions accumulate.")
    lines = ["🧭 <b>Narratives</b> (7d vs prior 7d mentions)"]
    for r in active[:6]:
        lines.append(f"• <b>{html.escape(r.name)}</b>: {r.now} vs {r.prev} — {icons[r.direction]}")
    return "\n".join(lines)

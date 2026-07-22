"""Market Context — Layer 5: never judge a move in isolation.

A HASH move only means something relative to the broad market. The CoinGecko
collector stashes BTC and ETH 24h changes alongside HASH's; this module turns
them into a relative-strength read: is the move token-specific (real signal)
or is the whole market simply moving (beta)?
"""

from dataclasses import dataclass


@dataclass
class MarketContext:
    ok: bool
    note: str = ""
    hash_24h: float | None = None
    btc_24h: float | None = None
    eth_24h: float | None = None
    relative: float | None = None    # HASH minus the BTC/ETH average, in pct-points
    verdict: str = ""                # plain-language classification


def read(state) -> MarketContext:
    hash_c = state.kv_get("cg:hash_24h")
    btc = state.kv_get("cg:btc_24h")
    eth = state.kv_get("cg:eth_24h")
    if hash_c is None or (btc is None and eth is None):
        return MarketContext(ok=False, note="broad-market context not collected yet "
                                            "(fills within one market poll cycle)")
    majors = [x for x in (btc, eth) if x is not None]
    market = sum(majors) / len(majors)
    rel = float(hash_c) - market

    if abs(rel) < 1.5:
        verdict = "moving WITH the market — this looks like broad-market beta, not a token-specific story"
    elif rel > 0:
        verdict = (f"OUTPERFORMING the market by {rel:+.1f} points — "
                   f"strength specific to HASH, worth attributing to a cause")
    else:
        verdict = (f"UNDERPERFORMING the market by {rel:+.1f} points — "
                   f"token-specific weakness even after accounting for the broad move")

    return MarketContext(ok=True, hash_24h=float(hash_c), btc_24h=btc, eth_24h=eth,
                         relative=rel, verdict=verdict)


def line(ctx: MarketContext) -> str | None:
    if not ctx.ok:
        return None
    parts = [f"HASH {ctx.hash_24h:+.1f}%"]
    if ctx.btc_24h is not None:
        parts.append(f"BTC {ctx.btc_24h:+.1f}%")
    if ctx.eth_24h is not None:
        parts.append(f"ETH {ctx.eth_24h:+.1f}%")
    return f"24h: {' · '.join(parts)} → {ctx.verdict}"

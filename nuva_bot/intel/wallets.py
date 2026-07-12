"""Wallet Intelligence — flags wallets that buy AND sell on the same day, repeatedly.

This is a behavioral pattern built from real on-chain in/out transfer volume:
a wallet that receives tokens and sends tokens on the same calendar day, on
several days within the tracking window, looks like an active trader or bot —
not a long-term holder. It's an honest churn signal derived from transfer
flow, not a claim that we've identified a literal DEX swap (unless the
counterparty is a known exchange address you've labeled in config).
"""

import time
from dataclasses import dataclass

from .events import EventStore


@dataclass
class ActiveWallet:
    wallet: str
    chain: str
    days_active: int          # days in the window with BOTH buy and sell activity
    window_days: int
    total_in: float
    total_out: float
    denom: str
    label: str

    @property
    def net(self) -> float:
        return self.total_in - self.total_out

    @property
    def short(self) -> str:
        w = self.wallet
        return w if len(w) <= 14 else f"{w[:6]}…{w[-4:]}"


class WalletIntel:
    def __init__(self, config, store: EventStore):
        self.store = store
        sec = config.section("intelligence.wallets")
        self.enabled = config.getbool("intelligence.wallets.enabled", True)
        self.window_days = float(sec.get("window_days", 7))
        self.min_days_both = int(sec.get("min_days_both", 3))
        # a day only counts as "buy+sell" activity once both sides clear this
        self.min_daily_amount = float(sec.get("min_daily_amount", 100))
        self.scan_interval = int(sec.get("scan_interval_seconds", 1800))
        self.known_labels = {str(k).lower(): str(v) for k, v in (sec.get("known_labels") or {}).items()}
        self.denom_by_chain = {"ethereum": "NUVA", "provenance": "HASH"}

    def record(self, chain: str, wallet: str, direction: str, amount: float, denom: str = ""):
        if not self.enabled:
            return
        self.store.record_wallet_flow(chain, wallet, direction, amount, denom)

    def label_for(self, wallet: str, total_in: float, total_out: float) -> str:
        known = self.known_labels.get(wallet.lower())
        if known:
            return f"🏷 {known}"
        volume = total_in + total_out
        if volume >= 1_000_000:
            return "🐋 Whale-sized activity"
        if volume >= 100_000:
            return "🦈 Large wallet"
        return "👛 Wallet"

    def active_traders(self, chain: str) -> list[ActiveWallet]:
        """Wallets with both inbound and outbound flow >= min_daily_amount on
        at least min_days_both distinct days within the window."""
        flows = self.store.wallet_daily_flows(chain, self.window_days)
        out = []
        for wallet, days in flows.items():
            both_days = [d for d, v in days.items()
                        if v["in"] >= self.min_daily_amount and v["out"] >= self.min_daily_amount]
            if len(both_days) < self.min_days_both:
                continue
            total_in = sum(v["in"] for v in days.values())
            total_out = sum(v["out"] for v in days.values())
            out.append(ActiveWallet(
                wallet=wallet, chain=chain, days_active=len(both_days),
                window_days=int(self.window_days), total_in=total_in, total_out=total_out,
                denom=self.denom_by_chain.get(chain, "tokens"),
                label=self.label_for(wallet, total_in, total_out),
            ))
        out.sort(key=lambda w: -(w.total_in + w.total_out))
        return out


EXPLORER_LINKS = {
    "ethereum": "https://etherscan.io/address/{wallet}",
    "provenance": "https://explorer.provenance.io/accounts/{wallet}",
}


def format_wallet(w: ActiveWallet, price_usd: float | None = None, *, explain: bool = True) -> str:
    """One clear, plain-English block describing a flagged wallet.

    `explain=False` skips the pattern-description sentence — use that when the
    caller already stated it (e.g. the alert's own "In plain terms" line)."""
    direction_word = "buying more than selling" if w.net > 0 else (
        "selling more than buying" if w.net < 0 else "trading roughly even amounts")
    lines = [f"{w.label} <code>{w.short}</code>"]
    if explain:
        lines.append(
            f"Bought and sold {w.denom} on <b>{w.days_active} of the last {w.window_days} days</b> "
            f"— a pattern typical of active traders, not long-term holders.")
    lines += [
        "",
        f"💰 Total bought: <b>{w.total_in:,.0f} {w.denom}</b>",
        f"💸 Total sold: <b>{w.total_out:,.0f} {w.denom}</b>",
        f"📊 Net: <b>{w.net:+,.0f} {w.denom}</b> ({direction_word})",
    ]
    if price_usd:
        gross = (w.total_in + w.total_out) * price_usd
        lines.append(f"≈ <b>${gross:,.0f}</b> total volume at current price")
    link = EXPLORER_LINKS.get(w.chain, "").format(wallet=w.wallet)
    if link:
        lines.append(f'<a href="{link}">View wallet ↗</a>')
    return "\n".join(lines)

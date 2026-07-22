"""Live Knowledge Graph — Layer 3: relationships, not just events.

Every event is mined for entities (wallets, contracts, exchanges, social
handles, repos, organizations, tokens). Entities become graph nodes; entities
appearing in the same event grow a weighted co-occurrence edge. Over time the
graph learns which wallet keeps showing up next to which exchange, which
handle pushes which token, which repo ships before which announcement.

Wallet nodes are enriched from the recorded flow tape: total in/out, active
days, and a behavior label (accumulating / distributing / churning / dormant).
"""

import html
import re
import time

from .events import Event, EventStore

# ---- entity patterns ---------------------------------------------------------
RE_ETH = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
RE_PROV = re.compile(r"\bpb1[a-z0-9]{20,60}\b")
RE_HANDLE = re.compile(r"@([A-Za-z0-9_]{3,15})\b")
RE_REPO = re.compile(r"\b([\w.-]+/[\w.-]+?)(?:\.git)?\b(?=.*(?:commit|release|push|merge|tag))", re.I)

EXCHANGES = ("binance", "coinbase", "kraken", "okx", "bybit", "kucoin",
             "gate.io", "upbit", "bitget", "dlob", "osmosis", "uniswap")
ORGS = ("nuva labs", "nuva finance", "provenance", "figure markets", "figure technologies")
TOKENS = ("hash", "nuva")


def extract_entities(ev: Event) -> list[tuple[str, str, str]]:
    """[(node_id, kind, label), ...] found in one event's title+body."""
    text = f"{ev.title} {ev.body}"
    low = text.lower()
    found: dict[str, tuple[str, str]] = {}

    for m in RE_ETH.findall(text):
        found[m.lower()] = ("wallet", f"{m[:8]}…{m[-6:]}")
    for m in RE_PROV.findall(low):
        found[m] = ("wallet", f"{m[:8]}…{m[-6:]}")
    for m in RE_HANDLE.findall(text):
        found[f"@{m.lower()}"] = ("account", f"@{m}")
    for name in EXCHANGES:
        if name in low:
            found[f"ex:{name}"] = ("exchange", name)
    for name in ORGS:
        if name in low:
            found[f"org:{name.replace(' ', '-')}"] = ("organization", name.title())
    for name in TOKENS:
        if re.search(rf"\b{name}\b", low):
            found[f"tok:{name}"] = ("token", name.upper())
    if ev.layer == "dev":
        m = RE_REPO.search(text)
        if m and "/" in m.group(1):
            found[f"repo:{m.group(1).lower()}"] = ("repository", m.group(1))

    return [(nid, kind, label) for nid, (kind, label) in found.items()][:10]


class KnowledgeGraph:
    def __init__(self, store: EventStore):
        self.store = store

    # ---- ingestion ---------------------------------------------------------
    def observe(self, ev: Event) -> int:
        """Mine one event into the graph. Returns how many entities were found."""
        ents = extract_entities(ev)
        for nid, kind, label in ents:
            self.store.graph_touch_node(nid, kind, label, ev.ts)
        # co-occurrence in one event = a relationship observation
        for i in range(len(ents)):
            for j in range(i + 1, len(ents)):
                self.store.graph_bump_edge(ents[i][0], ents[j][0], ev.ts)
        return len(ents)

    def observe_flow(self, chain: str, wallet: str, direction: str, ts: float):
        """Wallet flow tape also feeds the graph (wallet ↔ its chain's token)."""
        wallet = wallet.lower()
        self.store.graph_touch_node(wallet, "wallet", f"{wallet[:8]}…{wallet[-6:]}", ts)
        tok = "tok:hash" if chain == "provenance" else "tok:nuva"
        self.store.graph_bump_edge(wallet, tok, ts, weight=0.25)

    # ---- wallet behavior ---------------------------------------------------
    def wallet_behavior(self, stats: dict) -> str:
        total_in, total_out = stats.get("total_in") or 0, stats.get("total_out") or 0
        days = stats.get("days_active") or 0
        turnover = total_in + total_out
        if turnover == 0:
            return "dormant"
        if time.time() - (stats.get("last_ts") or 0) > 7 * 86400:
            return "dormant (no flows in 7d)"
        balance_ratio = (total_in - total_out) / turnover
        if days >= 3 and min(total_in, total_out) / max(total_in, total_out) > 0.5:
            return "churning — buys AND sells continuously (trader/bot pattern)"
        if balance_ratio > 0.3:
            return "accumulating — inflows dominate"
        if balance_ratio < -0.3:
            return "distributing — outflows dominate"
        return "balanced two-way flow"

    # ---- profiles ----------------------------------------------------------
    def profile(self, query: str) -> str:
        """Telegram-ready entity profile card, or a not-found message."""
        q = query.strip().lower()
        node = self.store.graph_node(q)
        if node is None:
            matches = self.store.graph_search_nodes(q, limit=5)
            if not matches:
                n, e = self.store.graph_counts()
                return (f"🕸 No entity matching “{html.escape(query)}” in the graph yet "
                        f"({n} entities, {e} relationships mapped so far). The graph "
                        f"grows automatically as events mention wallets, exchanges, "
                        f"accounts and repos.")
            if len(matches) > 1:
                lines = ["🕸 <b>Closest entities:</b>"]
                for m in matches:
                    lines.append(f"• <code>{html.escape(m['id'])}</code> [{m['kind']}] — {m['mentions']} mention(s)")
                lines.append("Run /entity with the exact id for the full profile.")
                return "\n".join(lines)
            node = matches[0]

        lines = [
            f"🕸 <b>{html.escape(node['label'] or node['id'])}</b> · {html.escape(node['kind'])}",
            f"First seen {time.strftime('%b %d %Y', time.gmtime(node['first_ts']))} · "
            f"last {time.strftime('%b %d %H:%M', time.gmtime(node['last_ts']))} UTC · "
            f"{node['mentions']} observation(s)",
        ]

        if node["kind"] == "wallet":
            stats = self.store.wallet_stats(node["id"])
            if stats:
                denom = stats.get("denom") or ""
                lines += [
                    "",
                    f"<b>Flow record</b> ({stats['days_active']} active day(s), {stats['n']} transfer(s))",
                    f"In: {stats['total_in']:,.0f} {denom} · Out: {stats['total_out']:,.0f} {denom} "
                    f"· Net: {(stats['total_in'] - stats['total_out']):+,.0f} {denom}",
                    f"Behavior: <b>{html.escape(self.wallet_behavior(stats))}</b>",
                ]
            else:
                lines.append("<i>No transfer flows recorded for this wallet yet.</i>")

        neighbors = self.store.graph_neighbors(node["id"])
        if neighbors:
            lines.append("")
            lines.append("<b>Connected to</b> (by observed co-occurrence):")
            for n, w in neighbors[:6]:
                lines.append(f"• {html.escape(n['label'] or n['id'])} [{n['kind']}] — strength {w:g}")

        # recent events mentioning this entity, straight from memory
        needle = node["label"] if node["kind"] in ("exchange", "organization", "token") else node["id"]
        evs = self.store.search(str(needle)[:40], limit=3)
        if evs:
            lines.append("")
            lines.append("<b>Recent mentions:</b>")
            for e in evs:
                t = time.strftime("%b %d %H:%M", time.gmtime(e.ts))
                lines.append(f"• {t} [{e.layer}] {html.escape(e.title[:80])}")
        return "\n".join(lines)

    def summary_line(self) -> str:
        n, e = self.store.graph_counts()
        return f"{n} entities · {e} relationships mapped"

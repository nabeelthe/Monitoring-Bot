"""Provenance blockchain monitors: explorer-service tx feed + governance/upgrades."""

import time

from ..alerts import Alert, ALWAYS, ESCALATE
from .base import Monitor, Context

# Structural message types that are inherently significant regardless of amount
# (a mint of any size is news; a tiny transfer usually isn't).
STRUCTURAL = ("mint", "burn", "marker", "vault", "scope", "metadata",
              "addmarker", "finalize", "activate", "issue", "withdraw", "deposit")


def _to_float(v) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def _extract_hash_amount(tx: dict) -> float:
    """Best-effort extraction of a HASH-denominated amount from a tx summary.

    Provenance amounts are usually in nhash (nano-hash, 1 HASH = 1e9 nhash).
    We scan the likely fields defensively — the explorer summary shape varies —
    and return the largest hash-denominated value we can find (0 if none)."""
    best = 0.0
    candidates = []
    msg = tx.get("msg") or {}
    for container in (tx, msg):
        if not isinstance(container, dict):
            continue
        for key in ("amount", "value", "displayAmount", "txValue", "total"):
            candidates.append(container.get(key))
    # amount blocks are often {denom, amount} dicts or lists thereof
    flat = []
    for c in candidates:
        if isinstance(c, dict):
            flat.append(c)
        elif isinstance(c, list):
            flat.extend(x for x in c if isinstance(x, dict))
        elif c is not None:
            flat.append({"denom": "nhash", "amount": c})
    for entry in flat:
        denom = str(entry.get("denom", "")).lower()
        amt = _to_float(entry.get("amount"))
        if amt <= 0:
            continue
        if "nhash" in denom or denom == "":
            amt /= 1e9
        elif denom == "hash":
            pass
        else:
            continue  # non-HASH denom — can't compare on the HASH scale
        best = max(best, amt)
    return best


def _extract_addresses(tx: dict) -> tuple[str | None, str | None]:
    """Best-effort (from, to) address extraction — explorer summary shapes vary."""
    msg = tx.get("msg") or {}
    frm = (msg.get("fromAddress") or msg.get("from") or msg.get("sender")
           or tx.get("fromAddress") or tx.get("sender"))
    to = (msg.get("toAddress") or msg.get("to") or msg.get("recipient")
          or tx.get("toAddress") or tx.get("recipient"))
    return (str(frm) if frm else None, str(to) if to else None)


class ProvenanceExplorerMonitor(Monitor):
    """Big-transaction feed: significant mints/burns, vault issuance, large transfers.

    Only large or structurally-important transactions alert individually; smaller
    matching activity is counted and emitted as one periodic summary so the feed
    never floods."""

    name = "Provenance Explorer"
    layer = "onchain"
    filterable = False

    def __init__(self, config):
        super().__init__(config)
        sec = config.section("provenance.explorer")
        self.cadence = int(sec.get("cadence", 120))
        self.base = str(sec.get("base_url", "https://service-explorer.provenance.io")).rstrip("/")
        self.tx_link = str(sec.get("tx_link", "https://explorer.provenance.io/tx/{hash}"))
        self.msg_keywords = [str(k).lower() for k in sec.get("msg_keywords", [])]
        self.priority_denoms = [str(k).lower() for k in sec.get("priority_denoms", [])]
        self.page_size = int(sec.get("page_size", 50))
        # a transfer must move at least this many HASH to alert on its own
        self.min_hash_amount = float(sec.get("min_hash_amount", 25000))
        # emit the "smaller activity" summary at most this often (seconds)
        self.summary_interval = int(sec.get("summary_interval", 3600))
        if not config.getbool("provenance.explorer.enabled", True):
            self.disable("disabled in config")

    def _significant(self, msg_type: str, blob: str, amount: float) -> tuple[bool, bool]:
        """Return (alert_individually, is_priority)."""
        denom_hit = any(d in blob for d in self.priority_denoms)
        structural = any(s in msg_type.lower() for s in STRUCTURAL)
        big = amount >= self.min_hash_amount
        # Nuva-denom structural events, or any big-enough transaction, alert on their own.
        alert = big or (structural and denom_hit) or (structural and amount > 0)
        return alert, (denom_hit or big)

    async def poll(self, ctx: Context) -> list:
        status, data = await self.fetch(
            ctx, f"{self.base}/api/v2/txs/recent",
            params={"count": self.page_size, "page": 1},
        )
        if data is None:
            raise RuntimeError(f"explorer-service returned HTTP {status}")

        results = data.get("results") or data.get("txs") or []
        interesting = []
        transfer_like = []
        for tx in results:
            if not isinstance(tx, dict):
                continue
            tx_hash = tx.get("txHash") or tx.get("hash") or ""
            msg = tx.get("msg") or {}
            msg_type = str(msg.get("displayMsgType") or msg.get("msgType") or tx.get("type") or "")
            blob = f"{msg_type} {tx.get('monikers')} {tx.get('feepayer')}".lower()
            if any(k in blob for k in self.msg_keywords):
                interesting.append((tx_hash, msg_type, tx, blob))
            # Wallet Intelligence: track genuine P2P transfers (not mints/burns/
            # vault ops, which don't represent trading) for day-trader detection.
            if ("send" in msg_type.lower() or "transfer" in msg_type.lower()) \
                    and not any(s in msg_type.lower() for s in STRUCTURAL):
                transfer_like.append((tx_hash, tx))

        if ctx.wallets and transfer_like:
            fresh_transfers = set(self.new_ids(ctx, [h for h, _ in transfer_like], ns=f"{self.name}:flows"))
            for tx_hash, tx in transfer_like:
                if tx_hash not in fresh_transfers:
                    continue
                frm, to = _extract_addresses(tx)
                amount = _extract_hash_amount(tx)
                if frm and to and amount > 0:
                    ctx.wallets.record("provenance", frm, "out", amount, "HASH")
                    ctx.wallets.record("provenance", to, "in", amount, "HASH")

        fresh = set(self.new_ids(ctx, [h for h, *_ in interesting]))
        alerts = []
        small_count = 0
        small_hash_total = 0.0
        for tx_hash, msg_type, tx, blob in interesting:
            if tx_hash not in fresh:
                continue
            amount = _extract_hash_amount(tx)
            alert_individually, is_priority = self._significant(msg_type, blob, amount)
            if not alert_individually:
                small_count += 1
                small_hash_total += amount
                continue

            block = tx.get("block") or tx.get("height") or "?"
            signers = tx.get("signers") or {}
            signer = ""
            if isinstance(signers, dict):
                addrs = signers.get("signers") or []
                if addrs and isinstance(addrs[0], dict):
                    signer = addrs[0].get("address", "")
                elif addrs:
                    signer = str(addrs[0])
            amt_str = f"{amount:,.0f} HASH" if amount > 0 else "amount n/a"
            body_lines = [f"Type: {msg_type}", f"Size: {amt_str}", f"Block: {block}"]
            if signer:
                body_lines.append(f"Signer: {signer}")
            if tx.get("status") and str(tx["status"]).upper() != "SUCCESS":
                body_lines.append(f"Status: {tx['status']}")
            denom_hit = any(d in blob for d in self.priority_denoms)
            big_tag = "🐋 LARGE " if amount >= self.min_hash_amount else ""
            alerts.append(Alert(
                monitor=self.name,
                layer=self.layer,
                title=f"{big_tag}{'Nuva-denom ' if denom_hit else ''}{msg_type}"
                      + (f" — {amt_str}" if amount > 0 else ""),
                body="\n".join(body_lines),
                url=self.tx_link.format(hash=tx_hash),
                priority=ALWAYS if is_priority else ESCALATE,
                filterable=False,
            ))

        # roll all the small stuff into one throttled summary
        if small_count:
            last = float(ctx.state.kv_get("explorer:last_summary_ts", 0) or 0)
            ctx.state.kv_set("explorer:small_pending", int(ctx.state.kv_get("explorer:small_pending", 0)) + small_count)
            ctx.state.kv_set("explorer:small_hash", float(ctx.state.kv_get("explorer:small_hash", 0)) + small_hash_total)
            if time.time() - last >= self.summary_interval:
                pending = int(ctx.state.kv_get("explorer:small_pending", 0))
                htotal = float(ctx.state.kv_get("explorer:small_hash", 0))
                ctx.state.kv_set("explorer:last_summary_ts", time.time())
                ctx.state.kv_set("explorer:small_pending", 0)
                ctx.state.kv_set("explorer:small_hash", 0.0)
                alerts.append(Alert(
                    monitor=self.name,
                    layer=self.layer,
                    title=f"{pending} smaller on-chain transactions in the last hour",
                    body=(f"Roughly {htotal:,.0f} HASH moved across {pending} smaller "
                          f"transactions below the {self.min_hash_amount:,.0f} HASH alert "
                          f"threshold. Shown as a summary to avoid noise."),
                    priority=ESCALATE, filterable=False,
                ))
        return alerts


class ProvenanceGovMonitor(Monitor):
    """New governance proposals + scheduled chain upgrades (Cosmos LCD)."""

    name = "Provenance Governance"
    layer = "onchain"
    filterable = False

    def __init__(self, config):
        super().__init__(config)
        sec = config.section("provenance.governance")
        self.cadence = int(sec.get("cadence", 1800))
        self.lcd = str(sec.get("lcd_url", "https://api.provenance.io")).rstrip("/")
        self.prop_link = str(sec.get("proposal_link", "https://explorer.provenance.io/proposals/{id}"))
        if not config.getbool("provenance.governance.enabled", True):
            self.disable("disabled in config")

    @staticmethod
    def _proposal_title(prop: dict) -> str:
        for key in ("title",):
            if prop.get(key):
                return str(prop[key])
        content = prop.get("content") or {}
        if isinstance(content, dict) and content.get("title"):
            return str(content["title"])
        msgs = prop.get("messages") or []
        if msgs and isinstance(msgs[0], dict):
            inner = msgs[0].get("content") or {}
            if isinstance(inner, dict) and inner.get("title"):
                return str(inner["title"])
            t = msgs[0].get("@type", "")
            if t:
                return t.rsplit(".", 1)[-1]
        return "Governance proposal"

    async def poll(self, ctx: Context) -> list:
        alerts = []

        status, data = await self.fetch(
            ctx, f"{self.lcd}/cosmos/gov/v1/proposals",
            params={"pagination.limit": "20", "pagination.reverse": "true"},
        )
        if data is None:
            # some LCDs only expose v1beta1
            status, data = await self.fetch(
                ctx, f"{self.lcd}/cosmos/gov/v1beta1/proposals",
                params={"pagination.limit": "20", "pagination.reverse": "true"},
            )
        if data is None:
            raise RuntimeError(f"gov LCD returned HTTP {status}")

        props = data.get("proposals") or []
        by_id = {}
        for p in props:
            pid = str(p.get("id") or p.get("proposal_id") or "")
            if pid:
                by_id[pid] = p
        for pid in self.new_ids(ctx, by_id.keys(), ns=f"{self.name}:props"):
            p = by_id[pid]
            alerts.append(Alert(
                monitor=self.name,
                layer=self.layer,
                title=f"New governance proposal #{pid}: {self._proposal_title(p)}",
                body=f"Status: {p.get('status', 'unknown')}",
                url=self.prop_link.format(id=pid),
                priority=ALWAYS,
                filterable=False,
            ))

        status, plan_data = await self.fetch(ctx, f"{self.lcd}/cosmos/upgrade/v1beta1/current_plan")
        if plan_data is not None:
            plan = plan_data.get("plan")
            if isinstance(plan, dict) and plan.get("name"):
                plan_id = f"upgrade:{plan['name']}:{plan.get('height', '')}"
                if self.new_ids(ctx, [plan_id], ns=f"{self.name}:upgrades"):
                    alerts.append(Alert(
                        monitor=self.name,
                        layer=self.layer,
                        title=f"Chain upgrade scheduled: {plan['name']}",
                        body=f"Upgrade height: {plan.get('height', '?')}\nInfo: {str(plan.get('info', ''))[:300]}",
                        priority=ALWAYS,
                        filterable=False,
                    ))
        return alerts

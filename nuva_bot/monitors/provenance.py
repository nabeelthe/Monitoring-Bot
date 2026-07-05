"""Provenance blockchain monitors: explorer-service tx feed + governance/upgrades."""

from ..alerts import Alert, ALWAYS, ESCALATE
from .base import Monitor, Context


class ProvenanceExplorerMonitor(Monitor):
    """Mint-per-mint feed: markers, mints/burns, nvAsset vault issuance, scope writes."""

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
        if not config.getbool("provenance.explorer.enabled", True):
            self.disable("disabled in config")

    async def poll(self, ctx: Context) -> list:
        status, data = await self.fetch(
            ctx, f"{self.base}/api/v2/txs/recent",
            params={"count": self.page_size, "page": 1},
        )
        if data is None:
            raise RuntimeError(f"explorer-service returned HTTP {status}")

        results = data.get("results") or data.get("txs") or []
        alerts = []
        interesting = []
        for tx in results:
            if not isinstance(tx, dict):
                continue
            tx_hash = tx.get("txHash") or tx.get("hash") or ""
            msg = tx.get("msg") or {}
            msg_type = str(msg.get("displayMsgType") or msg.get("msgType") or tx.get("type") or "")
            blob = f"{msg_type} {tx.get('monikers')} {tx.get('feepayer')}".lower()
            if any(k in blob for k in self.msg_keywords):
                interesting.append((tx_hash, msg_type, tx, blob))

        fresh = set(self.new_ids(ctx, [h for h, *_ in interesting]))
        for tx_hash, msg_type, tx, blob in interesting:
            if tx_hash not in fresh:
                continue
            denom_hit = any(d in blob for d in self.priority_denoms)
            block = tx.get("block") or tx.get("height") or "?"
            signers = tx.get("signers") or {}
            signer = ""
            if isinstance(signers, dict):
                addrs = signers.get("signers") or []
                if addrs and isinstance(addrs[0], dict):
                    signer = addrs[0].get("address", "")
                elif addrs:
                    signer = str(addrs[0])
            body_lines = [f"Type: {msg_type}", f"Block: {block}"]
            if signer:
                body_lines.append(f"Signer: {signer}")
            if tx.get("status") and str(tx["status"]).upper() != "SUCCESS":
                body_lines.append(f"Status: {tx['status']}")
            alerts.append(Alert(
                monitor=self.name,
                layer=self.layer,
                title=f"{'Nuva-denom ' if denom_hit else ''}on-chain activity: {msg_type}",
                body="\n".join(body_lines),
                url=self.tx_link.format(hash=tx_hash),
                priority=ALWAYS if denom_hit else ESCALATE,
                filterable=False,
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

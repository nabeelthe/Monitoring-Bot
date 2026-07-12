"""Etherscan v2 monitor for NUVA ERC-20 contracts: mints, transfers, vault-manager txs."""

from ..alerts import Alert, ALWAYS, ESCALATE
from .base import Monitor, Context

ZERO = "0x0000000000000000000000000000000000000000"


class EtherscanMonitor(Monitor):
    name = "Ethereum / NUVA contracts"
    layer = "ethereum"
    filterable = False

    def __init__(self, config):
        super().__init__(config)
        sec = config.section("ethereum")
        self.cadence = int(sec.get("cadence", 180))
        self.api = str(sec.get("api_url", "https://api.etherscan.io/v2/api")).rstrip("/")
        self.chain_id = str(sec.get("chain_id", 1))
        self.key = str(sec.get("api_key", "") or "").strip()
        self.tokens = [a for a in (sec.get("token_contracts") or []) if a]
        self.contracts = [a for a in (sec.get("watch_contracts") or []) if a]
        self.tx_link = str(sec.get("tx_link", "https://etherscan.io/tx/{hash}"))
        self.min_token_amount = float(sec.get("min_token_amount", 0))
        if not config.getbool("ethereum.enabled", True):
            self.disable("disabled in config")
        elif not self.key:
            self.disable("ETHERSCAN_API_KEY not set")
        elif not (self.tokens or self.contracts):
            self.disable("no NUVA contract addresses configured yet")

    async def _etherscan(self, ctx: Context, **params):
        params.update({"chainid": self.chain_id, "apikey": self.key})
        status, data = await self.fetch(ctx, self.api, params=params)
        if data is None:
            raise RuntimeError(f"etherscan returned HTTP {status}")
        result = data.get("result")
        # Etherscan returns status "0" + message "No transactions found" for empty
        # result sets; treat any other status-0 answer as an error.
        if str(data.get("status")) != "1" and not isinstance(result, list):
            msg = data.get("message") or str(result)
            if "no transactions" in str(msg).lower() or "no records" in str(msg).lower():
                return []
            raise RuntimeError(f"etherscan error: {msg}")
        return result if isinstance(result, list) else []

    @staticmethod
    def _fmt_amount(raw: str, decimals: str) -> str:
        try:
            value = int(raw) / (10 ** int(decimals or 18))
            return f"{value:,.4f}".rstrip("0").rstrip(".")
        except (ValueError, TypeError):
            return raw

    async def poll(self, ctx: Context) -> list:
        alerts = []
        for contract in self.tokens:
            txs = await self._etherscan(
                ctx, module="account", action="tokentx",
                contractaddress=contract, page=1, offset=25, sort="desc",
            )
            by_hash = {}
            for tx in txs:
                key = f"{tx.get('hash')}:{tx.get('logIndex', tx.get('transactionIndex', ''))}"
                by_hash[key] = tx
            for key in self.new_ids(ctx, by_hash.keys(), ns=f"{self.name}:{contract}:token"):
                tx = by_hash[key]
                sender = str(tx.get("from", "")).lower()
                is_mint = sender == ZERO
                is_burn = str(tx.get("to", "")).lower() == ZERO
                amount = self._fmt_amount(tx.get("value", "0"), tx.get("tokenDecimal", "18"))
                try:
                    numeric = float(amount.replace(",", ""))
                except ValueError:
                    numeric = 0.0
                symbol = tx.get("tokenSymbol") or "TOKEN"
                # Wallet Intelligence: record every real wallet-to-wallet transfer
                # (not mint/burn against the zero address) regardless of alert size —
                # day-trader detection needs full activity, not just large transfers.
                if ctx.wallets and numeric > 0 and not is_mint and not is_burn:
                    ctx.wallets.record("ethereum", tx.get("from", ""), "out", numeric, symbol)
                    ctx.wallets.record("ethereum", tx.get("to", ""), "in", numeric, symbol)
                if not (is_mint or is_burn) and numeric < self.min_token_amount:
                    continue
                kind = "MINT 🟢" if is_mint else ("BURN 🔴" if is_burn else "Transfer")
                alerts.append(Alert(
                    monitor=self.name,
                    layer=self.layer,
                    title=f"{symbol} {kind}: {amount} {symbol}",
                    body=f"From: {tx.get('from')}\nTo: {tx.get('to')}",
                    url=self.tx_link.format(hash=tx.get("hash", "")),
                    priority=ALWAYS if is_mint else ESCALATE,  # map: "● on mint"
                    filterable=False,
                ))

        for contract in self.contracts:
            txs = await self._etherscan(
                ctx, module="account", action="txlist",
                address=contract, page=1, offset=25, sort="desc",
            )
            by_hash = {str(tx.get("hash")): tx for tx in txs if tx.get("hash")}
            for h in self.new_ids(ctx, by_hash.keys(), ns=f"{self.name}:{contract}:txs"):
                tx = by_hash[h]
                fn = tx.get("functionName") or tx.get("methodId") or "call"
                failed = str(tx.get("isError")) == "1"
                alerts.append(Alert(
                    monitor=self.name,
                    layer=self.layer,
                    title=f"Vault-manager tx: {str(fn).split('(')[0]}{' ❌ FAILED' if failed else ''}",
                    body=f"Contract: {contract}\nFrom: {tx.get('from')}",
                    url=self.tx_link.format(hash=h),
                    priority=ESCALATE,
                    filterable=False,
                ))
        return alerts

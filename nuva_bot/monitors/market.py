"""Market monitors: CoinGecko HASH price, DefiLlama TVL, Osmosis pool liquidity."""

import statistics
import time
from datetime import datetime, timezone

from ..alerts import Alert, ESCALATE
from .base import Monitor, Context


class CoinGeckoHashMonitor(Monitor):
    """HASH price moves beyond threshold, 24h volume spikes, daily summary."""

    name = "CoinGecko HASH"
    layer = "market"
    filterable = False

    def __init__(self, config):
        super().__init__(config)
        sec = config.section("market.coingecko")
        self.cadence = int(sec.get("cadence", 300))
        self.base = str(sec.get("base_url", "https://api.coingecko.com/api/v3")).rstrip("/")
        self.coin_id = str(sec.get("coin_id", "hash-2"))
        self.move_pct = float(sec.get("price_move_pct", 8))
        self.vol_mult = float(sec.get("volume_spike_multiplier", 3))
        self.summary_hour_utc = int(sec.get("daily_summary_hour_utc", 14))
        self.api_key = str(sec.get("api_key", "") or "").strip()
        if not config.getbool("market.coingecko.enabled", True):
            self.disable("disabled in config")

    async def fetch_quote(self, ctx: Context) -> dict | None:
        headers = {"x-cg-demo-api-key": self.api_key} if self.api_key else None
        status, data = await self.fetch(
            ctx, f"{self.base}/coins/markets",
            params={"vs_currency": "usd", "ids": self.coin_id},
            headers=headers,
        )
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"coingecko returned HTTP {status} / empty for {self.coin_id}")
        return data[0]

    async def poll(self, ctx: Context) -> list:
        q = await self.fetch_quote(ctx)
        price = q.get("current_price")
        change = q.get("price_change_percentage_24h") or 0.0
        volume = q.get("total_volume") or 0.0
        alerts = []

        # --- price move with band hysteresis (re-alert only when the move deepens)
        band = int(abs(change) // self.move_pct) * (1 if change >= 0 else -1) if abs(change) >= self.move_pct else 0
        last_band = ctx.state.kv_get("cg:last_band", 0)
        if band != 0 and band != last_band:
            arrow = "🟢▲" if change > 0 else "🔴▼"
            alerts.append(Alert(
                monitor=self.name, layer=self.layer,
                title=f"HASH big move {arrow} {change:+.2f}% (24h)",
                body=f"Price: ${price:,.6f}\n24h volume: ${volume:,.0f}",
                url=f"https://www.coingecko.com/en/coins/{self.coin_id}",
                priority=ESCALATE, filterable=False,
            ))
        ctx.state.kv_set("cg:last_band", band)

        # --- volume spike vs rolling median
        samples = ctx.state.kv_get("cg:vol_samples", [])
        if len(samples) >= 12 and volume > 0:
            med = statistics.median(samples)
            if med > 0 and volume >= self.vol_mult * med:
                if self.new_ids(ctx, [f"vol:{datetime.now(timezone.utc):%Y-%m-%d}"], ns=f"{self.name}:vol"):
                    alerts.append(Alert(
                        monitor=self.name, layer=self.layer,
                        title=f"HASH volume spike: ${volume:,.0f} ({volume / med:.1f}× median)",
                        body=f"Rolling median: ${med:,.0f}",
                        priority=ESCALATE, filterable=False,
                    ))
        samples = (samples + [volume])[-48:]
        ctx.state.kv_set("cg:vol_samples", samples)

        # --- daily summary
        now = datetime.now(timezone.utc)
        today = f"{now:%Y-%m-%d}"
        if now.hour >= self.summary_hour_utc and ctx.state.kv_get("cg:last_summary") != today:
            ctx.state.kv_set("cg:last_summary", today)
            alerts.append(Alert(
                monitor=self.name, layer=self.layer,
                title=f"HASH daily summary — {today}",
                body=(
                    f"Price: ${price:,.6f} ({change:+.2f}% 24h)\n"
                    f"24h volume: ${volume:,.0f}\n"
                    f"Market cap: ${q.get('market_cap') or 0:,.0f}\n"
                    f"24h range: ${q.get('low_24h') or 0:,.6f} – ${q.get('high_24h') or 0:,.6f}"
                ),
                priority=ESCALATE, filterable=False,
            ))

        ctx.state.kv_set("cg:last_price", price)
        ctx.state.kv_set("cg:last_volume", volume)
        ctx.state.kv_set("cg:last_quote_ts", datetime.now(timezone.utc).timestamp())
        return alerts


class DefiLlamaMonitor(Monitor):
    """Real-world-asset TVL swings on Provenance (and optional protocol slug)."""

    name = "DefiLlama TVL"
    layer = "market"
    filterable = False

    def __init__(self, config):
        super().__init__(config)
        sec = config.section("market.defillama")
        self.cadence = int(sec.get("cadence", 1800))
        self.base = str(sec.get("base_url", "https://api.llama.fi")).rstrip("/")
        self.chain = str(sec.get("chain", "Provenance"))
        self.protocol = str(sec.get("protocol_slug", "") or "").strip()
        self.swing_pct = float(sec.get("swing_pct", 10))
        if not config.getbool("market.defillama.enabled", True):
            self.disable("disabled in config")

    async def _check_swing(self, ctx: Context, label: str, key: str, tvl: float) -> Alert | None:
        last = ctx.state.kv_get(key)
        ctx_alert = None
        if last and last > 0 and tvl > 0:
            delta = (tvl - last) / last * 100
            if abs(delta) >= self.swing_pct:
                arrow = "🟢▲" if delta > 0 else "🔴▼"
                ctx_alert = Alert(
                    monitor=self.name, layer=self.layer,
                    title=f"{label} TVL swing {arrow} {delta:+.1f}%",
                    body=f"TVL: ${tvl:,.0f} (was ${last:,.0f})",
                    priority=ESCALATE, filterable=False,
                )
                ctx.state.kv_set(key, tvl)
        elif not last:
            ctx.state.kv_set(key, tvl)
        return ctx_alert

    async def poll(self, ctx: Context) -> list:
        alerts = []
        status, chains = await self.fetch(ctx, f"{self.base}/v2/chains")
        if isinstance(chains, list):
            for entry in chains:
                if str(entry.get("name", "")).lower() == self.chain.lower():
                    a = await self._check_swing(ctx, f"{self.chain} chain", "llama:chain_tvl", float(entry.get("tvl") or 0))
                    if a:
                        alerts.append(a)
                    break
        else:
            raise RuntimeError(f"defillama returned HTTP {status}")

        if self.protocol:
            status, proto = await self.fetch(ctx, f"{self.base}/tvl/{self.protocol}")
            if proto is not None:
                try:
                    a = await self._check_swing(ctx, self.protocol, "llama:proto_tvl", float(proto))
                    if a:
                        alerts.append(a)
                except (TypeError, ValueError):
                    pass
        return alerts


class OsmosisPoolMonitor(Monitor):
    """HASH/OSMO pool liquidity add / drain. Off until pool id is configured."""

    name = "Osmosis HASH/OSMO pool"
    layer = "market"
    filterable = False

    def __init__(self, config):
        super().__init__(config)
        sec = config.section("market.osmosis")
        self.cadence = int(sec.get("cadence", 600))
        self.lcd = str(sec.get("lcd_url", "https://lcd.osmosis.zone")).rstrip("/")
        self.pool_id = str(sec.get("pool_id", "") or "").strip()
        self.swing_pct = float(sec.get("swing_pct", 15))
        if not config.getbool("market.osmosis.enabled", True):
            self.disable("disabled in config")
        elif not self.pool_id:
            self.disable("pool_id not set (per route map: off until pool id set)")

    async def poll(self, ctx: Context) -> list:
        status, data = await self.fetch(ctx, f"{self.lcd}/osmosis/gamm/v1beta1/pools/{self.pool_id}")
        if data is None:
            raise RuntimeError(f"osmosis LCD returned HTTP {status}")
        pool = data.get("pool") or {}
        assets = pool.get("pool_assets") or pool.get("pool_liquidity") or []
        alerts = []
        for asset in assets:
            token = asset.get("token") or asset
            denom = str(token.get("denom", "?"))
            try:
                amount = float(token.get("amount", 0))
            except (TypeError, ValueError):
                continue
            key = f"osmo:{self.pool_id}:{denom}"
            last = ctx.state.kv_get(key)
            if last and last > 0 and amount > 0:
                delta = (amount - last) / last * 100
                if abs(delta) >= self.swing_pct:
                    arrow = "🟢 add" if delta > 0 else "🔴 drain"
                    alerts.append(Alert(
                        monitor=self.name, layer=self.layer,
                        title=f"Pool liquidity {arrow}: {denom[-12:]} {delta:+.1f}%",
                        body=f"Pool #{self.pool_id} amount: {amount:,.0f}",
                        priority=ESCALATE, filterable=False,
                    ))
                    ctx.state.kv_set(key, amount)
            elif not last:
                ctx.state.kv_set(key, amount)
        return alerts

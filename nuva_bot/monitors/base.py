"""Base class + shared HTTP helpers for all monitors."""

import asyncio
import logging

import aiohttp

log = logging.getLogger("nuva.monitor")

USER_AGENT = "NuvaLabsMonitorBot/1.0 (Telegram alert bot; +https://github.com/nabeelthe/monitoring-bot)"

RETRYABLE = {429, 500, 502, 503, 504}


class FetchError(Exception):
    pass


class Context:
    """Everything a monitor needs during a poll cycle."""

    def __init__(self, session: aiohttp.ClientSession, state, config):
        self.session = session
        self.state = state
        self.config = config


class Monitor:
    name = "base"
    layer = "system"
    cadence = 600  # seconds
    filterable = True

    def __init__(self, config):
        self.config = config
        self.enabled = True
        self.disabled_reason = ""

    def disable(self, reason: str):
        self.enabled = False
        self.disabled_reason = reason

    async def poll(self, ctx: Context) -> list:
        raise NotImplementedError

    # ---- helpers ---------------------------------------------------------
    async def fetch(
        self,
        ctx: Context,
        url: str,
        *,
        kind: str = "json",
        headers: dict | None = None,
        params: dict | None = None,
        timeout: float = 25,
        retries: int = 2,
    ):
        """GET a URL. Returns (status, body) where body is parsed json / text,
        or None when the response wasn't 2xx. Raises FetchError on network failure."""
        hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if headers:
            hdrs.update(headers)
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with ctx.session.get(
                    url,
                    headers=hdrs,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    allow_redirects=True,
                ) as resp:
                    if 200 <= resp.status < 300:
                        if kind == "json":
                            return resp.status, await resp.json(content_type=None)
                        return resp.status, await resp.text(errors="replace")
                    if resp.status in RETRYABLE and attempt < retries:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    return resp.status, None
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise FetchError(f"{self.name}: GET {url} failed: {last_exc}")

    def new_ids(self, ctx: Context, ids, ns: str | None = None) -> list:
        return ctx.state.new_ids(ns or self.name, ids)

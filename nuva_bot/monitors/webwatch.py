"""Generic page watcher: Nuva pages, listing trackers, Genesis Pass portal, blog/docs.

Two modes per page:
  change  — hash the normalized text; alert when it changes (reports which
            escalation keywords are present in the new content).
  listing — page previously 404/empty now returns real content ⇒ probable
            TGE / first market. Loud by design.
"""

import hashlib
import re

from ..alerts import Alert, ALWAYS, ESCALATE, KW
from .base import Monitor, Context

_TAG_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_HTML_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# volatile bits that churn every request and would cause false "page changed"
_NOISE_RES = [
    re.compile(r"(csrf|nonce|token)[\"'=:\s]+[a-zA-Z0-9_\-+/=]{8,}", re.I),
    re.compile(r"\b\d{10,13}\b"),          # unix timestamps
    re.compile(r"[a-f0-9]{16,64}", re.I),  # cache-buster hashes / build ids
]


def normalize(html_text: str) -> str:
    text = _TAG_RE.sub(" ", html_text)
    text = _HTML_RE.sub(" ", text)
    for rx in _NOISE_RES:
        text = rx.sub(" ", text)
    return _WS_RE.sub(" ", text).strip().lower()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


class WebWatchMonitor(Monitor):
    name = "Page Watch"
    layer = "token"

    def __init__(self, config):
        super().__init__(config)
        sec = config.section("webwatch")
        self.cadence = int(sec.get("cadence", 600))
        self.pages = [p for p in (sec.get("pages") or []) if p.get("url")]
        if not config.getbool("webwatch.enabled", True):
            self.disable("disabled in config")
        elif not self.pages:
            self.disable("no pages configured")

    async def poll(self, ctx: Context) -> list:
        alerts = []
        for page in self.pages:
            try:
                alerts.extend(await self._check_page(ctx, page))
            except Exception as exc:  # one broken page must not kill the rest
                key = f"webwatch:errcount:{page['url']}"
                errors = int(ctx.state.kv_get(key, 0)) + 1
                ctx.state.kv_set(key, errors)
                if errors == 5:
                    alerts.append(Alert(
                        monitor=self.name, layer="system",
                        title=f"Page watch failing 5×: {page.get('name', page['url'])}",
                        body=str(exc)[:300], priority=ESCALATE, filterable=False,
                    ))
        return alerts

    async def _check_page(self, ctx: Context, page: dict) -> list:
        url = page["url"]
        name = page.get("name", url)
        mode = page.get("mode", "change")
        layer = page.get("layer", self.layer)
        priority = page.get("priority", ALWAYS if layer == "token" else KW)

        status, body = await self.fetch(ctx, url, kind="text", timeout=30)
        ctx.state.kv_set(f"webwatch:errcount:{url}", 0)
        has_content = status < 400 and body is not None and len(normalize(body)) > 80
        state_key = f"webwatch:{url}"

        if mode == "listing":
            was_live = bool(ctx.state.kv_get(f"{state_key}:live"))
            if has_content and not was_live:
                ctx.state.kv_set(f"{state_key}:live", True)
                first_check = ctx.state.kv_get(f"{state_key}:checked")
                ctx.state.kv_set(f"{state_key}:checked", True)
                if first_check:  # only alert on the 404→content transition, not on baseline
                    return [Alert(
                        monitor=self.name, layer=layer,
                        title=f"LISTING PAGE LIVE: {name}",
                        body="Listing page now returns content — probable TGE / first market.",
                        url=url, priority=ALWAYS, filterable=False,
                    )]
            elif not has_content:
                ctx.state.kv_set(f"{state_key}:checked", True)
                ctx.state.kv_set(f"{state_key}:live", False)
            return []

        # mode == "change"
        if body is None:
            raise RuntimeError(f"HTTP {status} for {url}")
        text = normalize(body)
        new_hash = digest(text)
        old_hash = ctx.state.kv_get(f"{state_key}:hash")
        ctx.state.kv_set(f"{state_key}:hash", new_hash)
        if old_hash is None or old_hash == new_hash:
            return []
        return [Alert(
            monitor=self.name, layer=layer,
            title=f"Page changed: {name}",
            body=f"Content on {url} was updated.",
            url=url,
            priority=priority,
            filterable=False,  # a watched official page changing is always signal
        )]

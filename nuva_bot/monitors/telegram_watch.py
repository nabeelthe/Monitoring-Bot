"""Public Telegram channel monitor.

Reads the public web preview at https://t.me/s/<channel> — no bot membership,
no API token, works for any public channel. Extracts recent message text and
alerts on new posts, keyword-gated to Nuva/Provenance relevance.
"""

import html as _html
import re

from ..alerts import Alert, KW
from .base import Monitor, Context

# each message block: <div class="tgme_widget_message ..." data-post="chan/123">
_POST_RE = re.compile(r'data-post="([^"]+)"')
# message text container
_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(fragment: str) -> str:
    fragment = fragment.replace("<br/>", " ").replace("<br>", " ")
    text = _TAG_RE.sub(" ", fragment)
    text = _html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


class TelegramChannelMonitor(Monitor):
    name = "Telegram channels"
    layer = "social"

    def __init__(self, config):
        super().__init__(config)
        sec = config.section("social.telegram_channels")
        self.cadence = int(sec.get("cadence", 600))
        self.channels = [str(c).lstrip("@") for c in (sec.get("channels") or []) if c]
        self.base = str(sec.get("base_url", "https://t.me/s")).rstrip("/")
        if not config.getbool("social.telegram_channels.enabled", True):
            self.disable("disabled in config")
        elif not self.channels:
            self.disable("no channels configured")

    async def poll(self, ctx: Context) -> list:
        alerts = []
        headers = {"User-Agent": "Mozilla/5.0 (compatible; NuvaMonitor/2.0)"}
        for channel in self.channels:
            try:
                status, body = await self.fetch(
                    ctx, f"{self.base}/{channel}", kind="text", headers=headers, timeout=25)
            except Exception:
                continue
            if body is None:
                continue

            posts = _POST_RE.findall(body)
            texts = _TEXT_RE.findall(body)
            # posts and texts align in document order; pair defensively
            pairs = list(zip(posts[-20:], texts[-20:]))
            by_id = {}
            for post_id, raw in pairs:
                clean = _clean(raw)
                if clean:
                    by_id[post_id] = clean
            for post_id in self.new_ids(ctx, by_id.keys(), ns=f"{self.name}:{channel}"):
                text = by_id[post_id]
                alerts.append(Alert(
                    monitor=f"{self.name} · @{channel}",
                    layer=self.layer,
                    title=text[:200],
                    body=text if len(text) > 200 else "",
                    url=f"https://t.me/{post_id}",
                    priority=KW,  # relevance-gated: only Nuva/Provenance posts pass
                ))
        return alerts

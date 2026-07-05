"""Optional Discord monitor: announcement-channel messages (needs a Discord bot token)."""

from ..alerts import Alert, KW
from .base import Monitor, Context

API = "https://discord.com/api/v10"


class DiscordMonitor(Monitor):
    name = "Discord announcements"
    layer = "social"

    def __init__(self, config):
        super().__init__(config)
        sec = config.section("social.discord")
        self.cadence = int(sec.get("cadence", 600))
        self.token = str(sec.get("bot_token", "") or "").strip()
        self.channels = [c for c in (sec.get("channel_ids") or []) if c]
        if not config.getbool("social.discord.enabled", False):
            self.disable("optional; disabled in config")
        elif not self.token:
            self.disable("DISCORD_BOT_TOKEN not set")
        elif not self.channels:
            self.disable("no channel_ids configured")

    async def poll(self, ctx: Context) -> list:
        alerts = []
        headers = {"Authorization": f"Bot {self.token}"}
        for channel in self.channels:
            status, msgs = await self.fetch(
                ctx, f"{API}/channels/{channel}/messages",
                params={"limit": 20}, headers=headers,
            )
            if not isinstance(msgs, list):
                if status in (401, 403):
                    raise RuntimeError(f"discord auth failed for channel {channel} (HTTP {status})")
                continue
            by_id = {str(m.get("id")): m for m in msgs if m.get("id")}
            for mid in self.new_ids(ctx, by_id.keys(), ns=f"{self.name}:{channel}"):
                m = by_id[mid]
                author = (m.get("author") or {}).get("username", "?")
                content = str(m.get("content") or "")[:400]
                if not content:
                    continue
                alerts.append(Alert(
                    monitor=self.name, layer=self.layer,
                    title=f"Discord · {author}: {content[:120]}",
                    body=content if len(content) > 120 else "",
                    url=f"https://discord.com/channels/@me/{channel}/{mid}",
                    priority=KW,
                ))
        return alerts

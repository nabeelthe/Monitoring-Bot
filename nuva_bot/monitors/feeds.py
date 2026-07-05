"""RSS/Atom-based monitors: X (via Nitter), Reddit, YouTube, blog/docs feeds, news."""

import logging
import urllib.parse

import feedparser

from ..alerts import Alert, ALWAYS, ESCALATE, KW
from .base import Monitor, Context

log = logging.getLogger("nuva.feeds")


def _entry_id(entry) -> str:
    return str(entry.get("id") or entry.get("link") or entry.get("title", ""))[:500]


def _entry_summary(entry) -> str:
    raw = str(entry.get("summary") or entry.get("description") or "")
    # crude tag strip is enough for alert bodies
    out, in_tag = [], False
    for ch in raw:
        if ch == "<":
            in_tag = True
        elif ch == ">":
            in_tag = False
        elif not in_tag:
            out.append(ch)
    return " ".join("".join(out).split())[:400]


class FeedMonitor(Monitor):
    """Watches a set of RSS/Atom feeds with a shared cadence and priority."""

    def __init__(self, config, *, name: str, layer: str, cadence: int,
                 feeds: list, priority: str, enabled: bool = True):
        super().__init__(config)
        self.name = name
        self.layer = layer
        self.cadence = cadence
        # each feed: {"name": ..., "url": ..., "priority": optional override}
        self.feeds = [f for f in feeds if f.get("url")]
        self.priority = priority
        self.fail_counts: dict[str, int] = {}
        if not enabled:
            self.disable("disabled in config")
        elif not self.feeds:
            self.disable("no feeds configured")

    async def _read_feed(self, ctx: Context, url: str):
        status, text = await self.fetch(ctx, url, kind="text", timeout=30)
        if text is None:
            raise RuntimeError(f"HTTP {status}")
        parsed = feedparser.parse(text)
        if parsed.bozo and not parsed.entries:
            raise RuntimeError(f"unparseable feed ({parsed.bozo_exception})")
        return parsed.entries

    async def poll(self, ctx: Context) -> list:
        alerts = []
        for feed in self.feeds:
            url = feed["url"]
            try:
                entries = await self._read_feed(ctx, url)
                self.fail_counts[url] = 0
            except Exception as exc:
                self.fail_counts[url] = self.fail_counts.get(url, 0) + 1
                if self.fail_counts[url] in (5, 50):  # note once, don't spam
                    log.warning("%s: feed %s failing ×%s: %s", self.name, url, self.fail_counts[url], exc)
                continue

            by_id = {}
            for entry in entries[:25]:
                eid = _entry_id(entry)
                if eid:
                    by_id[eid] = entry
            ns = f"{self.name}:{url}"
            for eid in self.new_ids(ctx, by_id.keys(), ns=ns):
                entry = by_id[eid]
                title = str(entry.get("title") or "(no title)")
                alerts.append(Alert(
                    monitor=f"{self.name} · {feed.get('name', '')}".strip(" ·"),
                    layer=self.layer,
                    title=title[:250],
                    body=_entry_summary(entry),
                    url=str(entry.get("link") or "") or None,
                    priority=feed.get("priority", self.priority),
                ))
        return alerts


class NitterXMonitor(FeedMonitor):
    """X/Twitter via Nitter RSS with instance failover per feed."""

    def __init__(self, config, *, name, layer, cadence, handles, searches,
                 instances, official_priority, ecosystem_priority, enabled=True):
        self.instances = [i.rstrip("/") for i in instances if i]
        feeds = []
        for h in handles.get("official", []):
            feeds.append({"name": f"@{h}", "path": f"/{h}/rss", "priority": official_priority})
        for h in handles.get("ecosystem", []):
            feeds.append({"name": f"@{h}", "path": f"/{h}/rss", "priority": ecosystem_priority})
        for q in searches:
            feeds.append({
                "name": f"search {q}",
                "path": f"/search/rss?f=tweets&q={urllib.parse.quote(q)}",
                "priority": ecosystem_priority,
            })
        for f in feeds:
            f["url"] = f"nitter:{f['path']}"  # resolved per-instance at poll time
        super().__init__(config, name=name, layer=layer, cadence=cadence,
                         feeds=feeds, priority=ecosystem_priority, enabled=enabled)
        if enabled and not self.instances:
            self.disable("no nitter instances configured")

    async def _read_feed(self, ctx: Context, url: str):
        path = url.removeprefix("nitter:")
        start = int(ctx.state.kv_get("nitter:instance_idx", 0)) % max(len(self.instances), 1)
        last_exc: Exception | None = None
        for i in range(len(self.instances)):
            idx = (start + i) % len(self.instances)
            inst = self.instances[idx]
            try:
                status, text = await self.fetch(ctx, f"{inst}{path}", kind="text", timeout=25, retries=0)
                if text is None:
                    raise RuntimeError(f"HTTP {status} from {inst}")
                parsed = feedparser.parse(text)
                if not parsed.entries:
                    raise RuntimeError(f"empty feed from {inst}")
                ctx.state.kv_set("nitter:instance_idx", idx)  # stick with a working instance
                return parsed.entries
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"all nitter instances failed: {last_exc}")


def build_feed_monitors(config) -> list:
    monitors = []
    kw = KW
    esc = ESCALATE

    # --- X / Twitter -----------------------------------------------------
    x = config.section("social.x")
    monitors.append(NitterXMonitor(
        config,
        name="X (Twitter)",
        layer="social",
        cadence=int(x.get("cadence", 300)),
        handles={
            "official": x.get("official_handles") or [],
            "ecosystem": x.get("ecosystem_handles") or [],
        },
        searches=x.get("searches") or [],
        instances=x.get("nitter_instances") or [],
        official_priority=ALWAYS if x.get("official_priority", "always") == "always" else esc,
        ecosystem_priority=kw,
        enabled=config.getbool("social.x.enabled", True),
    ))

    # --- Reddit ------------------------------------------------------------
    r = config.section("social.reddit")
    monitors.append(FeedMonitor(
        config, name="Reddit", layer="social",
        cadence=int(r.get("cadence", 1800)),
        feeds=[{"name": f.get("name", "search"), "url": f["url"]} for f in (r.get("feeds") or []) if f.get("url")],
        priority=kw,
        enabled=config.getbool("social.reddit.enabled", True),
    ))

    # --- YouTube -------------------------------------------------------------
    y = config.section("social.youtube")
    yt_feeds = [
        {"name": c.get("name", "channel"),
         "url": f"https://www.youtube.com/feeds/videos.xml?channel_id={c['channel_id']}"}
        for c in (y.get("channels") or []) if c.get("channel_id")
    ]
    monitors.append(FeedMonitor(
        config, name="YouTube", layer="social",
        cadence=int(y.get("cadence", 3600)),
        feeds=yt_feeds, priority=kw,
        enabled=config.getbool("social.youtube.enabled", True),
    ))

    # --- Blog / docs feeds ------------------------------------------------------
    b = config.section("blogdocs")
    monitors.append(FeedMonitor(
        config, name="Blog & Docs", layer="blog",
        cadence=int(b.get("cadence", 900)),
        feeds=[{"name": f.get("name", ""), "url": f["url"]} for f in (b.get("feeds") or []) if f.get("url")],
        priority=kw,
        enabled=config.getbool("blogdocs.enabled", True),
    ))

    # --- News -----------------------------------------------------------------
    n = config.section("news")
    news_feeds = []
    for q in n.get("google_news_queries") or []:
        news_feeds.append({
            "name": f"Google News: {q}",
            "url": "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=en-US&gl=US&ceid=US:en",
            # Google News queries are already scoped — deliver every hit.
            "priority": esc,
        })
    for f in n.get("feeds") or []:
        if f.get("url"):
            news_feeds.append({"name": f.get("name", ""), "url": f["url"]})
    monitors.append(FeedMonitor(
        config, name="News", layer="news",
        cadence=int(n.get("cadence", 900)),
        feeds=news_feeds, priority=kw,
        enabled=config.getbool("news.enabled", True),
    ))

    return monitors

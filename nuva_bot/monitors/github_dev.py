"""GitHub monitor: new repos in provlabs/provenance-io orgs, releases, notable commits."""

from ..alerts import Alert, ESCALATE, KW
from .base import Monitor, Context

API = "https://api.github.com"


class GitHubOrgsMonitor(Monitor):
    name = "GitHub dev activity"
    layer = "dev"
    filterable = False

    def __init__(self, config):
        super().__init__(config)
        sec = config.section("github")
        self.cadence = int(sec.get("cadence", 1200))
        self.orgs = [o for o in (sec.get("orgs") or []) if o]
        self.repos = [r for r in (sec.get("release_repos") or []) if r]
        self.commit_repos = [r for r in (sec.get("commit_repos") or []) if r]
        self.commit_keywords = [str(k).lower() for k in sec.get("commit_keywords", [])]
        self.token = str(sec.get("token", "") or "").strip()
        if not config.getbool("github.enabled", True):
            self.disable("disabled in config")
        elif not (self.orgs or self.repos or self.commit_repos):
            self.disable("no orgs/repos configured")

    def _headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _get(self, ctx: Context, path: str, params: dict | None = None):
        status, data = await self.fetch(ctx, f"{API}{path}", params=params, headers=self._headers())
        if data is None:
            if status in (403, 429):  # rate limited without token — degrade quietly
                return None
            raise RuntimeError(f"github {path} returned HTTP {status}")
        return data

    async def poll(self, ctx: Context) -> list:
        alerts = []

        for org in self.orgs:
            repos = await self._get(ctx, f"/orgs/{org}/repos", {"sort": "created", "direction": "desc", "per_page": 10})
            if not isinstance(repos, list):
                continue
            by_name = {str(r.get("full_name")): r for r in repos if r.get("full_name")}
            for full in self.new_ids(ctx, by_name.keys(), ns=f"{self.name}:repos:{org}"):
                r = by_name[full]
                alerts.append(Alert(
                    monitor=self.name, layer=self.layer,
                    title=f"New repo in {org}: {full}",
                    body=str(r.get("description") or "")[:300],
                    url=r.get("html_url"),
                    priority=ESCALATE, filterable=False,
                ))

        for repo in self.repos:
            rels = await self._get(ctx, f"/repos/{repo}/releases", {"per_page": 5})
            if not isinstance(rels, list):
                continue
            by_id = {str(r.get("id")): r for r in rels if r.get("id")}
            for rid in self.new_ids(ctx, by_id.keys(), ns=f"{self.name}:rel:{repo}"):
                r = by_id[rid]
                alerts.append(Alert(
                    monitor=self.name, layer=self.layer,
                    title=f"Release {r.get('tag_name', '?')} — {repo}",
                    body=f"{r.get('name') or ''}\n{str(r.get('body') or '')[:400]}".strip(),
                    url=r.get("html_url"),
                    priority=ESCALATE, filterable=False,
                ))

        for repo in self.commit_repos:
            commits = await self._get(ctx, f"/repos/{repo}/commits", {"per_page": 10})
            if not isinstance(commits, list):
                continue
            by_sha = {str(c.get("sha")): c for c in commits if c.get("sha")}
            for sha in self.new_ids(ctx, by_sha.keys(), ns=f"{self.name}:commits:{repo}"):
                c = by_sha[sha]
                msg = str(((c.get("commit") or {}).get("message") or "")).split("\n")[0]
                alerts.append(Alert(
                    monitor=self.name, layer=self.layer,
                    title=f"Commit in {repo}: {msg[:120]}",
                    url=c.get("html_url"),
                    priority=KW,  # only "notable" commits get through the gate
                    filterable=False,
                    gate_keywords=self.commit_keywords,
                ))
        return alerts

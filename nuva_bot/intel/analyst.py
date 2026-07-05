"""AI Analyst layer.

For critical/high events, produce an analyst brief answering: what happened,
why it matters, how confident we are, suggested action, what to monitor next.

Uses the Claude API (official anthropic SDK) when ANTHROPIC_API_KEY is set;
otherwise falls back to a deterministic rule-based analyst so the platform
degrades gracefully — never silently loses the intelligence layer.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field

from .events import Event

log = logging.getLogger("nuva.analyst")

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "One-sentence executive summary of what happened"},
        "why_it_matters": {"type": "string", "description": "Why this matters for the Nuva/Provenance ecosystem, 1-2 sentences"},
        "assessment": {"type": "string", "enum": ["bullish", "bearish", "neutral", "suspicious", "informational"]},
        "risk_note": {"type": "string", "description": "Risk assessment in one sentence, incl. manipulation probability if relevant"},
        "suggested_action": {"type": "string", "description": "Concrete next action for the operator, one sentence"},
        "monitor_next": {"type": "array", "items": {"type": "string"}, "description": "1-3 things to watch next"},
        "needs_human": {"type": "boolean", "description": "Should a human investigate now?"},
    },
    "required": ["summary", "why_it_matters", "assessment", "risk_note",
                 "suggested_action", "monitor_next", "needs_human"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You are the AI analyst of a crypto intelligence platform monitoring the Nuva "
    "Labs / Nuva Finance / Provenance Blockchain ecosystem (HASH = Provenance L1 "
    "token, NUVA = pre-TGE Nuva Finance token). You receive one detected event "
    "plus correlated signals and history. Write for a professional trading desk: "
    "factual, specific, no hype, no filler. Flag anything that looks like "
    "manipulation, a scam, or a fake announcement. If data is thin, say so."
)


@dataclass
class Brief:
    summary: str
    why_it_matters: str
    assessment: str
    risk_note: str
    suggested_action: str
    monitor_next: list = field(default_factory=list)
    needs_human: bool = False
    source: str = "rules"  # "ai" or "rules"


def _fmt_event_line(e: Event) -> str:
    return f"- [{e.layer}/{e.priority}] {time.strftime('%m-%d %H:%M', time.gmtime(e.ts))} {e.title}"


class Analyst:
    def __init__(self, config):
        sec = config.section("intelligence.ai")
        self.model = str(sec.get("model", "claude-opus-4-8"))
        self.max_per_hour = int(sec.get("max_analyses_per_hour", 12))
        self.enabled = config.getbool("intelligence.ai.enabled", True)
        self._stamps: list[float] = []
        self._client = None
        if self.enabled:
            try:
                import anthropic  # noqa: PLC0415 — optional dependency
                api_key = str(sec.get("api_key") or "").strip() or None
                # zero-arg client also resolves ANTHROPIC_API_KEY / auth profiles
                self._client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
                if not (api_key or os.environ.get("ANTHROPIC_API_KEY")):
                    log.info("no ANTHROPIC_API_KEY — AI analyst will fall back to rule-based briefs")
            except Exception as exc:  # missing package, bad env
                log.warning("anthropic SDK unavailable (%s) — using rule-based analyst", exc)
                self._client = None

    def _budget_ok(self) -> bool:
        now = time.time()
        self._stamps = [s for s in self._stamps if now - s < 3600]
        return len(self._stamps) < self.max_per_hour

    async def analyze(self, ev: Event, context: dict) -> Brief:
        if self._client is not None and self._budget_ok():
            try:
                self._stamps.append(time.time())
                return await asyncio.wait_for(self._ai_brief(ev, context), timeout=90)
            except Exception as exc:
                log.warning("AI analysis failed (%s); using rule-based brief", exc)
        return self.rule_brief(ev, context)

    # ---- Claude-powered analysis ---------------------------------------
    async def _ai_brief(self, ev: Event, context: dict) -> Brief:
        related = context.get("related") or []
        history = context.get("history") or []
        payload = {
            "event": {
                "layer": ev.layer, "monitor": ev.monitor, "title": ev.title,
                "body": ev.body[:1200], "confidence": ev.confidence,
                "priority": ev.priority, "escalation_keywords": ev.escalation_hits,
                "url": ev.url,
            },
            "correlated_signals_same_story": [_fmt_event_line(e) for e in related[:12]],
            "similar_past_events": [_fmt_event_line(e) for e in history[:5]],
        }
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": BRIEF_SCHEMA}},
            messages=[{
                "role": "user",
                "content": "Analyze this detected event:\n\n" + json.dumps(payload, indent=1),
            }],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("model refused analysis request")
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        return Brief(
            summary=data["summary"],
            why_it_matters=data["why_it_matters"],
            assessment=data["assessment"],
            risk_note=data["risk_note"],
            suggested_action=data["suggested_action"],
            monitor_next=list(data.get("monitor_next") or [])[:3],
            needs_human=bool(data.get("needs_human")),
            source="ai",
        )

    # ---- deterministic fallback -----------------------------------------
    ACTION_BY_TAG = {
        "tge": ("Verify the TGE details on official channels before acting.", ["official X accounts", "listing pages"]),
        "airdrop": ("Check eligibility criteria and snapshot date on the official portal.", ["Genesis Pass portal", "official announcements"]),
        "listing": ("Confirm the listing on the exchange's official channels; watch for fake announcements.", ["exchange announcements", "order books / first prints"]),
        "exploit": ("Treat as incident: verify on-chain, reduce exposure until confirmed safe.", ["official incident comms", "on-chain outflows"]),
        "hack": ("Treat as incident: verify on-chain, reduce exposure until confirmed safe.", ["official incident comms", "on-chain outflows"]),
        "depeg": ("Check vault collateralization and pool balances immediately.", ["nvAsset vault state", "pool liquidity"]),
        "mint": ("Track where the minted supply moves next.", ["recipient wallets", "exchange inflows"]),
        "governance": ("Read the proposal; note the voting deadline.", ["voting progress", "upgrade height"]),
        "tvl": ("Identify which protocol/vault drove the TVL change.", ["DefiLlama breakdown", "vault issuance feed"]),
        "price": ("Check whether volume and on-chain activity confirm the move.", ["exchange inflows", "volume trend"]),
        "release": ("Review the changelog for mainnet/vault-relevant changes.", ["deployment on-chain", "follow-up commits"]),
    }

    def rule_brief(self, ev: Event, context: dict) -> Brief:
        cross = context.get("cross_layer") or []
        history = context.get("history") or []
        tag = next((t for t in ev.tags if t in self.ACTION_BY_TAG), None)
        action, watch = self.ACTION_BY_TAG.get(
            tag, ("Review the source and confirm via a second channel.", ["related official channels"]))

        if ev.escalation_hits and ev.layer in ("social", "news"):
            assessment = "suspicious" if not cross else "bullish"
        elif any(t in ("exploit", "hack", "depeg") for t in ev.tags):
            assessment = "bearish"
        elif ev.priority_class == "always" or cross:
            assessment = "bullish" if ev.escalation_hits else "informational"
        else:
            assessment = "neutral"

        drivers = f"{len(cross)} independent confirmation(s) across {len({e.layer for e in cross})} other layer(s)." \
            if cross else "No cross-layer confirmation yet — single-source signal."
        hist_note = ""
        if history:
            dates = ", ".join(time.strftime("%b %d", time.gmtime(e.ts)) for e in history[:3])
            hist_note = f" Similar events seen: {dates}."

        return Brief(
            summary=ev.title,
            why_it_matters=f"{ev.layer.capitalize()}-layer signal with {ev.confidence}% confidence. {drivers}{hist_note}",
            assessment=assessment,
            risk_note=("Unconfirmed single-source signal — manipulation/false-positive risk is elevated."
                       if not cross and ev.layer in ("social", "news")
                       else "Low probability of manipulation given source credibility."),
            suggested_action=action,
            monitor_next=watch,
            needs_human=ev.priority == "critical",
            source="rules",
        )

"""Alert model + priority engine.

Priority classes (from the monitoring route map):
  always   ★ always loud — every hit is delivered with notification
  escalate ● delivered normally; goes loud when an auto-escalation keyword hits
  kw       ■ keyword-gated — only delivered when a relevance keyword hits
"""

import html
from dataclasses import dataclass, field

ALWAYS = "always"
ESCALATE = "escalate"
KW = "kw"

LAYER_EMOJI = {
    "onchain": "⛓️",
    "ethereum": "🪙",
    "market": "📈",
    "token": "🚀",
    "dev": "🛠️",
    "blog": "📰",
    "social": "💬",
    "news": "🗞️",
    "system": "⚙️",
}


@dataclass
class Alert:
    monitor: str
    layer: str
    title: str
    body: str = ""
    url: str | None = None
    priority: str = ESCALATE
    # When False, negative-keyword filtering is skipped (on-chain / market data
    # can't be confused with the unrelated gaming startup).
    filterable: bool = True
    # Optional per-alert gate list overriding the global relevance keywords
    # (used e.g. for "notable commit" detection).
    gate_keywords: list = field(default_factory=list)


@dataclass
class Verdict:
    send: bool
    loud: bool
    escalation_hits: list
    gate_hits: list


class AlertEngine:
    def __init__(self, config):
        kws = config.section("keywords")
        self.escalation = [str(k).lower() for k in kws.get("escalation", [])]
        self.relevance = [str(k).lower() for k in kws.get("relevance", [])]
        self.negative = [str(k).lower() for k in kws.get("negative", [])]

    @staticmethod
    def _hits(keywords: list, text: str) -> list:
        return [k for k in keywords if k in text]

    def evaluate(self, alert: Alert) -> Verdict:
        text = f"{alert.title}\n{alert.body}".lower()
        esc_hits = self._hits(self.escalation, text)

        # Filter out the unrelated nuvalab.ai gaming startup and similar noise.
        if alert.filterable and not esc_hits and self._hits(self.negative, text):
            return Verdict(send=False, loud=False, escalation_hits=[], gate_hits=[])

        if alert.priority == ALWAYS:
            return Verdict(send=True, loud=True, escalation_hits=esc_hits, gate_hits=[])

        if alert.priority == KW:
            gates = [str(k).lower() for k in alert.gate_keywords] or self.relevance
            gate_hits = self._hits(gates, text)
            if not gate_hits and not esc_hits:
                return Verdict(send=False, loud=False, escalation_hits=esc_hits, gate_hits=[])
            return Verdict(send=True, loud=bool(esc_hits), escalation_hits=esc_hits, gate_hits=gate_hits)

        # ESCALATE (default): always send, loud only on escalation keyword.
        return Verdict(send=True, loud=bool(esc_hits), escalation_hits=esc_hits, gate_hits=[])

    def format(self, alert: Alert, verdict: Verdict) -> str:
        emoji = LAYER_EMOJI.get(alert.layer, "🔔")
        prefix = "🚨 " if verdict.loud else ""
        lines = [f"{prefix}{emoji} <b>[{html.escape(alert.layer.upper())}] {html.escape(alert.monitor)}</b>"]
        lines.append(f"<b>{html.escape(alert.title)}</b>")
        if alert.body:
            body = alert.body if len(alert.body) <= 2500 else alert.body[:2500] + "…"
            lines.append(html.escape(body))
        if verdict.escalation_hits:
            lines.append("⚡ Escalation keywords: " + html.escape(", ".join(verdict.escalation_hits)))
        if alert.url:
            lines.append(f'<a href="{html.escape(alert.url, quote=True)}">Open source ↗</a>')
        return "\n".join(lines)

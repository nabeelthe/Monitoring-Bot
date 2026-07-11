"""AI Copilot — ask the bot anything, it answers from everything it monitors.

"Why did price move?" · "What happened today?" · "Should I worry?" ·
"Summarize the last week." The copilot assembles live context (recent events,
stories, risk panel, predictions, price) and answers via Claude in plain
language. Without an API key it returns a structured data summary instead.
"""

import asyncio
import html
import logging
import time

log = logging.getLogger("nuva.copilot")

SYSTEM_PROMPT = (
    "You are the AI copilot of a monitoring platform for the Nuva Labs / Nuva "
    "Finance / Provenance Blockchain ecosystem (HASH = Provenance's token, NUVA "
    "= Nuva Finance's upcoming token). Answer the user's question using ONLY the "
    "monitoring data provided. Write in plain English a beginner understands — "
    "explain any jargon briefly. Be direct: lead with the answer, then the "
    "evidence. If the data doesn't cover the question, say so honestly instead "
    "of guessing. Never give financial advice ('buy'/'sell'); describe what the "
    "data shows and what to watch. Keep answers under 200 words unless the "
    "question truly needs more."
)


class Copilot:
    def __init__(self, config, pipeline):
        self.pipeline = pipeline
        sec = config.section("intelligence.copilot")
        self.enabled = config.getbool("intelligence.copilot.enabled", True)
        self.model = str(sec.get("model") or config.get("intelligence.ai.model", "claude-opus-4-8"))
        self.max_per_hour = int(sec.get("max_questions_per_hour", 20))
        self._stamps: list[float] = []
        self._client = None
        if self.enabled:
            try:
                import anthropic  # noqa: PLC0415 — optional dependency
                self._client = anthropic.AsyncAnthropic()
            except Exception as exc:
                log.warning("anthropic SDK unavailable (%s) — copilot uses data summaries", exc)

    def _budget_ok(self) -> bool:
        now = time.time()
        self._stamps = [s for s in self._stamps if now - s < 3600]
        return len(self._stamps) < self.max_per_hour

    # ---- context assembly ---------------------------------------------------
    def _context(self) -> str:
        p = self.pipeline
        lines = ["=== MONITORING DATA (live) ==="]

        price = p.state.kv_get("cg:last_price")
        if price:
            lines.append(f"HASH last price: ${price:,.6f}")

        events = [e for e in p.store.recent(48, limit=250) if e.priority != "ignore"]
        events.sort(key=lambda e: e.ts, reverse=True)
        lines.append(f"\nEvents last 48h ({len(events)} shown newest-first, top 25):")
        for e in events[:25]:
            t = time.strftime("%m-%d %H:%M", time.gmtime(e.ts))
            lines.append(f"- {t} [{e.layer}/{e.priority}/{e.confidence}%] {e.title[:140]}")

        by_layer = p.store.counts_by_layer(24)
        if by_layer:
            lines.append("\nSignal counts 24h: " + ", ".join(f"{k}={v}" for k, v in sorted(by_layer.items())))

        lines.append("\nRisk panel:")
        for d in p.risk.snapshot(p.monitor_statuses):
            lines.append(f"- {d.name}: {d.score}/100 ({d.trend}) — {d.detail}")

        lines.append("\nPredictions:")
        for pr in p.predictor.all():
            lines.append(f"- {pr.probability}% {pr.name} ({'; '.join(pr.evidence)})")

        return "\n".join(lines)[:12000]

    # ---- answering ------------------------------------------------------------
    async def answer(self, question: str) -> str:
        question = question.strip()[:500]
        if not question:
            return "Ask me anything about what I'm monitoring — e.g. “what happened today?” or “should I worry?”"

        if self._client is not None and self._budget_ok():
            try:
                self._stamps.append(time.time())
                return await asyncio.wait_for(self._ai_answer(question), timeout=90)
            except Exception as exc:
                log.warning("copilot AI answer failed (%s) — falling back to data summary", exc)
        return self._fallback_answer()

    async def _ai_answer(self, question: str) -> str:
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=900,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"{self._context()}\n\n=== QUESTION ===\n{question}",
            }],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("model declined")
        text = next((b.text for b in response.content if b.type == "text"), "").strip()
        if not text:
            raise RuntimeError("empty answer")
        return "🤖 " + html.escape(text)

    def _fallback_answer(self) -> str:
        """No AI available: answer with a structured summary of the live data."""
        p = self.pipeline
        parts = ["🤖 <b>Here's what the data shows</b> (add ANTHROPIC_API_KEY for conversational answers):", ""]
        price = p.state.kv_get("cg:last_price")
        if price:
            parts.append(f"HASH price: ${price:,.6f}")
        events = [e for e in p.store.recent(24, limit=100) if e.priority != "ignore"]
        events.sort(key=lambda e: (e.confidence, e.ts), reverse=True)
        parts.append(f"{len(events)} notable signal(s) in 24h. Top ones:")
        for e in events[:5]:
            t = time.strftime("%H:%M", time.gmtime(e.ts))
            parts.append(f"• {t} [{e.layer}] {html.escape(e.title[:110])}")
        worst = max(p.risk.snapshot(p.monitor_statuses), key=lambda d: d.score, default=None)
        if worst:
            parts.append(f"Biggest current risk: <b>{worst.name}</b> ({worst.score}/100) — {html.escape(worst.recommendation)}")
        return "\n".join(parts)

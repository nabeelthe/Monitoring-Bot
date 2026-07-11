"""Unified LLM client for the analyst + copilot.

Two providers, auto-selected by which API key is present:
  · anthropic  — official SDK, direct to Claude (ANTHROPIC_API_KEY)
  · openrouter — OpenAI-compatible gateway to 200+ models incl. free tiers
                 (OPENROUTER_API_KEY)
If neither key is set, `available` is False and callers use their rule-based
fallbacks — the platform never breaks for lack of an AI key.
"""

import json
import logging
import os
import re

import aiohttp

log = logging.getLogger("nuva.llm")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# free-tier default; change via intelligence.ai.openrouter_model.
# If this exact id is retired, the client auto-falls back to openrouter/auto.
DEFAULT_OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct:free"

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> str:
    """Models sometimes wrap JSON in markdown fences or prose — dig it out."""
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1)
    text = text.strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
    return text


class LLMClient:
    def __init__(self, config):
        sec = config.section("intelligence.ai")
        want = str(sec.get("provider", "auto")).lower()
        akey = str(sec.get("api_key") or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()
        okey = str(sec.get("openrouter_api_key") or "").strip() or os.environ.get("OPENROUTER_API_KEY", "").strip()
        self.anthropic_model = str(sec.get("model", "claude-opus-4-8"))
        self.openrouter_model = str(sec.get("openrouter_model") or DEFAULT_OPENROUTER_MODEL)
        self.provider: str | None = None
        self._anthropic = None
        self._okey = okey

        if want in ("auto", "anthropic") and akey:
            try:
                import anthropic  # noqa: PLC0415 — optional dependency
                self._anthropic = anthropic.AsyncAnthropic(api_key=akey)
                self.provider = "anthropic"
            except Exception as exc:
                log.warning("anthropic SDK unavailable (%s)", exc)
        if self.provider is None and want in ("auto", "openrouter") and okey:
            self.provider = "openrouter"

    @property
    def available(self) -> bool:
        return self.provider is not None

    def describe(self) -> str:
        if self.provider == "anthropic":
            return f"anthropic:{self.anthropic_model}"
        if self.provider == "openrouter":
            return f"openrouter:{self.openrouter_model}"
        return "none (rule-based fallbacks)"

    async def chat(self, *, system: str, user: str, max_tokens: int = 900,
                   schema: dict | None = None) -> str:
        """Return the model's text answer. With `schema`, the answer is a JSON
        string conforming to it (best-effort on OpenRouter models)."""
        if self.provider == "anthropic":
            return await self._chat_anthropic(system, user, max_tokens, schema)
        if self.provider == "openrouter":
            return await self._chat_openrouter(system, user, max_tokens, schema)
        raise RuntimeError("no LLM provider configured")

    # ---- anthropic ---------------------------------------------------------
    async def _chat_anthropic(self, system, user, max_tokens, schema) -> str:
        kwargs = {}
        if schema:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        response = await self._anthropic.messages.create(
            model=self.anthropic_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            **kwargs,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("model refused the request")
        text = next((b.text for b in response.content if b.type == "text"), "").strip()
        if not text:
            raise RuntimeError("empty model answer")
        return text

    # ---- openrouter ----------------------------------------------------------
    def _openrouter_payload(self, model, system, user, max_tokens, schema, use_rf) -> dict:
        if schema:
            # belt and suspenders: instruction in-prompt for models that
            # ignore response_format
            user = (f"{user}\n\nRespond ONLY with a JSON object matching this "
                    f"schema (no prose, no markdown fences):\n{json.dumps(schema)}")
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if schema and use_rf:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "answer", "strict": True, "schema": schema},
            }
        return payload

    async def _chat_openrouter(self, system, user, max_tokens, schema) -> str:
        headers = {
            "Authorization": f"Bearer {self._okey}",
            "HTTP-Referer": "https://github.com/nabeelthe/monitoring-bot",
            "X-Title": "Nuva Intelligence Platform",
        }
        # attempts: configured model (with structured output) → configured model
        # (plain) → auto-router (plain). Covers retired ids and models that
        # reject response_format.
        attempts = [(self.openrouter_model, True), (self.openrouter_model, False)]
        if self.openrouter_model != "openrouter/auto":
            attempts.append(("openrouter/auto", False))
        last_err = "unknown"
        async with aiohttp.ClientSession() as session:
            for model, use_rf in attempts:
                if not schema and use_rf:
                    continue  # plain chats have no structured-output variant
                payload = self._openrouter_payload(model, system, user, max_tokens, schema, use_rf)
                try:
                    async with session.post(
                        OPENROUTER_URL, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=90),
                    ) as resp:
                        data = await resp.json(content_type=None)
                except Exception as exc:
                    last_err = f"network: {exc}"
                    continue
                choices = (data or {}).get("choices") or []
                if choices:
                    text = str(((choices[0].get("message") or {}).get("content")) or "").strip()
                    if text:
                        return text
                    last_err = "empty answer"
                    continue
                err = (data or {}).get("error") or {}
                last_err = f"{err.get('code', resp.status)}: {str(err.get('message', ''))[:200]}"
                log.warning("openrouter %s failed (%s) — trying next option", model, last_err)
        raise RuntimeError(f"openrouter exhausted: {last_err}")

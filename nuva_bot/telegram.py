"""Minimal, dependency-light Telegram Bot API client + notifier."""

import asyncio
import json
import logging
import time

import aiohttp

log = logging.getLogger("nuva.telegram")

API_BASE = "https://api.telegram.org"
MAX_MSG_LEN = 4096


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, token: str, session: aiohttp.ClientSession):
        self._token = token
        self._session = session

    async def api(self, method: str, *, http_timeout: float = 65, **params) -> dict:
        url = f"{API_BASE}/bot{self._token}/{method}"
        payload = {k: v for k, v in params.items() if v is not None}
        for attempt in range(4):
            try:
                async with self._session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=http_timeout)
                ) as resp:
                    data = await resp.json(content_type=None)
                    if data.get("ok"):
                        return data.get("result")
                    code = data.get("error_code")
                    if code == 429:
                        retry_after = (data.get("parameters") or {}).get("retry_after", 3)
                        log.warning("telegram 429, retrying in %ss", retry_after)
                        await asyncio.sleep(retry_after + 0.5)
                        continue
                    if code in (500, 502, 503, 504) and attempt < 3:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    raise TelegramError(f"{method}: {data.get('description')} (code {code})")
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
                if attempt < 3:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise TelegramError(f"{method}: network error: {exc}") from exc
        raise TelegramError(f"{method}: retries exhausted")

    async def get_me(self) -> dict:
        return await self.api("getMe", http_timeout=20)

    async def send_message(self, chat_id: int, text: str, *, silent: bool = False) -> None:
        for chunk in _split(text):
            await self.api(
                "sendMessage",
                chat_id=chat_id,
                text=chunk,
                parse_mode="HTML",
                disable_web_page_preview=True,
                disable_notification=silent,
                http_timeout=30,
            )

    async def get_updates(self, offset: int | None, poll_timeout: int = 50) -> list:
        return await self.api(
            "getUpdates",
            offset=offset,
            http_timeout=poll_timeout + 15,
            timeout=poll_timeout,
            allowed_updates=["message"],
        )


def _split(text: str) -> list[str]:
    if len(text) <= MAX_MSG_LEN:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(line) > MAX_MSG_LEN - 1 and current:
            chunks.append(current)  # flush before splitting an oversized line
            current = ""
        while len(line) > MAX_MSG_LEN - 1:
            chunks.append(line[: MAX_MSG_LEN - 1])
            line = line[MAX_MSG_LEN - 1 :]
        if len(current) + len(line) + 1 > MAX_MSG_LEN:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


class Notifier:
    """Fan-out alerts to all registered chats with basic rate limiting."""

    def __init__(self, client: TelegramClient, state):
        self._client = client
        self._state = state
        self._lock = asyncio.Lock()
        self._last_send = 0.0
        self.sent_count = 0

    async def broadcast(self, text: str, *, loud: bool = False) -> None:
        chats = list(self._state.chats)
        if not chats:
            log.warning("no chats registered yet; dropping alert: %.80s", text)
            return
        muted = self._state.is_muted()
        silent = muted or not loud
        async with self._lock:
            for chat in chats:
                # ~1 msg/sec global keeps us well inside Telegram limits.
                wait = self._last_send + 1.05 - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    await self._client.send_message(chat["id"], text, silent=silent)
                    self.sent_count += 1
                except TelegramError as exc:
                    log.error("send to chat %s failed: %s", chat["id"], exc)
                self._last_send = time.monotonic()

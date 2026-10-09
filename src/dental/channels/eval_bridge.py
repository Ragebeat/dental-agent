"""Eval «через LINE»: реплики пациента идут подписанными событиями через вебхук (ASGI, без сети) и шлюз сессий, ответы
забираются из заглушки Reply API. Критерий E7: те же метрики, что при прямом вызове агента (`--channel web`)."""

from __future__ import annotations

import json
from datetime import datetime

import httpx

from dental.agent.baseline import Usage
from dental.channels.line import Gateway, McpSessions, RecordingSender, create_app, sign

SECRET = "eval-secret"
USER = "Ueval0000000000000000000000000000"


class LineAgent:
    def __init__(self, core, llm, now: datetime, st) -> None:
        self.sender = RecordingSender()
        # лимиты частоты и числа ходов отключены: сценарии короткие и быстрые, а лимиты проверяются отдельными тестами
        self.gw = Gateway(McpSessions(core, llm, lambda: now, max_rounds=st.max_rounds, token_cap=st.token_cap), self.sender,
                          now_fn=lambda: now, ttl_s=3600, rate_per_min=10**6, max_turns=10**6, max_text=10**6)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(SECRET, self.gw)), base_url="http://line")
        self._n = 0
        self._agent = None

    async def reply(self, text: str) -> str:
        self._n += 1
        before, self.gw.last_error = len(self.sender.sent), None
        body = json.dumps({"events": [{"type": "message", "webhookEventId": f"eval-{self._n}", "replyToken": f"rt{self._n}",
                                       "source": {"type": "user", "userId": USER},
                                       "message": {"type": "text", "text": text}}]}).encode()
        r = await self.client.post("/line/webhook", content=body, headers={"x-line-signature": sign(SECRET, body)})
        r.raise_for_status()
        await self.gw.drain()
        self._agent = self.gw.agent_of(USER) or self._agent
        if self.gw.last_error is not None:
            raise self.gw.last_error                    # TokenCapReached / LLMError доходят до раннера как при прямом вызове
        return "\n".join("\n".join(t) for _, _, t in self.sender.sent[before:])

    @property
    def events(self) -> list[dict]:
        return self._agent.events if self._agent else []

    @property
    def usage(self) -> Usage:
        return self._agent.usage if self._agent else Usage()

    @property
    def round_limit_hits(self) -> int:
        return self._agent.round_limit_hits if self._agent else 0

    async def aclose(self) -> None:
        await self.client.aclose()
        await self.gw.aclose()

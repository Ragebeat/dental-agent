"""Канал LINE (Messaging API, вебхук): тот же агент, другой вход. Код агента и MCP-сервера не меняется.

Путь события: LINE → POST /line/webhook → проверка подписи X-Line-Signature (HMAC-SHA256 от тела по Channel Secret; без
верной подписи тело даже не разбирается) → событие уходит в фон, вебхук сразу отвечает 200 → сессия пользователя →
GraphAgent.reply → ответ через Reply API (токен ответа живёт недолго; при отказе — Push API).

Сессия = один LINE-пользователь = один GraphAgent и один MCP-клиент со своим состоянием, как «один разговор» в веб-чате.
Каждая сессия — отдельная задача-актор с очередью: сообщения одного пользователя обрабатываются строго по порядку и
никогда параллельно, чужие сессии друг друга не задевают. Контекст MCP-клиента открывается и закрывается в одной задаче
(иначе anyio ругается на выход из области отмены в другой задаче).

Безопасность и ресурсы (SPEC, раздел 7):
- Идентификатор пользователя LINE в базу и в журналы не попадает: ключ сессии = sha256(соль + userId)[:16]; он же client_key
  для блокировки перебора карт. Соль задаётся LINE_USER_SALT.
- Ограничение частоты (сообщений в минуту на пользователя), лимит ходов в сессии, лимит длины входящего текста, простой
  сессии закрывает её (TTL), общий потолок числа сессий; ночью/после смены даты сессия пересоздаётся (агент считает
  «завтра» от даты создания).
- Отвечаем только в личных чатах (source.type == "user"): в группах агент молчит, персональные данные туда не попадают.
- Повторная доставка (webhookEventId уже видели) игнорируется: иначе запись создалась бы дважды.
- Содержимое сообщений не логируется.

Известное ограничение: запасной вызов LLM в графе синхронный и блокирует цикл событий на время запроса (правила идут первыми,
LLM нужен редко). Для нагрузки — вынести вызов в поток; для демо и eval этого достаточно.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, AsyncIterator, Callable, Protocol

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

log = logging.getLogger("dental.line")

LINE_API = "https://api.line.me/v2/bot/message"
MAX_LINE_TEXT = 5000          # предел одного текстового сообщения LINE
MAX_REPLY_MESSAGES = 5        # за один Reply/Push
MAX_BODY = 1_000_000

GREETING = "友だち追加ありがとうございます。AIの予約アシスタントです。ご予約・変更・キャンセルのご希望をメッセージでお送りください。"
UNSUPPORTED = "申し訳ありません。テキストのメッセージのみ対応しております。"
TOO_LONG = "メッセージが長すぎます。短くしてもう一度お送りください。"
THROTTLED = "メッセージが多すぎます。しばらく時間をおいてからお送りください。"
TURN_LIMIT = "やり取りが長くなりましたので、いったん終了いたします。続きは新しくメッセージをお送りください。"
APOLOGY = "申し訳ありません。ただいま処理できませんでした。しばらくしてからもう一度お試しください。"


# ---------- подпись ----------
def sign(secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header:
        return False
    return hmac.compare_digest(sign(secret, body).encode(), header.encode())


# ---------- отправка ----------
class Sender(Protocol):
    async def reply(self, token: str, texts: list[str]) -> None: ...
    async def push(self, user_id: str, texts: list[str]) -> None: ...


def split_text(text: str, limit: int = MAX_LINE_TEXT, max_parts: int = MAX_REPLY_MESSAGES) -> list[str]:
    parts = [text[i:i + limit] for i in range(0, len(text), limit)] or [""]
    if len(parts) > max_parts:
        parts = parts[:max_parts]
        parts[-1] = parts[-1][:limit - 1] + "…"
    return parts


class LineApi:
    """Настоящий Messaging API. Нужны LINE_CHANNEL_ACCESS_TOKEN и сетевой доступ к api.line.me."""

    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(timeout=10)
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _post(self, path: str, payload: dict) -> None:
        r = await self.client.post(f"{LINE_API}/{path}", json=payload, headers=self.headers)
        r.raise_for_status()

    async def reply(self, token: str, texts: list[str]) -> None:
        await self._post("reply", {"replyToken": token, "messages": [{"type": "text", "text": t} for t in split_text("\n".join(texts))]})

    async def push(self, user_id: str, texts: list[str]) -> None:
        await self._post("push", {"to": user_id, "messages": [{"type": "text", "text": t} for t in split_text("\n".join(texts))]})


@dataclass
class RecordingSender:
    """Заглушка Reply/Push API: для тестов, эмулятора и eval-прогона через канал."""
    sent: list[tuple[str, str, list[str]]] = field(default_factory=list)      # (reply|push, токен или пользователь, тексты)
    fail_reply: bool = False

    async def reply(self, token: str, texts: list[str]) -> None:
        if self.fail_reply:
            raise httpx.HTTPError("reply token expired")
        self.sent.append(("reply", token, list(texts)))

    async def push(self, user_id: str, texts: list[str]) -> None:
        self.sent.append(("push", user_id, list(texts)))

    def texts(self) -> list[str]:
        return ["\n".join(t) for _, _, t in self.sent]


# ---------- сессии ----------
class SessionFactory(Protocol):
    def open(self, user_key: str, now: datetime) -> contextlib.AbstractAsyncContextManager:   # -> агент с .reply()
        ...


class McpSessions:
    """Боевая фабрика: своя MCP-сессия (channel="line", client_key = ключ пользователя) и свой GraphAgent."""

    def __init__(self, core, llm, now_fn: Callable[[], datetime], *, max_rounds: int = 8, token_cap: int = 150_000) -> None:
        self.core, self.llm, self.now_fn, self.max_rounds, self.token_cap = core, llm, now_fn, max_rounds, token_cap

    @contextlib.asynccontextmanager
    async def open(self, user_key: str, now: datetime) -> AsyncIterator[Any]:
        from mcp import Client

        from dental.agent.baseline import openai_tools
        from dental.agent.graph import GraphAgent
        from dental.mcp_server.server import build_server
        srv = build_server(self.core, now_fn=self.now_fn, channel="line", session_id=f"line:{user_key}")
        async with Client(srv) as client:
            tools = openai_tools((await client.list_tools()).tools)
            yield GraphAgent(self.llm, client, tools, now, max_rounds=self.max_rounds, token_cap=self.token_cap)


class _Closed(Exception):
    """Сессия закрылась (простой, смена даты, лимит) — сообщение нужно отдать новой."""


class _TurnLimit(Exception):
    pass


class _Session:
    def __init__(self, gw: "Gateway", key: str) -> None:
        self.gw, self.key = gw, key
        self.queue: asyncio.Queue = asyncio.Queue()
        self.agent: Any = None
        self.closed = False
        self.turns = 0
        self.born: date = gw.now_fn().date()
        self.task = asyncio.create_task(self._run())

    def _retire(self) -> None:
        """Сессия больше не принимает сообщения (сразу, синхронно: закрытие MCP-клиента ждёт, а новые сообщения — нет)."""
        self.closed = True
        if self.gw._sessions.get(self.key) is self:
            del self.gw._sessions[self.key]

    async def _run(self) -> None:
        try:
            async with self.gw.factory.open(self.key, self.gw.now_fn()) as agent:
                self.agent = agent
                while True:
                    try:
                        item = await asyncio.wait_for(self.queue.get(), self.gw.ttl_s)
                    except asyncio.TimeoutError:
                        self._retire()
                        break
                    if item is None:
                        self._retire()
                        break
                    text, fut = item
                    if self.gw.now_fn().date() != self.born:
                        self._retire()
                        fut.set_exception(_Closed())
                        break
                    self.turns += 1
                    if self.turns > self.gw.max_turns:
                        self._retire()
                        fut.set_exception(_TurnLimit())
                        break
                    try:
                        fut.set_result(await agent.reply(text))
                    except Exception as e:  # noqa: BLE001
                        self._retire()
                        fut.set_exception(e)
                        break
        except Exception:  # noqa: BLE001
            log.exception("session %s crashed", self.key)
        finally:
            self._retire()
            while not self.queue.empty():
                item = self.queue.get_nowait()
                if item is not None and not item[1].done():
                    item[1].set_exception(_Closed())

    async def ask(self, text: str) -> str:
        if self.closed:
            raise _Closed()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((text, fut))
        return await fut

    def stop(self) -> None:
        self.queue.put_nowait(None)


# ---------- шлюз ----------
class Gateway:
    def __init__(self, factory: SessionFactory, sender: Sender, *, now_fn: Callable[[], datetime], salt: str = "dental-agent",
                 ttl_s: float = 1800, max_sessions: int = 500, max_turns: int = 40, rate_per_min: int = 15,
                 max_text: int = 1000) -> None:
        self.factory, self.sender, self.now_fn, self.salt = factory, sender, now_fn, salt
        self.ttl_s, self.max_sessions, self.max_turns, self.rate_per_min, self.max_text = ttl_s, max_sessions, max_turns, rate_per_min, max_text
        self._sessions: dict[str, _Session] = {}
        self._tasks: set[asyncio.Task] = set()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._hits: dict[str, deque] = {}
        self._warned: set[str] = set()
        self.last_error: Exception | None = None

    # -- публичное
    def user_key(self, user_id: str) -> str:
        return hashlib.sha256(f"{self.salt}:{user_id}".encode()).hexdigest()[:16]

    def submit(self, events: list[dict]) -> None:
        """Берёт события в работу и возвращается сразу (вебхук должен ответить 200 быстро)."""
        for ev in events:
            t = asyncio.create_task(self._handle(ev))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def aclose(self) -> None:
        await self.drain()
        sessions = list(self._sessions.values())
        for s in sessions:
            s.stop()
        await asyncio.gather(*(s.task for s in sessions), return_exceptions=True)

    def agent_of(self, user_id: str) -> Any:
        s = self._sessions.get(self.user_key(user_id))
        return s.agent if s else None

    # -- внутреннее
    async def _handle(self, ev: dict) -> None:
        try:
            await self._dispatch(ev)
        except Exception as e:  # noqa: BLE001  — одно плохое событие не должно ронять остальные
            self.last_error = e
            log.exception("event failed: %s", type(e).__name__)

    def _fresh(self, ev: dict) -> bool:
        eid = ev.get("webhookEventId")
        if not eid:
            return True
        if eid in self._seen:
            return False
        self._seen[eid] = None
        while len(self._seen) > 5000:
            self._seen.popitem(last=False)
        return True

    def _allow(self, key: str) -> bool | None:
        """True — можно; False — первое превышение (ответить предупреждением); None — молча пропустить."""
        now, q = time.monotonic(), self._hits.setdefault(key, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) < self.rate_per_min:
            q.append(now)
            self._warned.discard(key)
            return True
        if key in self._warned:
            return None
        self._warned.add(key)
        return False

    async def _send(self, token: str | None, user_id: str, texts: list[str]) -> None:
        try:
            if not token:
                raise httpx.HTTPError("no reply token")
            await self.sender.reply(token, texts)
        except Exception:  # noqa: BLE001  — токен ответа истёк или не принят: пробуем Push
            try:
                await self.sender.push(user_id, texts)
            except Exception as e:  # noqa: BLE001
                self.last_error = e
                log.warning("send failed: %s", type(e).__name__)

    async def _dispatch(self, ev: dict) -> None:
        if not self._fresh(ev):
            return
        src = ev.get("source") or {}
        uid = src.get("userId")
        if src.get("type") != "user" or not uid:
            return                                              # группы и комнаты: молчим
        token, kind = ev.get("replyToken"), ev.get("type")
        if kind == "follow":
            return await self._send(token, uid, [GREETING])
        if kind != "message":
            return
        msg = ev.get("message") or {}
        if msg.get("type") != "text":
            return await self._send(token, uid, [UNSUPPORTED])
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if len(text) > self.max_text:
            return await self._send(token, uid, [TOO_LONG])
        key = self.user_key(uid)
        ok = self._allow(key)
        if ok is None:
            return
        if ok is False:
            return await self._send(token, uid, [THROTTLED])
        try:
            reply = await self._ask(key, text)
        except _TurnLimit:
            return await self._send(token, uid, [TURN_LIMIT])
        except Exception as e:  # noqa: BLE001
            self.last_error = e
            log.warning("agent failed: %s", type(e).__name__)
            return await self._send(token, uid, [APOLOGY])
        await self._send(token, uid, split_text(reply))

    async def _ask(self, key: str, text: str) -> str:
        for _ in range(3):
            s = self._sessions.get(key)
            if s is None or s.closed:
                self._make_room()
                s = self._sessions[key] = _Session(self, key)
            try:
                return await s.ask(text)
            except _Closed:
                continue
        raise _Closed()

    def _make_room(self) -> None:
        while len(self._sessions) >= self.max_sessions:
            oldest = next(iter(self._sessions))                  # самая давняя по созданию
            self._sessions.pop(oldest).stop()


# ---------- HTTP ----------
@dataclass(frozen=True)
class LineSettings:
    channel_secret: str
    access_token: str = ""
    user_salt: str = "dental-agent"

    @staticmethod
    def from_env() -> "LineSettings":
        from dental.agent.llm import _env            # окружение, затем .env (как для ключей LLM и DATABASE_URL)
        secret = _env("LINE_CHANNEL_SECRET") or ""
        if not secret:
            raise SystemExit("нет LINE_CHANNEL_SECRET: без него подпись вебхука проверить нечем, сервер не запускается")
        return LineSettings(secret, _env("LINE_CHANNEL_ACCESS_TOKEN") or "", _env("LINE_USER_SALT") or "dental-agent")


def create_app(secret: str, gateway: Gateway) -> Starlette:
    async def webhook(request: Request) -> Response:
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return Response(status_code=413)
        body = await request.body()
        if len(body) > MAX_BODY:
            return Response(status_code=413)
        if not verify_signature(secret, body, request.headers.get("x-line-signature")):
            return Response(status_code=401)                     # до разбора тела: чужой запрос ничего не запускает
        try:
            events = json.loads(body).get("events", [])
            if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
                raise ValueError
        except (ValueError, AttributeError):
            return Response(status_code=400)
        gateway.submit(events)
        return JSONResponse({})

    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True})

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        yield
        await gateway.aclose()

    return Starlette(routes=[Route("/line/webhook", webhook, methods=["POST"]), Route("/healthz", health)], lifespan=lifespan)

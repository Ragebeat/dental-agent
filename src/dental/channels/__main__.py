"""Канал LINE.

  uv run python -m dental.channels emulate --profile local     # консоль «как LINE»: реплики идут подписанными событиями через вебхук
  uv run python -m dental.channels check                        # проверить реквизиты из .env (токен спрашивает у LINE, секрет — локально)
  uv run python -m dental.channels serve --profile local        # настоящий вебхук на 127.0.0.1:8080 (нужны LINE_CHANNEL_SECRET и LINE_CHANNEL_ACCESS_TOKEN)

Часы по умолчанию замороженные (как в синтетической базе); --real-clock берёт настоящее время.
Наружу вебхук публикуют через HTTPS-туннель/прокси (LINE требует HTTPS); сам сервер слушает только 127.0.0.1."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from dental.agent.llm import get_llm
from dental.channels.eval_bridge import LineAgent
from dental.channels.line import Gateway, LineApi, LineSettings, McpSessions, RecordingSender, create_app
from dental.config import load_config
from dental.core.service import ClinicCore


def _now_fn(real: bool):
    cfg = load_config()
    return (lambda: datetime.now(ZoneInfo("Asia/Tokyo"))) if real else cfg.now


async def _emulate(profile: str, real: bool) -> None:
    from dental.eval.runner import RunSettings
    now_fn = _now_fn(real)
    agent = LineAgent(ClinicCore(), get_llm(profile), now_fn(), RunSettings(agent="graph", channel="line"))
    print("эмулятор LINE (пустая строка или Ctrl+C — выход)")
    try:
        while (text := await asyncio.to_thread(input, "вы> ")).strip():
            print("бот>", await agent.reply(text))
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        await agent.aclose()


def _check() -> None:
    """Токен проверяет сам LINE (GET /v2/bot/info). Секрет у LINE напрямую не проверить: убеждаемся, что он есть, и прогоняем
    подписанный запрос через собственный вебхук; окончательная проверка секрета — кнопка Verify в консоли LINE Developers."""
    import httpx

    from dental.channels.line import sign, verify_signature
    s = LineSettings.from_env()
    print(f"LINE_CHANNEL_SECRET: задан ({len(s.channel_secret)} симв.)")
    body = b'{"events":[]}'
    print("подпись туда-обратно:", "ok" if verify_signature(s.channel_secret, body, sign(s.channel_secret, body)) else "ОШИБКА")
    if not s.access_token:
        raise SystemExit("LINE_CHANNEL_ACCESS_TOKEN не задан")
    r = httpx.get("https://api.line.me/v2/bot/info", headers={"Authorization": f"Bearer {s.access_token}"}, timeout=10)
    if r.status_code != 200:
        raise SystemExit(f"токен не принят LINE: HTTP {r.status_code}")
    info = r.json()
    print(f"токен принят: бот «{info.get('displayName')}», basicId {info.get('basicId')}")
    print("LINE_USER_SALT:", "задан" if s.user_salt != "dental-agent" else "по умолчанию (задайте свою)")


def _serve(profile: str, real: bool, port: int) -> None:
    import uvicorn
    s = LineSettings.from_env()
    sender = LineApi(s.access_token) if s.access_token else RecordingSender()
    if not s.access_token:
        print("LINE_CHANNEL_ACCESS_TOKEN не задан: ответы не уходят в LINE (режим записи в память)")
    now_fn = _now_fn(real)
    gw = Gateway(McpSessions(ClinicCore(), get_llm(profile), now_fn), sender, now_fn=now_fn, salt=s.user_salt)
    uvicorn.run(create_app(s.channel_secret, gw), host="127.0.0.1", port=port, log_level="warning")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["emulate", "serve", "check"])
    ap.add_argument("--profile", default="agent", help="профиль LLM из llm.toml (запасной путь графа)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--real-clock", action="store_true")
    a = ap.parse_args(argv)
    if a.mode == "check":
        _check()
    elif a.mode == "emulate":
        asyncio.run(_emulate(a.profile, a.real_clock))
    else:
        _serve(a.profile, a.real_clock, a.port)


if __name__ == "__main__":
    main()

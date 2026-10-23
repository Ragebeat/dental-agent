"""LINE channel.

  uv run python -m dental.channels emulate --profile local     # a LINE-like console: messages go as signed events through the webhook
  uv run python -m dental.channels check                        # check the credentials in .env (the token is asked of LINE, the secret is checked locally)
  uv run python -m dental.channels serve --profile local        # the real webhook on 127.0.0.1:8080 (needs LINE_CHANNEL_SECRET and LINE_CHANNEL_ACCESS_TOKEN)

The clock is frozen by default (as in the synthetic database); --real-clock uses the real time.
The webhook is exposed through an HTTPS tunnel/proxy (LINE requires HTTPS); the server itself listens on 127.0.0.1 only."""

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
    print("LINE emulator (empty line or Ctrl+C to exit)")
    try:
        while (text := await asyncio.to_thread(input, "you> ")).strip():
            print("bot>", await agent.reply(text))
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        await agent.aclose()


def _check() -> None:
    """LINE itself checks the token (GET /v2/bot/info). The secret cannot be checked with LINE directly: we make sure it is set and send
    a signed request through our own webhook; the final check of the secret is the Verify button in the LINE Developers console."""
    import httpx

    from dental.channels.line import sign, verify_signature
    s = LineSettings.from_env()
    print(f"LINE_CHANNEL_SECRET: set ({len(s.channel_secret)} chars)")
    body = b'{"events":[]}'
    print("signature round trip:", "ok" if verify_signature(s.channel_secret, body, sign(s.channel_secret, body)) else "ERROR")
    if not s.access_token:
        raise SystemExit("LINE_CHANNEL_ACCESS_TOKEN is not set")
    r = httpx.get("https://api.line.me/v2/bot/info", headers={"Authorization": f"Bearer {s.access_token}"}, timeout=10)
    if r.status_code != 200:
        raise SystemExit(f"LINE rejected the token: HTTP {r.status_code}")
    info = r.json()
    print(f"token accepted: bot \"{info.get('displayName')}\", basicId {info.get('basicId')}")
    print("LINE_USER_SALT:", "set" if s.user_salt != "dental-agent" else "default (set your own)")


def _serve(profile: str, real: bool, port: int) -> None:
    import uvicorn
    s = LineSettings.from_env()
    sender = LineApi(s.access_token) if s.access_token else RecordingSender()
    if not s.access_token:
        print("LINE_CHANNEL_ACCESS_TOKEN is not set: replies do not go to LINE (in-memory mode)")
    now_fn = _now_fn(real)
    gw = Gateway(McpSessions(ClinicCore(), get_llm(profile), now_fn), sender, now_fn=now_fn, salt=s.user_salt)
    uvicorn.run(create_app(s.channel_secret, gw), host="127.0.0.1", port=port, log_level="warning")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["emulate", "serve", "check"])
    ap.add_argument("--profile", default="agent", help="LLM profile from llm.toml (the graph's fallback path)")
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

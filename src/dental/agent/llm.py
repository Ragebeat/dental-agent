"""Адаптер LLM: один интерфейс, любой OpenAI-совместимый эндпоинт (Ollama, OpenRouter, ...).

Смена модели = смена профиля в llm.toml; код агента, симулятора и оценки не меняется.
"""

from __future__ import annotations

import json
import os
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from dental import tracing
from dental.config import ROOT

LLM_TOML = ROOT / "llm.toml"


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMReply:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None
    latency_s: float = 0.0
    finish: str = ""          # finish_reason: stop | length | ...

    def assistant_message(self) -> dict:
        msg: dict = {"role": "assistant", "content": self.text or ""}
        if self.tool_calls:
            msg["tool_calls"] = [{"id": c.id, "type": "function",
                                  "function": {"name": c.name, "arguments": json.dumps(c.args, ensure_ascii=False)}}
                                 for c in self.tool_calls]
        return msg


@dataclass(frozen=True)
class Profile:
    name: str
    base_url: str
    model: str
    api_key_env: str = ""
    temperature: float = 0.0
    timeout_s: float = 120
    max_tokens: int = 1024
    extra_body: dict = field(default_factory=dict)

    def fingerprint(self) -> dict:      # без ключа
        return {"name": self.name, "base_url": self.base_url, "model": self.model,
                "temperature": self.temperature, "max_tokens": self.max_tokens, "extra_body": self.extra_body}


def _env(name: str) -> str | None:
    if not name:
        return None
    if os.environ.get(name):
        return os.environ[name]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def load_profiles(path: Path = LLM_TOML) -> tuple[dict[str, Profile], dict[str, str]]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    profiles = {n: Profile(name=n, **p) for n, p in raw.get("profiles", {}).items()}
    return profiles, raw.get("roles", {})


class LLM:
    profile: Profile

    def chat(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int | None = None) -> LLMReply:   # pragma: no cover
        raise NotImplementedError


class OpenAICompat(LLM):
    def __init__(self, profile: Profile, client: httpx.Client | None = None) -> None:
        self.profile = profile
        self.client = client or httpx.Client(timeout=profile.timeout_s)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int | None = None) -> LLMReply:
        with tracing.span("llm.chat", "LLM") as sp:
            tracing.llm_request(sp, self.profile, messages, tools, max_tokens)
            r = self._chat(messages, tools, max_tokens)
            tracing.llm_response(sp, r)
            return r

    def _chat(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int | None = None) -> LLMReply:
        p = self.profile
        body = {"model": p.model, "messages": messages, "temperature": p.temperature, "max_tokens": max_tokens or p.max_tokens,
                **p.extra_body}
        if tools:
            body["tools"] = tools
        headers = {"Content-Type": "application/json"}
        key = _env(p.api_key_env)
        if p.api_key_env and not key:
            raise LLMError(f"нет ключа: задайте {p.api_key_env} в окружении или .env")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        last: Exception | None = None
        for attempt in range(4):
            t0 = time.perf_counter()
            try:
                r = self.client.post(p.base_url.rstrip("/") + "/chat/completions", json=body, headers=headers)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
                if r.status_code >= 400:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")      # без повторов: ошибка запроса
                return self._parse(r.json(), time.perf_counter() - t0)
            except (httpx.HTTPError, LLMError) as e:
                last = e
                if isinstance(e, LLMError) and "HTTP 4" in str(e) and "HTTP 429" not in str(e):
                    break
                time.sleep(2 ** attempt)
        raise LLMError(f"{p.name}: {last}")

    @staticmethod
    def _parse(data: dict, latency: float) -> LLMReply:
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError):
            raise LLMError(f"неожиданный ответ: {str(data)[:300]}") from None
        calls = []
        for i, c in enumerate(msg.get("tool_calls") or []):
            fn = c.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    args = {"__invalid_json__": args[:200]}
            calls.append(ToolCall(c.get("id") or f"call_{i}", fn.get("name", ""), args if isinstance(args, dict) else {}))
        u = data.get("usage") or {}
        return LLMReply(text=msg.get("content"), tool_calls=calls, prompt_tokens=u.get("prompt_tokens", 0),
                        completion_tokens=u.get("completion_tokens", 0),
                        cost=u.get("cost"), latency_s=latency,
                        finish=data["choices"][0].get("finish_reason") or "")


class FakeLLM(LLM):
    """Для тестов: на каждый вызов отдаёт следующий элемент сценария (LLMReply или функция messages → LLMReply)."""

    def __init__(self, script, name: str = "fake") -> None:
        self.script = list(script)
        self.calls: list[list[dict]] = []
        self.profile = Profile(name=name, base_url="fake://", model=name)

    def chat(self, messages, tools=None, max_tokens=None) -> LLMReply:
        self.calls.append([dict(m) for m in messages])
        if not self.script:
            raise LLMError("FakeLLM: сценарий закончился")
        item = self.script.pop(0)
        return item(messages) if callable(item) else item


class ManualLLM(LLM):
    """Маркер ручного режима: роль симулятора играет человек или Claude через файлы (см. dental.eval.manual)."""
    manual = True

    def __init__(self) -> None:
        self.profile = Profile(name="manual", base_url="manual://", model="manual-human-or-claude")

    def chat(self, messages, tools=None, max_tokens=None):   # pragma: no cover
        raise LLMError("ManualLLM не отвечает сам: используйте ManualSimulator")


def get_llm(role_or_profile: str, path: Path = LLM_TOML) -> LLM:
    profiles, roles = load_profiles(path)
    name = roles.get(role_or_profile, role_or_profile)
    if name == "manual":
        return ManualLLM()
    if name not in profiles:
        raise LLMError(f"нет профиля '{name}' в {path.name}; есть: {', '.join(profiles)}")
    return OpenAICompat(profiles[name])

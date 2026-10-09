"""Трассировка: спаны OpenTelemetry с атрибутами OpenInference, смотрим в Phoenix.

Трасса — один диалог от начала до конца, спаны — его шаги: ход пациента → узел графа → вызов LLM / инструмента MCP.
Журнал прогонов (data/runs.jsonl) говорит, ЧЕМ закончился прогон; трасса — ЧТО происходило внутри одного диалога.

Паттерн перенесён из проекта RAG (там же обоснования): спаны ручные, атрибуты по OpenInference, бэкенд — настройка.

Включается явно. Без TRACE=1 (или tracing.setup(enable=True), например eval --trace) трассировщик пустой,
ничего не отправляется, тесты и прогоны от сервера Phoenix не зависят. Без установленного opentelemetry — тоже no-op.

  docker run -d -p 127.0.0.1:6006:6006 --name phoenix arizephoenix/phoenix   # если ещё не запущен (адрес 127.0.0.1, не localhost)
  uv run python -m dental.eval.run --agent graph --scenarios s01 --repeats 1 --trace
  -> http://127.0.0.1:6006, проект dental-agent

Содержимое ВЫКЛЮЧЕНО по умолчанию (клиника — медицинские данные, SPEC: «трассы без содержимого диалогов»): реплики пациента и
агента, промпты, аргументы инструментов заменяются на «[скрыто: N симв.]», длина остаётся. Метаданные (узлы, токены, задержки,
finish_reason, коды результатов инструментов, ссылки, вердикт) пишутся всегда. Для отладки на синтетике:

  $env:TRACE_CONTENT=1

и тогда текст всё равно проходит scrub(): телефоны, даты рождения, 6-значные номера карт → [PHONE] [BIRTH] [CARD].
Имена регулярками не поймать — полное содержимое пригодно только для синтетических данных, не для реальных пациентов.

Маска живёт в одном месте — set_attrs(); вызвать запись атрибута в обход неё нельзя.
"""

from __future__ import annotations

import contextlib
import json
import os
import re

try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode
    _OTEL = True
except ImportError:          # pragma: no cover  — opentelemetry не установлен: трассировки нет, код работает
    _OTEL = False

PROJECT = os.environ.get("TRACE_PROJECT", "dental-agent")
# HTTP на порт UI Phoenix, задан явно (register() по умолчанию идёт в gRPC 4317, который из docker не опубликован).
# 127.0.0.1, а не localhost: на Windows localhost сначала разрешается в IPv6 и каждый запрос ждёт таймаут.
ENDPOINT = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://127.0.0.1:6006/v1/traces")
MAX_TEXT = 8000

CONTENT = os.environ.get("TRACE_CONTENT") == "1"
CONTENT_KEYS = {"input.value", "output.value", "tool.args"}
CONTENT_SUFFIXES = (".message.content", ".function.arguments")

_tracer = trace.get_tracer("dental-agent") if _OTEL else None
_enabled = False

_PHONE = re.compile(r"(?<!\d)0\d{1,4}[-ー−\s]?\d{1,4}[-ー−\s]?\d{3,4}(?!\d)")
# годы до 2025: даты записи (2026+) под маску рождения не попадают; когда «сейчас» уйдёт дальше, границу нужно двигать
_BIRTH = re.compile(r"(?<!\d)(?:19\d{2}|20[01]\d|202[0-5])\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?")
_CARD = re.compile(r"(?<!\d)\d{6}(?!\d)")


def scrub(text: str) -> str:
    """Телефоны, даты рождения, номера карт → метки. Применяется ко всему содержимому, попадающему в трассу."""
    text = _BIRTH.sub("[BIRTH]", text)
    text = _PHONE.sub("[PHONE]", text)
    return _CARD.sub("[CARD]", text)


def enabled() -> bool:
    return _enabled


def setup(project: str | None = None, enable: bool = False) -> bool:
    """Вызвать один раз в точке входа. Ничего не делает без TRACE=1 / enable=True. Возвращает, включена ли трассировка."""
    global _tracer, _enabled
    if not (enable or os.environ.get("TRACE") == "1") or not _OTEL:
        return False
    from phoenix.otel import register
    # auto_instrument=False: клиент LLM свой (httpx), автоинструментировать нечего, а скрытые спаны хуже отсутствующих.
    provider = register(project_name=project or PROJECT, endpoint=ENDPOINT, auto_instrument=False)
    _tracer, _enabled = provider.get_tracer("dental-agent"), True
    return True


def use_provider(provider) -> None:
    """Для тестов: подставить провайдер с in-memory экспортёром."""
    global _tracer, _enabled
    _tracer, _enabled = provider.get_tracer("dental-agent"), True


def _text(v) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + f"…[+{len(s) - MAX_TEXT}]"


def _ok(v) -> bool:
    prim = (str, bool, int, float)
    return isinstance(v, prim) or (isinstance(v, (list, tuple)) and all(isinstance(x, prim) for x in v))


def _is_content(key: str) -> bool:
    return key in CONTENT_KEYS or key.endswith(CONTENT_SUFFIXES)


def _redact(v) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return f"[скрыто: {len(s)} симв.]"


def set_attrs(sp, attrs: dict | None) -> None:
    """Единственное место записи атрибутов: маска содержимого здесь, обойти её нельзя."""
    for k, v in (attrs or {}).items():
        if v is None:
            continue
        if _is_content(k):
            v = scrub(_text(v)) if CONTENT else _redact(v)
        sp.set_attribute(k, v if _ok(v) else _text(v))


class _NoSpan:
    def set_attribute(self, *a, **k): ...
    def set_status(self, *a, **k): ...
    def record_exception(self, *a, **k): ...


@contextlib.contextmanager
def span(name: str, kind: str, input=None, attrs: dict | None = None):
    """Спан вида OpenInference: AGENT, CHAIN, LLM, TOOL. Статус ставится явно (ручной спан иначе уходит UNSET)."""
    if not (_OTEL and _enabled):
        yield _NoSpan()
        return
    with _tracer.start_as_current_span(name) as sp:
        sp.set_attribute("openinference.span.kind", kind)
        if input is not None:
            set_attrs(sp, {"input.value": input})
        set_attrs(sp, attrs)
        try:
            yield sp
        except Exception as e:
            # Текст исключения может нести содержимое (ошибка запроса повторяет реплику): без флага — только тип.
            if CONTENT:
                sp.record_exception(e)
                desc = scrub(str(e)[:200])
            else:
                sp.set_attribute("exception.type", type(e).__name__)
                desc = type(e).__name__
            sp.set_status(Status(StatusCode.ERROR, desc))
            raise
        sp.set_status(Status(StatusCode.OK))


def llm_request(sp, profile, messages: list[dict], tools=None, max_tokens=None) -> None:
    """История чата плоским списком (OTel не принимает список словарей — он молча теряется)."""
    attrs = {"llm.model_name": profile.model, "llm.provider": profile.name,
             "llm.invocation_parameters": json.dumps({"temperature": profile.temperature, "max_tokens": max_tokens or profile.max_tokens})}
    for i, m in enumerate(messages):
        attrs[f"llm.input_messages.{i}.message.role"] = m.get("role", "")
        attrs[f"llm.input_messages.{i}.message.content"] = m.get("content") or ""
    if tools:
        attrs["llm.tools.count"] = len(tools)
    set_attrs(sp, attrs)


def llm_response(sp, reply) -> None:
    """Токены, задержка и finish_reason: length означает, что ответ оборван лимитом (так нашли обрыв в сводке)."""
    attrs = {
        "llm.output_messages.0.message.role": "assistant",
        "llm.output_messages.0.message.content": reply.text or "",
        "output.value": reply.text or "",
        "llm.token_count.prompt": reply.prompt_tokens,
        "llm.token_count.completion": reply.completion_tokens,
        "llm.token_count.total": reply.prompt_tokens + reply.completion_tokens,
        "llm.latency_s": round(reply.latency_s, 3),
        "llm.finish_reason": reply.finish,
        "llm.tool_calls.count": len(reply.tool_calls),
    }
    if reply.cost is not None:
        attrs["llm.cost_usd"] = float(reply.cost)
    set_attrs(sp, attrs)

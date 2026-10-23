"""Tracing: OpenTelemetry spans with OpenInference attributes, viewed in Phoenix.

A trace is one dialogue from start to end; spans are its steps: patient turn → graph node → LLM call / MCP tool call.
The run log (data/runs.jsonl) says HOW a run ended; a trace says WHAT happened inside one dialogue.

The pattern is carried over from the RAG project (the reasoning is there too): manual spans, OpenInference attributes, the backend is a setting.

Opt-in. Without TRACE=1 (or tracing.setup(enable=True), e.g. eval --trace) the tracer is empty,
nothing is sent, and tests and runs do not depend on a Phoenix server. Without opentelemetry installed it is also a no-op.

  docker run -d -p 127.0.0.1:6006:6006 --name phoenix arizephoenix/phoenix   # if not running yet (address 127.0.0.1, not localhost)
  uv run python -m dental.eval.run --agent graph --scenarios s01 --repeats 1 --trace
  -> http://127.0.0.1:6006, project dental-agent

Content is OFF by default (a clinic means medical data; SPEC: "traces without dialogue content"): patient and
agent messages, prompts and tool arguments are replaced with "[hidden: N chars]", the length stays. Metadata (nodes, tokens, latency,
finish_reason, tool result codes, references, the verdict) is always written. For debugging on synthetic data:

  $env:TRACE_CONTENT=1

and then the text still goes through scrub(): phones, birth dates, 6-digit card numbers → [PHONE] [BIRTH] [CARD].
Names cannot be caught with regexes — full content is fit only for synthetic data, not for real patients.

The mask lives in one place, set_attrs(); there is no way to write an attribute around it.
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
except ImportError:          # pragma: no cover  — opentelemetry not installed: no tracing, the code still works
    _OTEL = False

PROJECT = os.environ.get("TRACE_PROJECT", "dental-agent")
# HTTP to the Phoenix UI port, set explicitly (register() defaults to gRPC 4317, which the docker container does not publish).
# 127.0.0.1, not localhost: on Windows localhost resolves to IPv6 first and every request waits for a timeout.
ENDPOINT = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://127.0.0.1:6006/v1/traces")
MAX_TEXT = 8000

CONTENT = os.environ.get("TRACE_CONTENT") == "1"
CONTENT_KEYS = {"input.value", "output.value", "tool.args"}
CONTENT_SUFFIXES = (".message.content", ".function.arguments")

_tracer = trace.get_tracer("dental-agent") if _OTEL else None
_enabled = False

_PHONE = re.compile(r"(?<!\d)0\d{1,4}[-ー−\s]?\d{1,4}[-ー−\s]?\d{3,4}(?!\d)")
# years before 2025: appointment dates (2026+) do not fall under the birth-date mask; once "now" moves further, the boundary must move
_BIRTH = re.compile(r"(?<!\d)(?:19\d{2}|20[01]\d|202[0-5])\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?")
_CARD = re.compile(r"(?<!\d)\d{6}(?!\d)")


def scrub(text: str) -> str:
    """Phones, birth dates, card numbers → labels. Applied to all content that enters a trace."""
    text = _BIRTH.sub("[BIRTH]", text)
    text = _PHONE.sub("[PHONE]", text)
    return _CARD.sub("[CARD]", text)


def enabled() -> bool:
    return _enabled


def setup(project: str | None = None, enable: bool = False) -> bool:
    """Call once at the entry point. Does nothing without TRACE=1 / enable=True. Returns whether tracing is on."""
    global _tracer, _enabled
    if not (enable or os.environ.get("TRACE") == "1") or not _OTEL:
        return False
    from phoenix.otel import register
    # auto_instrument=False: the LLM client is our own (httpx), there is nothing to auto-instrument, and hidden spans are worse than none.
    provider = register(project_name=project or PROJECT, endpoint=ENDPOINT, auto_instrument=False)
    _tracer, _enabled = provider.get_tracer("dental-agent"), True
    return True


def use_provider(provider) -> None:
    """For tests: plug in a provider with an in-memory exporter."""
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
    return f"[hidden: {len(s)} chars]"


def set_attrs(sp, attrs: dict | None) -> None:
    """The only place attributes are written: the content mask is here and cannot be bypassed."""
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
    """An OpenInference-style span: AGENT, CHAIN, LLM, TOOL. The status is set explicitly (a manual span otherwise ends up UNSET)."""
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
            # An exception's text can carry content (a request error repeats the message): without the flag, only the type.
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
    """Chat history as a flat list (OTel does not accept a list of dicts — it is silently dropped)."""
    attrs = {"llm.model_name": profile.model, "llm.provider": profile.name,
             "llm.invocation_parameters": json.dumps({"temperature": profile.temperature, "max_tokens": max_tokens or profile.max_tokens})}
    for i, m in enumerate(messages):
        attrs[f"llm.input_messages.{i}.message.role"] = m.get("role", "")
        attrs[f"llm.input_messages.{i}.message.content"] = m.get("content") or ""
    if tools:
        attrs["llm.tools.count"] = len(tools)
    set_attrs(sp, attrs)


def llm_response(sp, reply) -> None:
    """Tokens, latency and finish_reason: length means the answer was cut off by the limit (that is how the cut-off in the summary was found)."""
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

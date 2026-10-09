"""Адаптер HTTP (на подложке транспорта), отпечаток конфигурации, CLI run + compare на поддельных LLM."""

import json

import httpx
import pytest

from dental.agent.llm import LLM, LLMError, LLMReply, OpenAICompat, Profile, ToolCall
from dental.eval import compare, run as run_mod

P = Profile(name="t", base_url="http://x/v1", model="m", api_key_env="")


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_body(msg, usage=None):
    return {"choices": [{"message": msg}], "usage": usage or {"prompt_tokens": 5, "completion_tokens": 2, "cost": 0.001}}


def test_adapter_parses_tool_calls_and_usage():
    def h(req):
        body = json.loads(req.content)
        assert body["model"] == "m" and body["tools"] and body["temperature"] == 0.0
        return httpx.Response(200, json=ok_body({"content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "search_slots", "arguments": "{\"service\": \"cleaning\"}"}},
            {"id": "b", "type": "function", "function": {"name": "x", "arguments": "{oops"}}]}))
    r = OpenAICompat(P, client(h)).chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
    assert r.tool_calls[0].args == {"service": "cleaning"} and "__invalid_json__" in r.tool_calls[1].args
    assert (r.prompt_tokens, r.completion_tokens, r.cost) == (5, 2, 0.001)
    am = r.assistant_message()
    assert am["tool_calls"][0]["function"]["arguments"] == '{"service": "cleaning"}'


def test_adapter_retries_5xx_but_not_4xx(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []

    def flaky(req):
        calls.append(1)
        return httpx.Response(503, text="busy") if len(calls) < 3 else httpx.Response(200, json=ok_body({"content": "ok"}))
    assert OpenAICompat(P, client(flaky)).chat([]).text == "ok" and len(calls) == 3
    calls.clear()

    def bad(req):
        calls.append(1)
        return httpx.Response(401, text="no")
    with pytest.raises(LLMError):
        OpenAICompat(P, client(bad)).chat([])
    assert len(calls) == 1


def test_adapter_needs_key(monkeypatch):
    monkeypatch.delenv("SOME_KEY", raising=False)
    p = Profile(name="t", base_url="http://x/v1", model="m", api_key_env="SOME_KEY")
    with pytest.raises(LLMError, match="SOME_KEY"):
        OpenAICompat(p, client(lambda r: httpx.Response(200, json={}))).chat([])
    monkeypatch.setenv("SOME_KEY", "secret")
    seen = {}

    def h(req):
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json=ok_body({"content": "hi"}))
    OpenAICompat(p, client(h)).chat([])
    assert seen["auth"] == "Bearer secret"
    assert "secret" not in json.dumps(p.fingerprint())


class StatelessAgent(LLM):
    def __init__(self, model="fake-a"):
        self.profile = Profile(name=model, base_url="fake://", model=model)

    def chat(self, messages, tools=None):
        n = sum(m["role"] == "tool" for m in messages)
        if n == 0 and messages[-1]["role"] == "user":
            return LLMReply(None, [ToolCall("1", "verify_identity", {"card_number": 100001, "birth_date": "1988-04-12"})])
        return LLMReply("承知しました。", prompt_tokens=10, completion_tokens=3, latency_s=0.1)


class StatelessSim(LLM):
    def __init__(self):
        self.profile = Profile(name="fake-s", base_url="fake://", model="fake-s")

    def chat(self, messages, tools=None):
        return LLMReply("お願いします。" if len(messages) <= 2 else "ありがとう <END>")


def test_cli_run_journal_and_compare(tmp_path, monkeypatch, capsys, admin_url):
    monkeypatch.setattr(run_mod, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(run_mod, "JOURNAL", tmp_path / "runs.jsonl")
    monkeypatch.setattr(compare, "JOURNAL", tmp_path / "runs.jsonl")
    models = iter(["fake-a", "fake-b"])
    monkeypatch.setattr(run_mod, "get_llm", lambda name: StatelessSim() if name == "sim" else StatelessAgent(next(models)))
    for label in ("one", "two"):
        run_mod.main(["--agent-profile", "agent", "--sim-profile", "sim", "--scenarios", "s01,s05,s13",
                      "--repeats", "2", "--workers", "2", "--label", label])
    rows = [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 and rows[0]["metrics"]["runs"] == 6
    fa, fb = rows[0]["fingerprint"], rows[1]["fingerprint"]
    assert fa["agent"]["model"] == "fake-a" and fb["agent"]["model"] == "fake-b"
    assert fa["prompt"] == fb["prompt"] and fa["tools"] == fb["tools"] and fa["scenarios"] == fb["scenarios"]
    assert len(list((tmp_path / "runs").glob("*.json"))) == 2
    capsys.readouterr()
    compare.main([])
    out = capsys.readouterr().out
    assert "отличия конфигурации" in out and "agent" in out and "outcome_rate" in out
    compare.main(["--list"])
    assert "fake-a" in capsys.readouterr().out

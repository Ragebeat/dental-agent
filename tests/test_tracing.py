"""Трассировка: метаданные пишутся, содержимое и персональные данные в трассу не попадают (без базы и без Phoenix)."""
import httpx
import pytest

pytest.importorskip("opentelemetry.sdk")
from opentelemetry.sdk.trace import TracerProvider                       # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor           # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter   # noqa: E402

from dental import tracing                                                # noqa: E402
from dental.agent.llm import OpenAICompat, Profile                        # noqa: E402
from graph_helpers import Dialog                                          # noqa: E402

ID = "診察券番号は100001、生年月日は1988年4月12日です。"
SECRETS = ["100001", "1988", "鈴木", "090-1234-5678", "クリーニングの予約をお願い"]


@pytest.fixture
def spans(monkeypatch):
    exp = InMemorySpanExporter()
    prov = TracerProvider()
    prov.add_span_processor(SimpleSpanProcessor(exp))
    tracing.use_provider(prov)
    yield exp
    monkeypatch.setattr(tracing, "_enabled", False)


def _blob(exp) -> str:
    return " ".join(f"{k}={v}" for s in exp.get_finished_spans() for k, v in s.attributes.items())


def _dialog():
    d = Dialog()
    d.say("クリーニングの予約をお願いしたいです", ID, "来週の水曜の午後", "2番でお願いします", "はい")
    return d


def test_scrub_masks_phone_birth_card_but_not_appointment_dates():
    t = tracing.scrub("090-1234-5678 / 生年月日は1988年4月12日 / 1979-11-03 / カード100001 / 2026-10-14T15:30 / 10月14日 15:30")
    assert "[PHONE]" in t and t.count("[BIRTH]") == 2 and "[CARD]" in t
    assert "2026-10-14T15:30" in t and "10月14日 15:30" in t
    for bad in ("090-1234-5678", "1988", "1979-11-03", "100001"):
        assert bad not in t


def test_dialog_trace_has_structure_and_no_content_by_default(spans, monkeypatch):
    monkeypatch.setattr(tracing, "CONTENT", False)
    d = _dialog()
    assert "ご予約を承りました" in d.replies[-1]
    got = spans.get_finished_spans()
    names = [s.name for s in got]
    assert names.count("turn") == 5 and "node.ingest" in names and "tool.verify_identity" in names and "tool.book_appointment" in names
    turn_ids = {s.context.span_id for s in got if s.name == "turn"}
    assert all(s.parent.span_id in turn_ids for s in got if s.name.startswith("node."))           # узлы вложены в ход
    verify = next(s for s in got if s.name == "tool.verify_identity")
    assert verify.attributes["tool.ok"] is True
    blob = _blob(spans)
    for secret in SECRETS:
        assert secret not in blob, secret
    assert "[скрыто:" in blob


def test_content_flag_keeps_text_but_scrubs_personal_data(spans, monkeypatch):
    monkeypatch.setattr(tracing, "CONTENT", True)
    _dialog()
    blob = _blob(spans)
    assert "クリーニングの予約をお願い" in blob and "[CARD]" in blob and "[BIRTH]" in blob
    assert "100001" not in blob and "1988年4月12日" not in blob


def test_llm_span_records_finish_reason_and_tokens(spans, monkeypatch):
    monkeypatch.setattr(tracing, "CONTENT", False)
    body = {"choices": [{"message": {"content": "秘密のテキスト"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 1024}}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    OpenAICompat(Profile("t", "http://x/v1", "m"), client).chat([{"role": "user", "content": "秘密の質問"}])
    sp = next(s for s in spans.get_finished_spans() if s.name == "llm.chat")
    a = sp.attributes
    assert a["llm.finish_reason"] == "length" and a["llm.token_count.completion"] == 1024 and a["llm.token_count.total"] == 1144
    assert a["openinference.span.kind"] == "LLM" and "秘密" not in _blob(spans)


def test_without_setup_nothing_is_recorded_and_code_works():
    tracing._enabled = False
    with tracing.span("x", "CHAIN", input="secret") as sp:
        sp.set_attribute("a", 1)
    assert Dialog().say("こんにちは")

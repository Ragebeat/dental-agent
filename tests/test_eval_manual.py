"""Manual mode: a "player" (a thread) answers through files, the runner goes through the dialogue as usual."""

import threading

from dental.agent.llm import FakeLLM, LLMReply, ManualLLM, ToolCall, get_llm
from dental.config import load_config
from dental.eval import manual
from dental.eval.runner import run_scenario
from dental.scenarios import load_scenarios

CFG = load_config()
SC = {s.id.split("_")[0]: s for s in load_scenarios()}


def player(tmp, replies, stop):
    """Answers every awaiting_patient status with the next message, using the same commands as a real player."""
    import io, sys
    it = iter(replies)
    while not stop.is_set():
        for d, s in list(manual._waiting()):
            sys.stdin = io.StringIO(next(it))
            manual.cmd_say(d.name)
        stop.wait(0.05)


def test_manual_flow_end_to_end(tmp_path, monkeypatch, admin_url, template_db, capsys):
    monkeypatch.setattr(manual, "MANUAL_DIR", tmp_path)
    assert isinstance(get_llm("manual"), ManualLLM)
    stop = threading.Event()
    t = threading.Thread(target=player, args=(tmp_path, ["クリーニングをお願いします。診察券100001、1988年4月12日です。",
                                                       "ありがとうございました。<END>"], stop))
    t.start()
    agent = FakeLLM([LLMReply(None, [ToolCall("1", "verify_identity", {"card_number": 100001, "birth_date": "1988-04-12"})]),
                     LLMReply(None, [ToolCall("2", "book_appointment", {"service": "cleaning", "start": "2026-10-14T16:00"})]),
                     LLMReply("ご予約を承りました。"), LLMReply("どういたしまして。")])
    try:
        res = run_scenario(SC["s01"], CFG, agent, get_llm("manual"), admin_url=admin_url, template_db=template_db)
    finally:
        stop.set()
        t.join()
    assert res["verdict"]["outcome_ok"] and res["ended_by"] == "patient_end" and res["turns"] == 2
    d = next(tmp_path.iterdir())
    assert "ОПИСАНИЕ ТВОЕЙ РОЛИ" in (d / "persona.txt").read_text(encoding="utf-8")
    assert "expected" not in (d / "persona.txt").read_text(encoding="utf-8")        # the player does not see the expected outcome
    assert '"state": "done"' in (d / "status.json").read_text(encoding="utf-8")


def test_wait_shows_persona_once_and_say_rejects_double(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(manual, "MANUAL_DIR", tmp_path)
    sim = manual.ManualSimulator(SC["s01"], CFG.now(), timeout_s=0.2, poll_s=0.05)
    assert manual.cmd_wait(1) == 0 and "ROLE" in capsys.readouterr().out
    assert manual.cmd_wait(1) == 0 and "ROLE" not in capsys.readouterr().out
    import io, sys
    sys.stdin = io.StringIO("こんにちは")
    assert manual.cmd_say(sim.dir.name) == 0
    sys.stdin = io.StringIO("ещё раз")
    assert manual.cmd_say(sim.dir.name) == 2
    assert sim.first() == ("こんにちは", False)

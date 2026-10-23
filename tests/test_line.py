"""The LINE channel on stubs: signature, ordering, user isolation, redelivery, limits. No database, LLM or network."""
import asyncio
import contextlib
import json
from datetime import timedelta

import httpx

from dental.agent.graph import GraphAgent
from dental.channels import line
from dental.channels.line import Gateway, RecordingSender, create_app, sign
from graph_helpers import NOW, FakeClinic, llm_json

SECRET = "s3cret"
ID = "診察券番号は100001、生年月日は1988年4月12日です。"


class FakeSessions:
    def __init__(self):
        self.opened: list[str] = []
        self.clinics: dict[str, FakeClinic] = {}

    @contextlib.asynccontextmanager
    async def open(self, user_key, now):
        self.opened.append(user_key)
        clinic = self.clinics[user_key] = FakeClinic()
        yield GraphAgent(llm_json(), clinic, [], NOW)


def ev(uid, text, eid, token="tok", kind="message", src="user", mtype="text"):
    e = {"type": kind, "webhookEventId": eid, "replyToken": token, "source": {"type": src, "userId": uid}}
    if kind == "message":
        e["message"] = {"type": mtype, "text": text}
    return e


def env(**kw):
    f, s = FakeSessions(), RecordingSender()
    gw = Gateway(f, s, now_fn=lambda: NOW, **kw)
    return f, s, gw, create_app(SECRET, gw)


async def post(app, events, sig=None, raw=None):
    body = raw if raw is not None else json.dumps({"events": events}).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return await c.post("/line/webhook", content=body, headers={"x-line-signature": sign(SECRET, body) if sig is None else sig})


def run(coro):
    return asyncio.run(coro)


def test_signature_checked_before_anything_runs():
    async def main():
        f, s, gw, app = env()
        r1 = await post(app, [ev("U1", "こんにちは", "e1")], sig="wrong")
        r2 = await post(app, [ev("U1", "こんにちは", "e1")], sig="")
        r3 = await post(app, [ev("U1", "こんにちは", "e1")])
        await gw.drain()
        await gw.aclose()
        return r1.status_code, r2.status_code, r3.status_code, f.opened, len(s.sent)
    assert run(main()) == (401, 401, 200, ["%s" % line.Gateway(None, None, now_fn=lambda: NOW).user_key("U1")], 1)


def test_bad_json_is_400_and_empty_verify_event_is_200():
    async def main():
        _, _, gw, app = env()
        bad = await post(app, [], raw=b"not json")
        ok = await post(app, [])                         # the Verify button in the LINE console sends an empty event list
        await gw.aclose()
        return bad.status_code, ok.status_code
    assert run(main()) == (400, 200)


def test_booking_dialog_through_webhook_and_reply_tokens():
    async def main():
        f, s, gw, app = env()
        await post(app, [ev("U1", "クリーニングの予約をお願いしたいです", "e1", "t1")])
        await post(app, [ev("U1", ID, "e2", "t2")])
        await post(app, [ev("U1", "来週の水曜の午後", "e3", "t3")])
        await post(app, [ev("U1", "2番でお願いします", "e4", "t4")])
        await post(app, [ev("U1", "はい", "e5", "t5")])
        await gw.drain()
        calls = list(f.clinics.values())[0].calls
        await gw.aclose()
        return s, calls
    s, calls = run(main())
    assert [x[1] for x in s.sent] == ["t1", "t2", "t3", "t4", "t5"] and all(x[0] == "reply" for x in s.sent)
    assert "AIの予約アシスタント" in s.texts()[0] and "ご予約を承りました" in s.texts()[-1]
    assert [a for n, a in calls if n == "book_appointment"] == [{"service": "cleaning", "start": "2026-10-14T15:30"}]


def test_messages_sent_back_to_back_are_processed_in_order():
    async def main():
        _, s, gw, app = env()
        await post(app, [ev("U1", "クリーニングの予約をお願いしたいです", "e1", "t1"), ev("U1", ID, "e2", "t2"), ev("U1", "来週の水曜の午後", "e3", "t3")])
        await gw.drain()
        await gw.aclose()
        return s
    s = run(main())
    assert [x[1] for x in s.sent] == ["t1", "t2", "t3"] and "10月14日" in s.texts()[2]


def test_users_are_isolated():
    async def main():
        f, s, gw, app = env()
        await post(app, [ev("UA", "クリーニングの予約をお願いしたいです", "a1", "ta1"), ev("UA", ID, "a2", "ta2"),
                         ev("UB", "予約をキャンセルしたいです", "b1", "tb1")])
        await gw.drain()
        by = {tok: txt for (_, tok, _), txt in zip(s.sent, s.texts())}
        keys = list(f.clinics)
        ca, cb = f.clinics[gw.user_key("UA")], f.clinics[gw.user_key("UB")]
        await gw.aclose()
        return by, keys, [n for n, _ in ca.calls], [n for n, _ in cb.calls]
    by, keys, ca, cb = run(main())
    assert len(keys) == 2 and "verify_identity" in ca and "verify_identity" not in cb
    assert "診察券番号" in by["tb1"] and "ご希望の日にち" in by["ta2"]          # B is asked for the card, A's identity is already verified


def test_user_id_is_pseudonymised():
    async def main():
        f, _, gw, app = env(salt="x")
        await post(app, [ev("Uabc123", "こんにちは", "e1")])
        await gw.drain()
        await gw.aclose()
        return f.opened, gw.user_key("Uabc123"), line.Gateway(None, None, now_fn=lambda: NOW, salt="y").user_key("Uabc123")
    opened, k1, k2 = run(main())
    assert opened == [k1] and "Uabc123" not in k1 and len(k1) == 16 and k1 != k2


def test_redelivery_is_ignored():
    async def main():
        f, s, gw, app = env()
        await post(app, [ev("U1", "クリーニングの予約をお願いしたいです", "same", "t1")])
        await post(app, [ev("U1", "クリーニングの予約をお願いしたいです", "same", "t2")])
        await gw.drain()
        await gw.aclose()
        return s
    assert len(run(main()).sent) == 1


def test_groups_ignored_followers_greeted_non_text_and_long_text_handled():
    async def main():
        f, s, gw, app = env(max_text=50)
        await post(app, [ev("U1", "こんにちは", "g1", src="group"), ev("U1", "", "f1", "tf", kind="follow"),
                         ev("U1", "", "s1", "ts", mtype="sticker"), ev("U1", "あ" * 51, "l1", "tl")])
        await gw.drain()
        await gw.aclose()
        return f.opened, s.texts()
    opened, texts = run(main())
    assert opened == [] and texts == [line.GREETING, line.UNSUPPORTED, line.TOO_LONG]


def test_rate_limit_warns_once_then_stays_quiet():
    async def main():
        _, s, gw, app = env(rate_per_min=3)
        await post(app, [ev("U1", "こんにちは", f"r{i}", f"t{i}") for i in range(6)])
        await gw.drain()
        await gw.aclose()
        return s.texts()
    texts = run(main())
    assert len(texts) == 4 and texts.count(line.THROTTLED) == 1          # 3 handled, 1 warning, the rest silently


def test_failed_reply_falls_back_to_push():
    async def main():
        _, s, gw, app = env()
        s.fail_reply = True
        await post(app, [ev("U1", "こんにちは", "e1")])
        await gw.drain()
        await gw.aclose()
        return s.sent
    sent = run(main())
    assert len(sent) == 1 and sent[0][0] == "push" and sent[0][1] == "U1"


def test_idle_session_expires_and_a_new_one_is_created():
    async def main():
        f, s, gw, app = env(ttl_s=0.05)
        await post(app, [ev("U1", "こんにちは", "e1")])
        await gw.drain()
        await asyncio.sleep(0.2)
        await post(app, [ev("U1", "こんにちは", "e2")])
        await gw.drain()
        await gw.aclose()
        return f.opened, len(s.sent)
    opened, n = run(main())
    assert len(opened) == 2 and opened[0] == opened[1] and n == 2


def test_session_is_recreated_after_date_change():
    clock = {"now": NOW}

    async def main():
        f, s = FakeSessions(), RecordingSender()
        gw = Gateway(f, s, now_fn=lambda: clock["now"])
        app = create_app(SECRET, gw)
        await post(app, [ev("U1", "こんにちは", "e1")])
        await gw.drain()
        clock["now"] = NOW + timedelta(days=1)
        await post(app, [ev("U1", "こんにちは", "e2")])
        await gw.drain()
        await gw.aclose()
        return f.opened, len(s.sent)
    opened, n = run(main())
    assert len(opened) == 2 and n == 2


def test_turn_limit_ends_session_politely():
    async def main():
        _, s, gw, app = env(max_turns=2)
        await post(app, [ev("U1", "こんにちは", f"e{i}", f"t{i}") for i in range(3)])
        await gw.drain()
        await gw.aclose()
        return s.texts()
    assert run(main())[2] == line.TURN_LIMIT


def test_agent_failure_gets_apology_and_next_message_starts_fresh():
    class Boom(FakeSessions):
        @contextlib.asynccontextmanager
        async def open(self, user_key, now):
            self.opened.append(user_key)
            class A:
                async def reply(self_, text):
                    if len(self.opened) == 1:
                        raise RuntimeError("boom")
                    return "ok"
            yield A()

    async def main():
        f, s = Boom(), RecordingSender()
        gw = Gateway(f, s, now_fn=lambda: NOW)
        app = create_app(SECRET, gw)
        await post(app, [ev("U1", "a", "e1")])
        await gw.drain()
        await post(app, [ev("U1", "b", "e2")])
        await gw.drain()
        await gw.aclose()
        return s.texts(), len(f.opened)
    texts, opened = run(main())
    assert texts == [line.APOLOGY, "ok"] and opened == 2


def test_split_text_respects_line_limits():
    parts = line.split_text("あ" * 12000)
    assert len(parts) == 3 and all(len(p) <= 5000 for p in parts)
    huge = line.split_text("あ" * 40000)
    assert len(huge) == 5 and huge[-1].endswith("…")

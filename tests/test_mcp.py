import asyncio
import json

import pytest
from mcp import Client

from dental.core.db import connect
from dental.mcp_server.server import build_server
from helpers import NOW

EXPECTED = {"list_services", "search_slots", "verify_identity", "register_new_patient", "get_my_appointments",
            "book_appointment", "cancel_appointment", "reschedule_appointment", "handoff_to_staff"}


def session(core, script):
    """script(call) is an async function; call(name, **args) → dict. One MCP session."""
    async def main():
        srv = build_server(core, now_fn=lambda: NOW)
        async with Client(srv) as c:
            async def call(tool, /, **args):
                r = await c.call_tool(tool, args)
                return json.loads(r.content[0].text)
            return await script(call, c)
    return asyncio.run(main())


def test_contract_no_patient_id_in_schemas(core):
    async def script(call, c):
        return (await c.list_tools()).tools
    tools = session(core, script)
    assert {t.name for t in tools} == EXPECTED
    for t in tools:
        props = set(t.input_schema.get("properties", {}))
        assert not {"patient_id", "patient", "card", "idempotency_key", "actor", "source"} & props, t.name
        assert t.description


def test_full_flow(core, db_url):
    async def script(call, c):
        assert (await call("get_my_appointments"))["error"] == "patient_not_verified"
        v = await call("verify_identity", card_number=100001, birth_date="1988-04-12")
        assert v["ok"] and v["card_number"] == 100001
        s = await call("search_slots", service="cleaning", date_from="2026-10-14", date_to="2026-10-14")
        assert s["ok"] and "16:00" in s["days"][0]["times"]
        b = await call("book_appointment", service="cleaning", start="2026-10-14T16:00")
        assert b["ok"] and b["appointment"]["start"].startswith("2026-10-14T16:00")
        aid = b["appointment"]["appointment_id"]
        b2 = await call("book_appointment", service="cleaning", start="2026-10-14T16:00")      # the model repeats itself
        assert b2["ok"] and b2["already_booked_by_retry"] and b2["appointment"]["appointment_id"] == aid
        mine = await call("get_my_appointments")
        assert [a["appointment_id"] for a in mine["appointments"]] == [aid]
        r = await call("reschedule_appointment", appointment_id=aid, new_start="2026-10-14T17:00")
        assert r["ok"] and r["old_appointment_id"] == aid
        aid2 = r["appointment"]["appointment_id"]
        x = await call("cancel_appointment", appointment_id=aid2)
        assert x["ok"] and x["appointment"]["status"] == "cancelled"
        again = await call("book_appointment", service="cleaning", start="2026-10-14T17:00")  # the same slot after cancelling
        assert again["ok"] and not again["already_booked_by_retry"]
    session(core, script)


def test_errors_are_structured(core):
    async def script(call, c):
        await call("verify_identity", card_number=100001, birth_date="1988-04-12")
        t = await call("book_appointment", service="cleaning", start="2026-10-09T15:00")
        assert t["ok"] is False and t["error"] == "slot_taken" and t["alternatives"]
        assert (await call("book_appointment", service="cleaning", start="2026-10-08T10:00"))["error"] == "closed_day"
        assert (await call("book_appointment", service="cleaning", start="yesterday"))["error"] == "invalid_input"
        assert (await call("search_slots", service="cleaning", date_from="2026-10-01", date_to="2026-11-30"))["error"] == "invalid_input"
        assert (await call("search_slots", service="nope", date_from="2026-10-14", date_to="2026-10-14"))["ok"] is False
    session(core, script)


def test_cannot_touch_foreign_appointment(core, db_url):
    with connect(db_url) as c:
        foreign = c.execute("SELECT id FROM appointments WHERE patient_id=2 AND status='active'").fetchone()["id"]

    async def script(call, c):
        await call("verify_identity", card_number=100001, birth_date="1988-04-12")
        assert (await call("cancel_appointment", appointment_id=foreign))["error"] == "not_found"
        assert (await call("reschedule_appointment", appointment_id=foreign, new_start="2026-10-14T16:00"))["error"] == "not_found"
        h = await call("handoff_to_staff", reason="other", summary="test", appointment_id=foreign)
        assert h["ok"]
    session(core, script)
    with connect(db_url) as c:
        assert c.execute("SELECT status FROM appointments WHERE id=%s", [foreign]).fetchone()["status"] == "active"
        assert c.execute("SELECT appointment_id FROM handoffs").fetchone()["appointment_id"] is None


def test_late_cancel_hint_and_handoff(core, db_url):
    async def script(call, c):
        await call("verify_identity", card_number=100003, birth_date="1992-07-21")
        mine = (await call("get_my_appointments"))["appointments"]
        r = await call("cancel_appointment", appointment_id=mine[0]["appointment_id"])
        assert r["error"] == "too_late_to_cancel" and r["handoff_reason"] == "late_cancel"
        assert (await call("handoff_to_staff", reason="late_cancel", summary="cancellation <24h ahead"))["ok"]
        assert (await call("handoff_to_staff", reason="bogus", summary="x"))["error"] == "invalid_input"
    session(core, script)
    with connect(db_url) as c:
        h = c.execute("SELECT patient_id, reason FROM handoffs").fetchone()
        assert (h["patient_id"], h["reason"]) == (3, "late_cancel")


def test_register_flow_and_first_visit(core):
    async def script(call, c):
        r = await call("register_new_patient", name="新 患者", name_kana="あらた かんじゃ", phone="070-0000-0006",
                       birth_date="1995-05-17")
        assert r["ok"] and r["card_number"] == 100604
        assert (await call("book_appointment", service="cleaning", start="2026-10-14T16:00"))["error"] == "first_visit_required"
        b = await call("book_appointment", service="shoshin", start="2026-10-14T14:00")
        assert b["ok"] and "/intake/" in b["intake_url"]
        again = await call("book_appointment", service="shoshin", start="2026-10-14T14:00")
        assert again["already_booked_by_retry"] and "intake_url" not in again
        tok = b["intake_url"].rsplit("/", 1)[1]
        with connect(core.url) as c:
            row = c.execute("SELECT token_hash FROM intake_tokens").fetchall()
        assert len(row) == 1 and tok not in row[0]["token_hash"]
    session(core, script)


def test_sessions_are_isolated(core):
    async def script(call, c):
        await call("verify_identity", card_number=100001, birth_date="1988-04-12")
        return (await call("get_my_appointments"))["ok"]

    async def other(call, c):
        return (await call("get_my_appointments"))["error"]
    assert session(core, script) is True
    assert session(core, other) == "patient_not_verified"


def test_lockout_through_tools(core):
    async def script(call, c):
        codes = [(await call("verify_identity", card_number=100001, birth_date="2000-01-01"))["error"] for _ in range(7)]
        assert codes == ["patient_not_verified"] * 5 + ["locked"] * 2
        # correct data in the same session is locked too
        assert (await call("verify_identity", card_number=100001, birth_date="1988-04-12"))["error"] == "locked"
    session(core, script)


def test_no_foreign_data_in_responses(core):
    async def script(call, c):
        await call("verify_identity", card_number=100001, birth_date="1988-04-12")
        blob = json.dumps([await call("get_my_appointments"), await call("list_services"),
                           await call("search_slots", service="cleaning", date_from="2026-10-14", date_to="2026-10-16")],
                          ensure_ascii=False)
        for secret in ("100002", "1979-11-03", "070-0000"):
            assert secret not in blob
    session(core, script)

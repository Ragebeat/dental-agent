"""An MCP client stub for graph tests: a small in-memory clinic, no database and no LLM."""
import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from dental.agent.graph import GraphAgent
from dental.agent.llm import FakeLLM, LLMReply

TZ = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 10, 7, 10, 0, tzinfo=TZ)
SERVICES = [{"code": "chiryo", "label_ja": "治療", "minutes": 60}, {"code": "cleaning", "label_ja": "クリーニング", "minutes": 30},
            {"code": "kenshin", "label_ja": "定期検診", "minutes": 30}, {"code": "shoshin", "label_ja": "初診", "minutes": 60}]


class FakeClinic:
    def __init__(self, slots=None, appts=None):
        self.slots = slots if slots is not None else {                  # date -> start times (any service)
            "2026-10-09": ["16:00", "16:30", "17:00"], "2026-10-13": ["09:00", "09:30", "10:30"],
            "2026-10-14": ["09:00", "09:30", "11:00", "14:00", "15:30"], "2026-10-16": ["09:00", "10:00", "14:00", "15:00"],
            "2026-10-19": ["09:00", "09:30", "11:00"]}
        self.patients = {(100001, "1988-04-12"): "鈴木 健太", (100002, "1979-11-03"): "高橋 さくら"}
        self.appts = appts if appts is not None else []
        self.handoffs: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.registered = None
        self.fail_search = 0

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        out = getattr(self, f"t_{name}")(**args)
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(out, ensure_ascii=False))], is_error=False)

    def t_list_services(self):
        return {"ok": True, "services": SERVICES, "cancel_free_until_hours_before": 24}

    def t_search_slots(self, service, date_from, date_to, time_from=None, time_to=None):
        if self.fail_search:
            self.fail_search -= 1
            return {"ok": False, "error": "tool_error", "message": "boom"}
        days = []
        for d, ts in sorted(self.slots.items()):
            if date_from <= d <= date_to:
                ok = [t for t in ts if (not time_from or t >= time_from) and (not time_to or t <= time_to)]
                if ok:
                    days.append({"date": d, "weekday": "x", "times": ok})
        return {"ok": True, "service": service, "days": days, "no_slots_dates": []}

    def t_verify_identity(self, card_number, birth_date):
        name = self.patients.get((card_number, birth_date))
        if not name:
            return {"ok": False, "error": "patient_not_verified", "message": "номер карты и дата рождения не совпадают"}
        return {"ok": True, "verified": True, "name": name, "card_number": card_number}

    def t_register_new_patient(self, name, name_kana, phone, birth_date):
        self.registered = dict(name=name, name_kana=name_kana, phone=phone, birth_date=birth_date)
        return {"ok": True, "verified": True, "card_number": 100604, "name": name}

    def t_get_my_appointments(self):
        return {"ok": True, "appointments": list(self.appts)}

    def t_book_appointment(self, service, start, note=None):
        d, t = start.split("T")
        if t not in self.slots.get(d, []):
            return {"ok": False, "error": "slot_taken", "message": "занято"}
        self.slots[d].remove(t)
        a = {"appointment_id": 900 + len(self.appts), "service": service, "service_ja": service,
             "start": f"{start}+09:00", "end": f"{start}+09:00", "status": "active"}
        self.appts.append(a)
        out = {"ok": True, "appointment": a}
        if service == "shoshin":
            out["intake_url"] = "http://localhost:8000/intake/tok"
        return out

    def t_cancel_appointment(self, appointment_id):
        a = next(x for x in self.appts if x["appointment_id"] == appointment_id)
        a["status"] = "cancelled"
        return {"ok": True, "appointment": a}

    def t_reschedule_appointment(self, appointment_id, new_start):
        a = next(x for x in self.appts if x["appointment_id"] == appointment_id)
        a["start"] = f"{new_start}+09:00"
        return {"ok": True, "appointment": a, "old_appointment_id": appointment_id}

    def t_handoff_to_staff(self, reason, summary, appointment_id=None):
        self.handoffs.append({"reason": reason, "summary": summary})
        return {"ok": True, "handoff_id": len(self.handoffs)}


def appt(i, service, start):
    return {"appointment_id": i, "service": service, "service_ja": service, "start": f"{start}+09:00", "end": f"{start}+09:00", "status": "active"}


def llm_json(*items):
    """Answers of the LLM fallback path (JSON strings); if there are more calls than answers, empty JSON."""
    class L(FakeLLM):
        def chat(self, messages, tools=None):
            self.calls.append(messages)
            return LLMReply(text=json.dumps(self.script.pop(0)) if self.script else "{}")
    return L(list(items))


class Dialog:
    def __init__(self, clinic=None, llm=None, now=NOW):
        self.clinic = clinic or FakeClinic()
        self.agent = GraphAgent(llm or llm_json(), self.clinic, [], now)
        self.replies: list[str] = []

    def say(self, *texts):
        for t in texts:
            self.replies.append(asyncio.run(self.agent.reply(t)))
        return self.replies[-1]

    def called(self, name):
        return [a for n, a in self.clinic.calls if n == name]

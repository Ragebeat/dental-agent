"""Structured facts for the summary. Code and SQL only; no LLM here.

The summary is built on a working-day morning (around 08:30) and describes TODAY. The event window: from the close of the previous working day to `now`
(cancellations, new patients, new bookings; weekends and holidays fall into the window by themselves), plus everything unresolved (open handoffs of any
age) and today's plan. Names, phones and handoff texts never enter the view given to the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from dental.core.service import ClinicCore

WINDOW_H = 24            # fallback window if the previous working day is not found
STALE_H = 24            # an open handoff older than this is "stuck"
SERVICE_RU = {"chiryo": "лечение", "cleaning": "чистка", "kenshin": "профосмотр", "shoshin": "первичный приём",
              "shishu": "проверка дёсен"}


@dataclass
class Facts:
    now: datetime
    today: date
    since: datetime            # start of the event window: the close of the previous working day
    open_today: bool = True
    today_appts: list[dict] = field(default_factory=list)     # id, start(iso), service, label_ja, card, name
    cancellations: list[dict] = field(default_factory=list)      # id, start, service, label_ja, card
    new_patients: list[dict] = field(default_factory=list)       # card, name
    new_bookings: list[dict] = field(default_factory=list)       # id, start, service, label_ja, card, source
    free_slots: dict[str, int] = field(default_factory=dict)     # service code -> number of start times today (after now)
    handoffs: list[dict] = field(default_factory=list)           # id, reason, age_h, stale, card, appointment_id, summary
    labels: dict[str, str] = field(default_factory=dict)         # service code -> label_ja

    @property
    def today_total(self) -> int:
        return len(self.today_appts)

    def svc(self, code: str, lang: str) -> str:
        return self.labels.get(code, code) if lang == "ja" else SERVICE_RU.get(code, code)

    def llm_view(self, lang: str = "ja") -> dict:
        """Everything the model sees: no names, phones, birth dates or free handoff text.
        Services by name in the text's language (not by code: from codes the model transliterates and mixes them up)."""
        S = lambda c: self.svc(c, lang)      # noqa: E731
        return {
            "today": self.today.isoformat(), "events_since": self.since.isoformat(timespec="minutes"),
            "today_total": self.today_total,
            "today_by_service": {S(k): v for k, v in _count_by(self.today_appts, self.labels).items()},
            "today_appointments": [{"ref": f"A{a['id']}", "time": a["start"][11:16], "service": S(a["service"]),
                                       "card": a["card"]} for a in self.today_appts],
            "cancellations_since_last_close": [{"ref": f"C{c['id']}", "was": c["start"][:16], "service": S(c["service"]),
                                        "card": c["card"]} for c in self.cancellations],
            "new_patients_since_last_close": [{"ref": f"N{p['card']}", "card": p["card"]} for p in self.new_patients],
            "new_bookings_since_last_close": [{"ref": f"B{b['id']}", "start": b["start"][:16], "service": S(b["service"]),
                                       "card": b["card"], "source": b["source"]} for b in self.new_bookings],
            "free_start_times_today": {S(k): v for k, v in self.free_slots.items()},
            "open_handoffs": [{"ref": f"H{h['id']}", "reason": h["reason"], "age_hours": h["age_h"], "stale": h["stale"],
                               "card": h["card"]} for h in self.handoffs],
        }

    def must_refs(self) -> list[str]:
        """What must appear in the text (recall): every cancellation, new patient, new booking, open handoff."""
        return ([f"C{c['id']}" for c in self.cancellations] + [f"N{p['card']}" for p in self.new_patients]
                + [f"B{b['id']}" for b in self.new_bookings] + [f"H{h['id']}" for h in self.handoffs])

    def known_refs(self) -> set[str]:
        return set(self.must_refs()) | {f"A{a['id']}" for a in self.today_appts}


def _count_by(rows: list[dict], labels: dict[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r["service"]] = out.get(r["service"], 0) + 1
    return out


def _prev_close(cal, today: date) -> datetime | None:
    """The close of the last working day before `today` (accounts for the clinic's days off and holidays)."""
    for i in range(1, 15):
        sh = cal.shifts(today - timedelta(days=i))
        if sh:
            return sh[-1][1]
    return None


def collect(core: ClinicCore, now: datetime) -> Facts:
    tz = now.tzinfo
    today = now.date()
    cal = core.clinic.calendar
    since = _prev_close(cal, today) or (now - timedelta(hours=WINDOW_H))
    f = Facts(now=now, today=today, since=since, open_today=cal.is_open(today))
    with core._connect() as conn:
        labels = {r["code"]: r["label_ja"] for r in conn.execute("SELECT code, label_ja FROM services ORDER BY code")}
        f.labels = labels

        def row(r, with_status=False):
            return {"id": r["id"], "start": r["start"].astimezone(tz).isoformat(timespec="minutes"),
                    "service": r["service_code"], "label_ja": labels[r["service_code"]], "card": r["card_number"]}

        base = ("SELECT a.id, lower(a.during) AS start, a.service_code, a.source, a.created_at, p.card_number, p.name"
                " FROM appointments a JOIN patients p ON p.id = a.patient_id ")
        for r in conn.execute(base + "WHERE a.day = %s AND a.status = 'active' ORDER BY lower(a.during), a.id", [today]):
            d = row(r)
            d["name"] = r["name"]
            f.today_appts.append(d)
        for r in conn.execute(base.replace("a.created_at", "a.cancelled_at AS created_at")
                              + "WHERE a.status = 'cancelled' AND a.cancelled_at > %s AND a.cancelled_at <= %s"
                                " ORDER BY a.cancelled_at, a.id", [since, now]):
            f.cancellations.append(row(r))
        for r in conn.execute(base + "WHERE a.created_at > %s AND a.created_at <= %s AND a.status = 'active' AND a.rescheduled_from_id IS NULL"
                              " AND a.source <> 'admin' ORDER BY a.created_at, a.id", [since, now]):
            d = row(r)
            d["source"] = r["source"]
            f.new_bookings.append(d)
        for r in conn.execute("SELECT card_number, name FROM patients WHERE created_at > %s AND created_at <= %s"
                              " ORDER BY created_at, id", [since, now]):
            f.new_patients.append({"card": r["card_number"], "name": r["name"]})
        for r in conn.execute("SELECT h.id, h.reason, h.created_at, h.appointment_id, h.summary, p.card_number"
                              " FROM handoffs h LEFT JOIN patients p ON p.id = h.patient_id"
                              " WHERE h.status = 'open' ORDER BY h.created_at, h.id"):
            age = max(0, int((now - r["created_at"]).total_seconds() // 3600))
            f.handoffs.append({"id": r["id"], "reason": r["reason"], "age_h": age, "stale": age >= STALE_H,
                               "card": r["card_number"], "appointment_id": r["appointment_id"], "summary": r["summary"]})
    for code in labels:
        try:
            n = len(core.find_slots(code, today, today, now=now))
        except Exception:        # noqa: BLE001  service unavailable that day (e.g. new patients only) — not a summary fact
            n = 0
        if n:
            f.free_slots[code] = n
    return f

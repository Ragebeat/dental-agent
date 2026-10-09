"""Инструменты агента: тонкая обёртка над ClinicCore без зависимости от MCP SDK.

Принципы:
- LLM не передаёт patient_id: пациент фиксируется в сессии через verify_identity / register_new_patient;
- любой отказ ядра возвращается словарём {ok: False, error, message, ...}, а не исключением;
- ключ идемпотентности строит сервер, а не модель;
- в ответах нет данных других пациентов и нет свободного текста пациента (note не возвращается).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dental.core import errors as E
from dental.core.service import ClinicCore, Patient

MAX_RANGE_DAYS = 14
WEEKDAYS_JA = "月火水木金土日"


def _date(s: str, what: str) -> date:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise E.InvalidInput(f"{what}: формат YYYY-MM-DD") from None


def _time(s: str | None, what: str) -> time | None:
    if s in (None, ""):
        return None
    try:
        return time.fromisoformat(s)
    except ValueError:
        raise E.InvalidInput(f"{what}: формат HH:MM") from None


class DentalTools:
    """Одна сессия = один разговор = один (или ни одного) проверенный пациент."""

    def __init__(self, core: ClinicCore, *, now_fn: Callable[[], datetime], channel: str = "web",
                 session_id: str | None = None) -> None:
        self.core = core
        self.now_fn = now_fn
        self.channel = channel                      # web | line
        self.session_id = session_id or uuid.uuid4().hex
        self.patient: Patient | None = None
        self._cancel_epoch = 0                      # после отмены тот же слот можно снова забронировать
        self.tz: ZoneInfo = core.clinic.calendar.tz

    # ---------- служебное ----------
    def _call(self, fn: Callable[[], dict]) -> dict:
        try:
            return {"ok": True, **fn()}
        except E.DentalError as e:
            return {"ok": False, **e.to_dict()}

    def _need_patient(self) -> Patient:
        if self.patient is None:
            raise E.PatientNotVerified("сначала нужно подтвердить личность: номер карты и дата рождения")
        return self.patient

    def _start(self, s: str) -> datetime:
        try:
            dt = datetime.fromisoformat(s)
        except (TypeError, ValueError):
            raise E.InvalidInput("время начала: формат YYYY-MM-DDTHH:MM (по Токио)") from None
        return dt.replace(tzinfo=self.tz) if dt.tzinfo is None else dt.astimezone(self.tz)

    def _key(self, *parts) -> str:
        raw = "|".join(str(p) for p in (self.session_id, *parts))
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _appt_view(self, a) -> dict:
        label = self.core.clinic.services[a.service_code].label_ja
        return {"appointment_id": a.id, "service": a.service_code, "service_ja": label,
                "start": a.start.isoformat(timespec="minutes"), "end": a.end.isoformat(timespec="minutes"),
                "status": a.status}

    # ---------- без проверки личности ----------
    def list_services(self) -> dict:
        def run():
            cl = self.core.clinic
            minutes = int(cl.calendar.slot.total_seconds() // 60)
            out = []
            for s in cl.services.values():
                if not s.is_active:
                    continue
                item = {"code": s.code, "label_ja": s.label_ja, "minutes": s.duration_slots * minutes,
                        "bookable_days_ahead": s.horizon_days, "min_lead_hours": s.min_lead_hours,
                        "new_patients_only": s.new_patient_only}
                if s.allowed_windows:
                    item["allowed_hours"] = [f"{a:%H:%M}-{b:%H:%M}" for a, b in s.allowed_windows]
                out.append(item)
            return {"services": out, "cancel_free_until_hours_before": cl.rules["cancel_before_hours"],
                    "max_active_appointments": cl.rules["max_active_per_patient"]}
        return self._call(run)

    def search_slots(self, service: str, date_from: str, date_to: str, time_from: str | None = None,
                     time_to: str | None = None) -> dict:
        def run():
            d1, d2 = _date(date_from, "date_from"), _date(date_to, "date_to")
            if d2 < d1:
                raise E.InvalidInput("date_to раньше date_from")
            if (d2 - d1).days + 1 > MAX_RANGE_DAYS:
                raise E.InvalidInput(f"диапазон не больше {MAX_RANGE_DAYS} дней; сузьте период или запросите по частям")
            slots = self.core.find_slots(service, d1, d2, now=self.now_fn(), time_from=_time(time_from, "time_from"),
                                         time_to=_time(time_to, "time_to"))
            by_day: dict[date, list[str]] = {}
            for s in slots:
                s = s.astimezone(self.tz)
                by_day.setdefault(s.date(), []).append(f"{s:%H:%M}")
            days = [{"date": d.isoformat(), "weekday": WEEKDAYS_JA[d.weekday()], "times": t}
                    for d, t in sorted(by_day.items())]
            empty = [(d1 + timedelta(days=i)).isoformat() for i in range((d2 - d1).days + 1)
                     if (d1 + timedelta(days=i)) not in by_day]
            return {"service": service, "days": days, "no_slots_dates": empty,
                    "note": "times — время начала по Токио; это полный список свободных стартов за период"}
        return self._call(run)

    def verify_identity(self, card_number: int, birth_date: str) -> dict:
        def run():
            if not isinstance(card_number, int) or isinstance(card_number, bool):
                raise E.InvalidInput("card_number должен быть числом")
            p = self.core.verify_patient(card_number, _date(birth_date, "birth_date"), channel=self.channel,
                                         client_key=self.session_id, now=self.now_fn())
            self.patient = p
            return {"verified": True, "name": p.name, "card_number": p.card_number}
        return self._call(run)

    def register_new_patient(self, name: str, name_kana: str, phone: str, birth_date: str) -> dict:
        def run():
            p = self.core.register_patient(name=name, name_kana=name_kana, phone=phone,
                                           birth_date=_date(birth_date, "birth_date"), now=self.now_fn())
            self.patient = p
            return {"verified": True, "card_number": p.card_number, "name": p.name,
                    "next": "новому пациенту доступна запись только на 初診 (shoshin)"}
        return self._call(run)

    def handoff_to_staff(self, reason: str, summary: str, appointment_id: int | None = None) -> dict:
        def run():
            hid = self.core.create_handoff(self.patient.id if self.patient else None, reason, summary,
                                           now=self.now_fn(), appointment_id=appointment_id)
            return {"handoff_id": hid, "message": "передано сотруднику клиники; он свяжется с пациентом"}
        return self._call(run)

    # ---------- после проверки личности ----------
    def get_my_appointments(self) -> dict:
        def run():
            p = self._need_patient()
            return {"appointments": [self._appt_view(a) for a in self.core.patient_appointments(p.id, now=self.now_fn())]}
        return self._call(run)

    def book_appointment(self, service: str, start: str, note: str | None = None) -> dict:
        def run():
            p = self._need_patient()
            st = self._start(start)
            a = self.core.book(p.id, service, st, source=self.channel, actor="clinic_agent", now=self.now_fn(),
                               note=note, idempotency_key=self._key("book", service, st.isoformat(), self._cancel_epoch))
            out = {"appointment": self._appt_view(a), "already_booked_by_retry": a.replayed}
            if self.core.clinic.services[service].new_patient_only and not a.replayed:
                token = self.core.issue_intake_token(p.id, now=self.now_fn())
                base = os.environ.get("INTAKE_BASE_URL", "http://localhost:8000/intake")
                out["intake_url"] = f"{base}/{token}"
                out["intake_note"] = ("анкета (問診票) необязательна: её можно заполнить по ссылке сейчас, частично и "
                                      "вернуться позже, либо заполнить в клинике; содержимое анкеты не обсуждается в чате")
            return out
        return self._call(run)

    def cancel_appointment(self, appointment_id: int) -> dict:
        def run():
            p = self._need_patient()
            if not isinstance(appointment_id, int) or isinstance(appointment_id, bool):
                raise E.InvalidInput("appointment_id должен быть числом")
            a = self.core.cancel(p.id, appointment_id, actor="clinic_agent", now=self.now_fn())
            self._cancel_epoch += 1
            return {"appointment": self._appt_view(a)}
        return self._call(run)

    def reschedule_appointment(self, appointment_id: int, new_start: str) -> dict:
        def run():
            p = self._need_patient()
            if not isinstance(appointment_id, int) or isinstance(appointment_id, bool):
                raise E.InvalidInput("appointment_id должен быть числом")
            st = self._start(new_start)
            a = self.core.reschedule(p.id, appointment_id, st, actor="clinic_agent", now=self.now_fn(),
                                     idempotency_key=self._key("resched", appointment_id, st.isoformat()),
                                     source=self.channel)
            self._cancel_epoch += 1
            return {"appointment": self._appt_view(a), "old_appointment_id": appointment_id,
                    "already_done_by_retry": a.replayed}
        return self._call(run)

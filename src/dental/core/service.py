"""Ядро записи: проверка личности, регистрация, поиск слотов, запись, отмена, перенос.

Без LLM. «Сейчас» всегда приходит параметром `now` (aware datetime), БД и код не вызывают now().
Гарантии, которые нельзя обойти: исключающие ограничения и уникальные индексы в БД (migrations/001_init.sql);
остальные правила проверяются здесь внутри транзакции с блокировкой строки пациента.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

import psycopg
from psycopg import errors as pgerr
from psycopg.types.range import Range

from dental.core import errors as E
from dental.core.db import connect, database_url
from dental.core.runtime import ClinicRuntime, ServiceInfo, load_runtime

NOTE_MAX = 500
PHONE_RE = re.compile(r"^[0-9+\-() ]{8,20}$")
KANA_RE = re.compile(r"^[ぁ-ゟ゠-ヿー ]+$")


@dataclass(frozen=True)
class Patient:
    id: int
    card_number: int
    name: str
    name_kana: str
    phone: str
    birth_date: date


@dataclass(frozen=True)
class Appointment:
    id: int
    patient_id: int
    service_code: str
    chair_id: int
    start: datetime
    end: datetime
    status: str
    source: str
    actor: str
    note: str | None
    created_at: datetime
    rescheduled_from_id: int | None
    replayed: bool = False


def _aware(dt: datetime, what: str = "время") -> datetime:
    if dt.tzinfo is None:
        raise E.InvalidInput(f"{what}: нужен часовой пояс")
    return dt


def _norm(s: str, what: str, maxlen: int = 100) -> str:
    s = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip()
    if not s or len(s) > maxlen:
        raise E.InvalidInput(f"{what}: пусто или длиннее {maxlen} символов")
    return s


class ClinicCore:
    def __init__(self, url: str | None = None) -> None:
        self.url = url or database_url()
        self._clinic: ClinicRuntime | None = None

    # ---------- настройки ----------
    @property
    def clinic(self) -> ClinicRuntime:
        if self._clinic is None:
            self.refresh()
        return self._clinic  # type: ignore[return-value]

    def refresh(self) -> None:
        with connect(self.url) as conn:
            self._clinic = load_runtime(conn)

    def _connect(self) -> psycopg.Connection:
        return connect(self.url)

    def _service(self, code: str) -> ServiceInfo:
        svc = self.clinic.services.get(code)
        if svc is None or not svc.is_active:
            raise E.ServiceUnavailable(f"услуга '{code}' недоступна", service=code)
        return svc

    def _appt(self, row: dict, replayed: bool = False) -> Appointment:
        tz = self.clinic.calendar.tz
        d = row["during"]
        return Appointment(
            id=row["id"], patient_id=row["patient_id"], service_code=row["service_code"], chair_id=row["chair_id"],
            start=d.lower.astimezone(tz), end=d.upper.astimezone(tz), status=row["status"], source=row["source"],
            actor=row["actor"], note=row["note"], created_at=row["created_at"].astimezone(tz),
            rescheduled_from_id=row["rescheduled_from_id"], replayed=replayed)

    # ---------- проверка личности и регистрация ----------
    def verify_patient(self, card_number: int, birth_date: date, *, channel: str, client_key: str,
                       now: datetime) -> Patient:
        """Карта + дата рождения. Блокировка после N неудач подряд по карте или по клиенту."""
        _aware(now)
        rules = self.clinic.rules
        result: Patient | None = None
        with self._connect() as conn:
            with conn.transaction():
                for key in sorted({f"auth:card:{card_number}", f"auth:client:{client_key}"}):
                    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])
                for col, val in (("card_number", card_number), ("client_key", client_key)):
                    self._check_lock(conn, col, val, now, rules)
                row = conn.execute("SELECT * FROM patients WHERE card_number = %s", [card_number]).fetchone()
                ok = row is not None and row["birth_date"] == birth_date
                conn.execute("INSERT INTO auth_attempts (channel, client_key, card_number, success, at)"
                             " VALUES (%s,%s,%s,%s,%s)", [channel, client_key, card_number, ok, now])
                if ok:
                    result = Patient(row["id"], row["card_number"], row["name"], row["name_kana"], row["phone"],
                                     row["birth_date"])
        if result is None:   # одинаково для неверной даты и несуществующей карты: без «оракула»
            raise E.PatientNotVerified("номер карты и дата рождения не совпадают")
        return result

    @staticmethod
    def _check_lock(conn, col: str, val, now: datetime, rules: dict) -> None:
        n = rules["max_failed_attempts"]
        rows = conn.execute(f"SELECT success, at FROM auth_attempts WHERE {col} = %s ORDER BY at DESC, id DESC LIMIT %s",
                            [val, n]).fetchall()
        if len(rows) == n and not any(r["success"] for r in rows):
            until = rows[0]["at"] + timedelta(minutes=rules["lockout_minutes"])
            if now < until:
                raise E.Locked("слишком много неудачных попыток, попробуйте позже", retry_after=until)

    def register_patient(self, *, name: str, name_kana: str, phone: str, birth_date: date, now: datetime) -> Patient:
        _aware(now)
        name, kana = _norm(name, "имя"), _norm(name_kana, "ふりがな")
        phone = _norm(phone, "телефон", 20)
        if not KANA_RE.match(kana):
            raise E.InvalidInput("ふりがな нужно писать кана (ひらがな)")
        if not PHONE_RE.match(phone):
            raise E.InvalidInput("телефон: допустимы цифры, +, -, скобки")
        if not (date(1900, 1, 1) <= birth_date <= now.date()):
            raise E.InvalidInput("дата рождения вне допустимого диапазона")
        with self._connect() as conn, conn.transaction():
            key = re.sub(r"\s+", "", name)
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"reg:{key}:{birth_date}"])
            # Совпадение имени (без пробелов) и даты рождения = вероятно тот же человек, даже если телефон другой:
            # второй карты не выдаём, дальше разбирается сотрудник.
            if conn.execute("SELECT 1 FROM patients WHERE regexp_replace(name, %s, '', 'g') = %s AND birth_date = %s",
                            [r"\s", key, birth_date]).fetchone():
                raise E.AlreadyRegistered("пациент с таким именем и датой рождения уже зарегистрирован; "
                                          "карту не выдаём, обратитесь к сотруднику", handoff_reason="other")
            row = conn.execute(
                "INSERT INTO patients (name, name_kana, phone, birth_date, is_synthetic, created_at)"
                " VALUES (%s,%s,%s,%s,true,%s) RETURNING *", [name, kana, phone, birth_date, now]).fetchone()
            conn.execute("INSERT INTO patient_intake (patient_id, status, answers, updated_at)"
                         " VALUES (%s,'not_started','{}',%s)", [row["id"], now])
        return Patient(row["id"], row["card_number"], row["name"], row["name_kana"], row["phone"], row["birth_date"])

    # ---------- поиск слотов ----------
    def find_slots(self, service_code: str, date_from: date, date_to: date, *, now: datetime,
                   time_from: time | None = None, time_to: time | None = None,
                   limit: int | None = None) -> list[datetime]:
        """Времена начала, на которые свободно хотя бы одно кресло на всю длительность услуги."""
        _aware(now)
        svc = self._service(service_code)
        with self._connect() as conn:
            out = self._find_slots(conn, svc, date_from, date_to, now, time_from, time_to)
        return out[:limit] if limit else out

    def _find_slots(self, conn, svc: ServiceInfo, date_from: date, date_to: date, now: datetime,
                    time_from: time | None, time_to: time | None) -> list[datetime]:
        cl = self.clinic
        cal, n = cl.calendar, svc.duration_slots
        today = now.astimezone(cal.tz).date()
        first = max(date_from, today)
        last = min(date_to, today + timedelta(days=svc.horizon_days))
        if first > last:
            return []
        earliest = now + timedelta(hours=svc.min_lead_hours)
        cands: list[datetime] = []
        for d in cal.open_dates(first, last):
            for s in cal.valid_starts(d, n):
                if s < earliest:
                    continue
                t = s.time()
                if (time_from and t < time_from) or (time_to and t > time_to):
                    continue
                if svc.allowed_windows and not any(
                        a <= t and s + n * cal.slot <= datetime.combine(d, b, tzinfo=cal.tz)
                        for a, b in svc.allowed_windows):
                    continue
                cands.append(s)
        if not cands:
            return []
        lo, hi = cands[0], cands[-1] + n * cal.slot
        busy: dict[int, set[datetime]] = defaultdict(set)
        for r in conn.execute("SELECT chair_id, lower(during) AS s, upper(during) AS e FROM appointments"
                              " WHERE status = 'active' AND during && tstzrange(%s, %s, '[)')", [lo, hi]).fetchall():
            c = r["s"].astimezone(cal.tz)
            while c < r["e"]:
                busy[r["chair_id"]].add(c)
                c += cal.slot
        return [s for s in cands
                if any(all((s + i * cal.slot) not in busy[ch] for i in range(n)) for ch in cl.chairs)]

    def _alternatives(self, conn, svc: ServiceInfo, requested: datetime, now: datetime, count: int = 3) -> list[datetime]:
        tz = self.clinic.calendar.tz
        d0 = requested.astimezone(tz).date()
        for span in (7, 21, svc.horizon_days):
            slots = [s for s in self._find_slots(conn, svc, d0 - timedelta(days=2), d0 + timedelta(days=span), now, None, None)
                     if s != requested]
            if slots:
                near = sorted(slots, key=lambda s: abs((s - requested).total_seconds()))[:count]
                return sorted(near)
        return []

    # ---------- запись ----------
    def book(self, patient_id: int, service_code: str, start: datetime, *, source: str, actor: str, now: datetime,
             note: str | None = None, idempotency_key: str | None = None, chair_id: int | None = None) -> Appointment:
        _aware(now); _aware(start)
        svc = self._service(service_code)
        with self._connect() as conn, conn.transaction():
            self._lock_patient(conn, patient_id)
            return self._book_locked(conn, patient_id, svc, start, source=source, actor=actor, now=now, note=note,
                                     key=idempotency_key, chair_id=chair_id)

    @staticmethod
    def _lock_patient(conn, patient_id: int) -> None:
        if not conn.execute("SELECT id FROM patients WHERE id = %s FOR UPDATE", [patient_id]).fetchone():
            raise E.NotFound("пациент не найден")

    def _validate_slot(self, svc: ServiceInfo, start: datetime, now: datetime) -> None:
        cal = self.clinic.calendar
        start = start.astimezone(cal.tz)
        d = start.date()
        if not cal.is_open(d):
            raise E.ClosedDay("в этот день клиника закрыта", date=d)
        if start not in set(cal.valid_starts(d, svc.duration_slots)):
            raise E.OutsideHours("это время вне часов приёма (запись на несколько слотов не может пересекать перерыв)",
                                 start=start, duration_slots=svc.duration_slots)
        if start < now + timedelta(hours=svc.min_lead_hours):
            raise E.TooSoon(f"записаться можно не ближе чем за {svc.min_lead_hours} ч", start=start)
        if (d - now.astimezone(cal.tz).date()).days > svc.horizon_days:
            raise E.BeyondHorizon(f"запись открыта на {svc.horizon_days} дней вперёд", start=start)
        if svc.allowed_windows and not any(
                a <= start.time() and start + svc.duration_slots * cal.slot <= datetime.combine(d, b, tzinfo=cal.tz)
                for a, b in svc.allowed_windows):
            raise E.OutsideAllowedWindow("эта услуга доступна в другие часы", start=start)

    def _book_locked(self, conn, patient_id: int, svc: ServiceInfo, start: datetime, *, source: str, actor: str,
                     now: datetime, note: str | None, key: str | None, chair_id: int | None,
                     rescheduled_from_id: int | None = None, skip_visit_rules: bool = False) -> Appointment:
        cl = self.clinic
        cal, n = cl.calendar, svc.duration_slots
        start = start.astimezone(cal.tz)
        if key:
            row = conn.execute("SELECT * FROM appointments WHERE patient_id = %s AND idempotency_key = %s",
                               [patient_id, key]).fetchone()
            if row:
                appt = self._appt(row, replayed=True)
                if appt.service_code != svc.code or appt.start != start:
                    raise E.IdempotencyConflict("этот ключ уже использован для другой записи")
                return appt
        if note is not None and len(note) > NOTE_MAX:
            raise E.InvalidInput(f"комментарий длиннее {NOTE_MAX} символов")
        self._validate_slot(svc, start, now)
        if not skip_visit_rules:
            has_completed = conn.execute("SELECT 1 FROM appointments WHERE patient_id = %s AND status = 'completed'",
                                         [patient_id]).fetchone()
            any_appt = conn.execute("SELECT 1 FROM appointments WHERE patient_id = %s LIMIT 1", [patient_id]).fetchone()
            if svc.new_patient_only and has_completed:
                raise E.NewPatientOnly("初診 только для пациентов, которые у нас ещё не лечились")
            if not svc.new_patient_only and not any_appt:
                raise E.FirstVisitRequired("новому пациенту сначала нужна запись на 初診")
        active = conn.execute("SELECT count(*) AS n FROM appointments WHERE patient_id = %s AND status = 'active'",
                              [patient_id]).fetchone()["n"]
        if active >= cl.rules["max_active_per_patient"]:
            raise E.LimitReached(f"не больше {cl.rules['max_active_per_patient']} активных записей",
                                 limit=cl.rules["max_active_per_patient"])
        end = start + n * cal.slot
        if conn.execute("SELECT 1 FROM appointments WHERE patient_id = %s AND status = 'active' AND service_code = %s"
                        " AND day = %s", [patient_id, svc.code, start.date()]).fetchone():
            raise E.DuplicateBooking("на этот день у вас уже есть запись на эту услугу")
        if conn.execute("SELECT 1 FROM appointments WHERE patient_id = %s AND status = 'active'"
                        " AND during && tstzrange(%s, %s, '[)')", [patient_id, start, end]).fetchone():
            raise E.PatientBusy("в это время у вас уже есть другая запись")
        # Одновременные вставки с exclusion-констрейнтом видят чужие незакоммиченные строки и могут взаимно
        # заблокироваться (DeadlockDetected). Записи на один день сериализуем; порядок блокировок всегда:
        # пациент → день.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"book:day:{start.date()}"])
        for ch in ([chair_id] if chair_id else cl.chairs):
            try:
                with conn.transaction():   # savepoint: неудача на одном кресле не рушит транзакцию
                    row = conn.execute(
                        "INSERT INTO appointments (patient_id, service_code, chair_id, during, day, status, source, note,"
                        " actor, idempotency_key, created_at, rescheduled_from_id)"
                        " VALUES (%s,%s,%s,%s,%s,'active',%s,%s,%s,%s,%s,%s) RETURNING *",
                        [patient_id, svc.code, ch, Range(start, end, "[)"), start.date(), source, note, actor, key, now,
                         rescheduled_from_id]).fetchone()
                return self._appt(row)
            except pgerr.ExclusionViolation as e:
                if e.diag.constraint_name == "appointments_patient_no_overlap":
                    raise E.PatientBusy("в это время у вас уже есть другая запись") from None
                continue   # кресло занято: пробуем следующее
            except pgerr.UniqueViolation as e:
                if e.diag.constraint_name == "appointments_idempotency":
                    row = conn.execute("SELECT * FROM appointments WHERE patient_id = %s AND idempotency_key = %s",
                                       [patient_id, key]).fetchone()
                    return self._appt(row, replayed=True)
                raise E.DuplicateBooking("на этот день у вас уже есть запись на эту услугу") from None
        raise E.SlotTaken("это время уже занято", alternatives=self._alternatives(conn, svc, start, now))

    # ---------- отмена и перенос ----------
    def _own_active(self, conn, patient_id: int, appointment_id: int, now: datetime) -> dict:
        row = conn.execute("SELECT * FROM appointments WHERE id = %s AND patient_id = %s FOR UPDATE",
                           [appointment_id, patient_id]).fetchone()
        if row is None:
            raise E.NotFound("запись не найдена")
        if row["status"] == "cancelled":
            raise E.AlreadyCancelled("эта запись уже отменена")
        if row["status"] != "active":
            raise E.NotFound("запись не найдена")
        svc = self.clinic.services[row["service_code"]]
        start = row["during"].lower
        if start - now < timedelta(hours=svc.cancel_before_hours):
            raise E.TooLateToCancel(
                f"отмена и перенос возможны не позднее чем за {svc.cancel_before_hours} ч; вызовите handoff_to_staff, сотрудник свяжется с пациентом",
                handoff_reason="late_cancel", start=start.astimezone(self.clinic.calendar.tz))
        return row

    def cancel(self, patient_id: int, appointment_id: int, *, actor: str, now: datetime,
               reason: str | None = None) -> Appointment:
        _aware(now)
        with self._connect() as conn, conn.transaction():
            self._lock_patient(conn, patient_id)
            self._own_active(conn, patient_id, appointment_id, now)
            row = conn.execute("UPDATE appointments SET status = 'cancelled', cancelled_at = %s, cancel_reason = %s"
                               " WHERE id = %s RETURNING *", [now, reason or "patient_request", appointment_id]).fetchone()
        return self._appt(row)

    def reschedule(self, patient_id: int, appointment_id: int, new_start: datetime, *, actor: str, now: datetime,
                   idempotency_key: str | None = None, source: str | None = None) -> Appointment:
        """Старая запись отменяется, новая создаётся в одной транзакции: при отказе старая остаётся."""
        _aware(now); _aware(new_start)
        with self._connect() as conn, conn.transaction():
            self._lock_patient(conn, patient_id)
            if idempotency_key:
                row = conn.execute("SELECT * FROM appointments WHERE patient_id = %s AND idempotency_key = %s",
                                   [patient_id, idempotency_key]).fetchone()
                if row:
                    return self._appt(row, replayed=True)
            old = self._own_active(conn, patient_id, appointment_id, now)
            svc = self._service(old["service_code"])
            if old["during"].lower == new_start:
                raise E.InvalidInput("новое время совпадает с текущим")
            conn.execute("UPDATE appointments SET status = 'cancelled', cancelled_at = %s, cancel_reason = 'rescheduled'"
                         " WHERE id = %s", [now, appointment_id])
            return self._book_locked(conn, patient_id, svc, new_start, source=source or old["source"], actor=actor,
                                     now=now, note=old["note"], key=idempotency_key, chair_id=None,
                                     rescheduled_from_id=appointment_id, skip_visit_rules=True)

    def patient_appointments(self, patient_id: int, *, now: datetime) -> list[Appointment]:
        _aware(now)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM appointments WHERE patient_id = %s AND status = 'active'"
                                " AND lower(during) >= %s ORDER BY lower(during)", [patient_id, now]).fetchall()
        return [self._appt(r) for r in rows]

    HANDOFF_REASONS = ("medical", "ambiguity", "late_cancel", "manipulation", "complaint", "tool_errors", "other")

    def create_handoff(self, patient_id: int | None, reason: str, summary: str, *, now: datetime,
                       appointment_id: int | None = None) -> int:
        """Передача человеку: структурированная сводка, не сырой диалог."""
        _aware(now)
        if reason not in self.HANDOFF_REASONS:
            raise E.InvalidInput(f"reason должен быть одним из: {', '.join(self.HANDOFF_REASONS)}")
        summary = _norm(summary, "сводка", 1000)
        with self._connect() as conn, conn.transaction():
            if appointment_id is not None and not conn.execute(
                    "SELECT 1 FROM appointments WHERE id = %s AND patient_id IS NOT DISTINCT FROM %s",
                    [appointment_id, patient_id]).fetchone():
                appointment_id = None     # чужой/несуществующий id в передачу не попадает
            return conn.execute(
                "INSERT INTO handoffs (patient_id, appointment_id, reason, summary, created_at)"
                " VALUES (%s,%s,%s,%s,%s) RETURNING id", [patient_id, appointment_id, reason, summary, now]
            ).fetchone()["id"]

    INTAKE_DAYS = 14

    def issue_intake_token(self, patient_id: int, *, now: datetime) -> str:
        """Токен ссылки на анкету. В БД только хеш; сам токен возвращается один раз и уходит пациенту ссылкой."""
        _aware(now)
        token = secrets.token_urlsafe(24)
        with self._connect() as conn, conn.transaction():
            self._lock_patient(conn, patient_id)
            conn.execute("INSERT INTO intake_tokens (token_hash, patient_id, created_at, expires_at)"
                         " VALUES (%s,%s,%s,%s)", [hashlib.sha256(token.encode()).hexdigest(), patient_id, now,
                                                   now + timedelta(days=self.INTAKE_DAYS)])
        return token

"""Clinic settings read from the database (the admin edits them there; clinic.toml is only the initial seed)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from dental.calendar import ClinicCalendar


@dataclass(frozen=True)
class ServiceInfo:
    code: str
    label_ja: str
    duration_slots: int
    horizon_days: int
    min_lead_hours: int
    cancel_before_hours: int
    allowed_windows: tuple[tuple[time, time], ...] | None
    recall_months: int | None
    new_patient_only: bool
    is_active: bool


@dataclass(frozen=True)
class ClinicRuntime:
    calendar: ClinicCalendar
    rules: dict
    services: dict[str, ServiceInfo]
    chairs: list[int]


def load_runtime(conn) -> ClinicRuntime:
    sched = conn.execute("SELECT value FROM clinic_settings WHERE key = 'schedule'").fetchone()["value"]
    rules = conn.execute("SELECT value FROM clinic_settings WHERE key = 'rules'").fetchone()["value"]
    chairs = [r["id"] for r in conn.execute("SELECT id FROM chairs ORDER BY id").fetchall()]
    services: dict[str, ServiceInfo] = {}
    for r in conn.execute("SELECT * FROM services ORDER BY code").fetchall():
        aw = r["allowed_windows"]
        services[r["code"]] = ServiceInfo(
            code=r["code"], label_ja=r["label_ja"], duration_slots=r["duration_slots"],
            horizon_days=rules["horizon_days"] if r["horizon_days"] is None else r["horizon_days"],
            min_lead_hours=rules["min_lead_hours"] if r["min_lead_hours"] is None else r["min_lead_hours"],
            cancel_before_hours=rules["cancel_before_hours"] if r["cancel_before_hours"] is None else r["cancel_before_hours"],
            allowed_windows=None if aw is None else tuple((time.fromisoformat(a), time.fromisoformat(b)) for a, b in aw),
            recall_months=r["recall_months"], new_patient_only=r["new_patient_only"], is_active=r["is_active"],
        )
    return ClinicRuntime(calendar=ClinicCalendar(sched), rules=rules, services=services, chairs=chairs)

"""The clinic's working calendar: which days are open and at which times an appointment can start.

Pure functions, no database and no LLM. Built from clinic.toml (ClinicConfig) or from a settings
dict (as stored in the database: clinic_settings.schedule).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import jpholiday

from dental.config import WEEKDAYS, ClinicConfig


def settings_from_config(cfg: ClinicConfig) -> dict:
    return dict(
        timezone=cfg.clinic.timezone, chairs=cfg.clinic.chairs, slot_minutes=cfg.clinic.slot_minutes,
        closed_weekdays=list(cfg.clinic.closed_weekdays), closed_on_holidays=cfg.clinic.closed_on_holidays,
        hours=[dict(weekdays=list(h.weekdays), open=h.open.strftime("%H:%M"), close=h.close.strftime("%H:%M"))
               for h in cfg.hours],
    )


class ClinicCalendar:
    def __init__(self, source: ClinicConfig | dict) -> None:
        s = source if isinstance(source, dict) else settings_from_config(source)
        self.tz = ZoneInfo(s["timezone"])
        self.slot = timedelta(minutes=s["slot_minutes"])
        self.chairs = s["chairs"]
        self._closed_weekdays = set(s["closed_weekdays"])
        self._closed_on_holidays = s["closed_on_holidays"]
        shifts: dict[int, list] = {i: [] for i in range(7)}
        for sh in s["hours"]:
            for wd in sh["weekdays"]:
                shifts[WEEKDAYS.index(wd)].append((time.fromisoformat(sh["open"]), time.fromisoformat(sh["close"])))
        self._shifts = {i: sorted(v) for i, v in shifts.items()}

    def is_holiday(self, d: date) -> bool:
        return jpholiday.is_holiday(d)

    def is_open(self, d: date) -> bool:
        if WEEKDAYS[d.weekday()] in self._closed_weekdays:
            return False
        if self._closed_on_holidays and self.is_holiday(d):
            return False
        return bool(self._shifts[d.weekday()])

    def shifts(self, d: date) -> list[tuple[datetime, datetime]]:
        if not self.is_open(d):
            return []
        return [
            (datetime.combine(d, o, tzinfo=self.tz), datetime.combine(d, c, tzinfo=self.tz))
            for o, c in self._shifts[d.weekday()]
        ]

    def valid_starts(self, d: date, n_slots: int = 1) -> list[datetime]:
        """Appointment start times for n_slots slots: the whole appointment within one shift."""
        out: list[datetime] = []
        need = self.slot * n_slots
        for o, c in self.shifts(d):
            s = o
            while s + need <= c:
                out.append(s)
                s += self.slot
        return out

    def open_dates(self, first: date, last: date) -> list[date]:
        """Open days from first to last inclusive."""
        days = (last - first).days
        return [d for d in (first + timedelta(days=i) for i in range(days + 1)) if self.is_open(d)]

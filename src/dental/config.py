"""Loading and validating clinic.toml (Pydantic, extra=forbid: a typo in a key fails at once)."""

from __future__ import annotations

import hashlib
import json
import tomllib
from datetime import datetime, time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, model_validator

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "clinic.toml"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Clinic(_Strict):
    name: str
    timezone: str
    chairs: int
    slot_minutes: int
    closed_weekdays: list[Weekday]
    closed_on_holidays: bool


class Hours(_Strict):
    weekdays: list[Weekday]
    open: time
    close: time


class Rules(_Strict):
    horizon_days: int
    min_lead_hours: int
    cancel_before_hours: int
    max_active_per_patient: int
    max_failed_attempts: int
    lockout_minutes: int


class Service(_Strict):
    label_ja: str
    duration_slots: int = 1
    new_patient_only: bool = False
    recall_months: int | None = None
    # None = take the value from [rules]
    horizon_days: int | None = None
    min_lead_hours: int | None = None
    cancel_before_hours: int | None = None
    allowed_windows: list[tuple[time, time]] | None = None
    is_active: bool = True


class LoadProfile(_Strict):
    by_weeks_ahead: list[float]
    cap: float
    weekday_multiplier: dict[Weekday, float]


class Synthetic(_Strict):
    seed: int
    frozen_now: datetime
    schedule_weeks: int
    test_patients: int
    background_patients: int
    background_source: str
    far_recall_count: int
    service_mix: dict[str, float]
    load: LoadProfile


class ClinicConfig(_Strict):
    clinic: Clinic
    hours: list[Hours]
    rules: Rules
    services: dict[str, Service]
    synthetic: Synthetic

    @model_validator(mode="after")
    def _check(self) -> "ClinicConfig":
        slot = self.clinic.slot_minutes
        for sh in self.hours:
            o = sh.open.hour * 60 + sh.open.minute
            c = sh.close.hour * 60 + sh.close.minute
            if c <= o or o % slot or c % slot:
                raise ValueError(f"shift {sh.open}-{sh.close} does not fit the {slot}-minute grid")
        syn = self.synthetic
        if set(syn.service_mix) != set(self.services):
            raise ValueError("synthetic.service_mix and services list different services")
        if abs(sum(syn.service_mix.values()) - 1) > 1e-6:
            raise ValueError("the synthetic.service_mix shares do not add up to 1")
        if len(syn.load.by_weeks_ahead) != syn.schedule_weeks:
            raise ValueError("the length of load.by_weeks_ahead must equal schedule_weeks")
        return self

    def now(self) -> datetime:
        """The frozen "now" in the clinic's time zone."""
        return self.synthetic.frozen_now.astimezone(ZoneInfo(self.clinic.timezone))

    def horizon_days(self, code: str) -> int:
        v = self.services[code].horizon_days
        return self.rules.horizon_days if v is None else v


def load_config(path: Path = CONFIG_PATH) -> ClinicConfig:
    with open(path, "rb") as f:
        return ClinicConfig.model_validate(tomllib.load(f))


def config_sha(path: Path = CONFIG_PATH) -> str:
    """A short fingerprint of the configuration's content (for the run log, as in the RAG project).

    Hashes the parsed TOML, not the file bytes: comments and formatting do not change it (DECISIONS #42)."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:12]

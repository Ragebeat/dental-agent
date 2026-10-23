"""Loading and validating golden scenarios (YAML). Format: docs/SCENARIO_FORMAT.md.

Scenario texts (title, goal_ru, persona) stay as written: they are inputs of the logged runs and part of the fingerprint."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from dental.calendar import ClinicCalendar
from dental.config import ROOT, ClinicConfig
from dental.synth import fixtures as fx

SCENARIO_DIR = ROOT / "scenarios" / "golden"
HOLDOUT_DIR = ROOT / "scenarios" / "holdout"      # the held-out set: not used for tuning the agent (docs/DECISIONS.md #37)
STYLES = {"keigo", "casual", "rude", "typos", "halfwidth", "vague"}
REASONS = {"medical", "ambiguity", "late_cancel", "manipulation", "complaint", "tool_errors", "other"}
DB_KEYS = {
    "patient", "active_count", "must_have", "date_not", "cancelled", "still_active", "not_active_at",
    "total_new_appointments", "unchanged", "patient_created", "intake_link_issued",
}
REPLY_CHECKS = {"no_medical_advice", "no_leak"}
PLACEHOLDER = re.compile(r"\{(P_[A-Z]+)\.(name|name_kana|card|phone|birth_date)\}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Identity(_Strict):
    card: str          # own | none | other:P_X | after_booking (P_NEW only)
    birth_date: str    # own | none | other:P_X


class Expected(_Strict):
    handoff: Literal["required", "forbidden", "either"]
    handoff_reason: str | None = None
    db: list[dict[str, Any]]
    reply_checks: list[Any] = []


class Scenario(_Strict):
    id: str
    title: str
    categories: list[int]
    style: str | list[str]
    subject: str
    identity: Identity
    goal_ru: str
    persona_ja: str
    scripted_lines_ja: list[str] = []
    max_turns: int
    expected: Expected
    attack: bool


def load_scenarios(directory: Path = SCENARIO_DIR) -> list[Scenario]:
    out = []
    for f in sorted(directory.glob("*.yaml")):
        sc = Scenario.model_validate(yaml.safe_load(f.read_text(encoding="utf-8")))
        if sc.id != f.stem:
            raise ValueError(f"{f.name}: id '{sc.id}' does not match the file name")
        out.append(sc)
    return out


def render(text: str) -> str:
    """Substitute {P_X.field} with fixture values."""
    def sub(m):
        return str(fx.FIXTURE_PATIENTS[m.group(1)][m.group(2)])
    return PLACEHOLDER.sub(sub, text)


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _in_window(s: datetime, lo: str, hi: str) -> bool:
    return time.fromisoformat(lo) <= s.time() <= time.fromisoformat(hi)


def validate_scenario(sc: Scenario, cfg: ClinicConfig) -> list[str]:
    """Cross-checks a scenario against the config and fixtures: references, services, dates, windows, horizons."""
    cal = ClinicCalendar(cfg)
    today = cfg.now().date()
    p: list[str] = []
    keys = set(fx.FIXTURE_PATIENTS)
    if sc.subject not in keys:
        p.append(f"subject {sc.subject} is not a fixture")
    for style in ([sc.style] if isinstance(sc.style, str) else sc.style):
        if style not in STYLES:
            p.append(f"unknown style {style}")
    if sc.expected.handoff_reason and sc.expected.handoff_reason not in REASONS:
        p.append(f"unknown handoff reason {sc.expected.handoff_reason}")
    for field, allowed in (("card", {"own", "none", "after_booking"}), ("birth_date", {"own", "none"})):
        val = getattr(sc.identity, field)
        if val.startswith("other:"):
            if val[6:] not in keys:
                p.append(f"identity.{field} refers to {val}")
        elif val not in allowed:
            p.append(f"identity.{field}: {val}")
    for s in _strings(sc.model_dump()):
        for m in PLACEHOLDER.finditer(s):
            f = fx.FIXTURE_PATIENTS.get(m.group(1))
            if f is None or f[m.group(2)] is None:
                p.append(f"placeholder {m.group(0)} does not expand")
    fixture_active = {(a["patient"], a["start"]) for a in fx.FIXTURE_APPOINTMENTS if a["status"] == "active"}
    for chk in sc.expected.reply_checks:
        name = chk if isinstance(chk, str) else next(iter(chk))
        if name not in REPLY_CHECKS:
            p.append(f"unknown reply_check {name}")
    for item in sc.expected.db:
        for k in item:
            if k not in DB_KEYS:
                p.append(f"unknown db key: {k}")
        for k in ("patient",):
            if k in item and item[k] not in keys:
                p.append(f"db.{k}: {item[k]} is not a fixture")
        for must in item.get("must_have", []):
            code = must["service"]
            if code not in cfg.services:
                p.append(f"service {code} is not in the config")
                continue
            a, b = (date.fromisoformat(x) for x in must["date_between"])
            if (b - today).days > cfg.horizon_days(code):
                p.append(f"{code}: {b} is beyond the horizon of {cfg.horizon_days(code)} days")
            days = cal.open_dates(a, b)
            if not days:
                p.append(f"{code}: no open days in {a}..{b}")
                continue
            if "start_between" in must:
                lo, hi = must["start_between"]
                n = cfg.services[code].duration_slots
                if not any(_in_window(s, lo, hi) for d in days for s in cal.valid_starts(d, n)):
                    p.append(f"{code}: no valid starts for {n} slots in the {lo}-{hi} window")
                if hi > max(s.strftime("%H:%M") for d in days for s in cal.valid_starts(d, n)):
                    p.append(f"{code}: the upper bound {hi} is unreachable for {n} slots")
        for d in item.get("date_not", []):
            date.fromisoformat(d)
        for k in ("cancelled", "still_active", "not_active_at"):
            if k in item:
                pk, at = item[k]["patient"], item[k]["at"]
                if pk not in keys:
                    p.append(f"{k}: patient {pk} is not a fixture")
                if k in ("cancelled", "still_active") and (pk, at) not in fixture_active:
                    p.append(f"{k}: {pk} has no active fixture appointment at {at}")
        if "patient_created" in item and sc.subject != "P_NEW":
            p.append("patient_created is allowed only for P_NEW")
    return p

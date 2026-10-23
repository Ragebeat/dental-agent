"""Snapshot check: appointment invariants and fixture guarantees (docs/FIXTURES.md). No database, no LLM."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from dental.calendar import ClinicCalendar
from dental.config import ClinicConfig
from dental.synth import fixtures as fx


def _parse(snap: dict) -> list[dict]:
    out = []
    for a in snap["appointments"]:
        b = dict(a)
        b["s"], b["e"] = datetime.fromisoformat(a["start"]), datetime.fromisoformat(a["end"])
        out.append(b)
    return out


def _cells(a: dict, slot: timedelta):
    s = a["s"]
    while s < a["e"]:
        yield (a["chair_id"], s)
        s += slot


def verify_snapshot(snap: dict, cfg: ClinicConfig) -> list[str]:
    cal = ClinicCalendar(cfg)
    slot = cal.slot
    now = cfg.now()
    today = now.date()
    problems: list[str] = []
    A = _parse(snap)
    active = [a for a in A if a["status"] == "active"]

    # --- invariants ---
    for field, label in (("chair_id", "chair"), ("patient_id", "patient")):
        groups = defaultdict(list)
        for a in active:
            groups[a[field]].append(a)
        for k, lst in groups.items():
            lst.sort(key=lambda a: a["s"])
            for x, y in zip(lst, lst[1:]):
                if y["s"] < x["e"]:
                    problems.append(f"overlap ({label} {k}): appointments {x['id']} and {y['id']}")
    starts_cache: dict = {}
    per_patient: dict[int, int] = defaultdict(int)
    seen = set()
    for a in active:
        svc = cfg.services[a["service_code"]]
        d = a["s"].date()
        if a["e"] - a["s"] != svc.duration_slots * slot:
            problems.append(f"appointment {a['id']}: duration does not match the service")
        key = (d, svc.duration_slots)
        if key not in starts_cache:
            starts_cache[key] = set(cal.valid_starts(*key))
        if a["s"] not in starts_cache[key]:
            problems.append(f"appointment {a['id']}: outside hours / closed day / across the break ({a['start']})")
        if a["s"] < now:
            problems.append(f"appointment {a['id']}: in the past")
        if (d - today).days > cfg.horizon_days(a["service_code"]):
            problems.append(f"appointment {a['id']}: beyond the service horizon")
        per_patient[a["patient_id"]] += 1
        dup = (a["patient_id"], a["service_code"], d)
        if dup in seen:
            problems.append(f"duplicate appointment: patient {a['patient_id']}, {a['service_code']}, {d}")
        seen.add(dup)
    cap = cfg.rules.max_active_per_patient
    test_ids = {p["id"] for p in snap["patients"] if p["group"] == "test"}
    for pid, n in per_patient.items():
        if n > cap:
            problems.append(f"patient {pid}: {n} active appointments (limit {cap})")
        if pid in test_ids and n > 1:
            problems.append(f"test patient {pid}: {n} active appointments (0-1 allowed)")

    # --- fixtures: patients ---
    by_id = {p["id"]: p for p in snap["patients"]}
    cards = [p["card_number"] for p in snap["patients"]]
    if len(set(cards)) != len(cards):
        problems.append("card numbers are not unique")
    for key, f in fx.FIXTURE_PATIENTS.items():
        if f["id"] is None:
            if any(p["name"] == f["name"] or p["phone"] == f["phone"] for p in snap["patients"]):
                problems.append(f"{key} must not be in the database")
            continue
        p = by_id.get(f["id"])
        if not p or (p["name"], p["name_kana"], p["card_number"], p["phone"], p["birth_date"]) != (f["name"], f["name_kana"], f["card"], f["phone"], f["birth_date"]):
            problems.append(f"{key}: patient data does not match the fixture")
    for key, want in fx.FIXTURE_ACTIVE_COUNT.items():
        pid = fx.FIXTURE_PATIENTS[key]["id"]
        got = sum(1 for a in active if a["patient_id"] == pid)
        if got != want:
            problems.append(f"{key}: active appointments {got}, expected {want}")
    for fa in fx.FIXTURE_APPOINTMENTS:
        pid = fx.FIXTURE_PATIENTS[fa["patient"]]["id"]
        want_start = datetime.fromisoformat(fa["start"]).replace(tzinfo=cal.tz)
        if not any(a["patient_id"] == pid and a["s"] == want_start and a["status"] == fa["status"]
                   and a["service_code"] == fa["service"] for a in A):
            problems.append(f"no fixture appointment {fa['patient']} {fa['start']}")

    # --- fixtures: slots ---
    busy = {c for a in active for c in _cells(a, slot)}
    for d_iso, t0, t1, chair_list in fx.FORCED_BUSY:
        d = date.fromisoformat(d_iso)
        for s in cal.valid_starts(d, 1):
            if t0 <= s.strftime("%H:%M") < t1:
                for c in chair_list:
                    if (c, s) not in busy:
                        problems.append(f"F_BUSY/F_FULL: chair {c} is free at {s.isoformat()}")
    for d_iso, t0, t1 in fx.RESERVED_FREE:
        d = date.fromisoformat(d_iso)
        for s in cal.valid_starts(d, 1):
            if t0 <= s.strftime("%H:%M") < t1 and (fx.RESERVED_CHAIR, s) in busy:
                problems.append(f"free-window reserve violated: {s.isoformat()}")
    for d_iso in fx.NEXTWEEK_DAYS:
        d = date.fromisoformat(d_iso)
        n = sum(1 for s in cal.valid_starts(d, 1) if any((c, s) not in busy for c in snap["chairs"]))
        if n < fx.NEXTWEEK_MIN_STARTS:
            problems.append(f"F_NEXTWEEK {d_iso}: free starts {n} < {fx.NEXTWEEK_MIN_STARTS}")
    jan = set(fx.F_JAN_DAYS)
    for a in A:
        if a["day"] in jan:
            problems.append(f"F_JAN: appointment {a['id']} on {a['day']}")
    if not cal.is_open(date(2026, 10, 9)) or cal.is_open(date(2026, 10, 12)) or cal.is_open(date(2027, 1, 7)):
        problems.append("the calendar disagrees with FIXTURES.md (holidays/closed days)")

    # --- load shape: denser closer in ---
    st = load_stats(snap, cfg)
    occ = st["occupancy_by_week"]
    if sum(occ[:3]) / 3 < 0.45:
        problems.append(f"the first 3 weeks are too free: {occ[:3]}")
    if sum(occ[5:]) / len(occ[5:]) > 0.25:
        problems.append(f"the later weeks are too busy: {occ[5:]}")
    return problems


def load_stats(snap: dict, cfg: ClinicConfig) -> dict:
    cal = ClinicCalendar(cfg)
    now = cfg.now()
    today = now.date()
    earliest = now + timedelta(hours=cfg.rules.min_lead_hours)
    weeks = cfg.synthetic.schedule_weeks
    A = [a for a in _parse(snap) if a["status"] == "active"]
    busy = {c for a in A for c in _cells(a, cal.slot)}
    horizon_end = today + timedelta(days=weeks * 7)
    occ_week, tot_busy, tot_cap = [], 0, 0
    for w in range(weeks):
        cap = b = 0
        for d in cal.open_dates(today + timedelta(days=7 * w), today + timedelta(days=7 * w + 6)):
            for s in cal.valid_starts(d, 1):
                if s < earliest:
                    continue
                for c in snap["chairs"]:
                    cap += 1
                    b += (c, s) in busy
        occ_week.append(b / cap if cap else 0.0)
        tot_busy += b
        tot_cap += cap
    far = sum(1 for a in A if a["s"].date() >= horizon_end)
    return dict(occupancy_by_week=occ_week, occupancy_all=tot_busy / tot_cap if tot_cap else 0.0,
                active=len(A), far=far, appointments_total=len(snap["appointments"]))

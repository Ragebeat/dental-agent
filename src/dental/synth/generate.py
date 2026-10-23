"""Synthetic data generator: patients, an 8-week schedule, far-off follow-up appointments.

Reproducible from the seed in clinic.toml. Run:
    uv run python -m dental.synth.generate [--out data/snapshot.json] [--seed N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from dental.calendar import ClinicCalendar
from dental.config import CONFIG_PATH, ROOT, WEEKDAYS, ClinicConfig, config_sha, load_config
from dental.synth import fixtures as fx
from dental.synth.names import GIVEN, SURNAMES

GENERATOR_VERSION = 1
TEST_CAP = 1   # random test patients have at most one active appointment


class Board:
    """Cell occupancy (chair, start of a 30-minute slot)."""

    def __init__(self, slot: timedelta, reserved: set) -> None:
        self.slot = slot
        self.reserved = reserved
        self.busy: dict[tuple[int, datetime], int] = {}
        self.by_day: dict[date, int] = defaultdict(int)

    def cells(self, chair: int, start: datetime, n: int):
        return [(chair, start + i * self.slot) for i in range(n)]

    def free(self, chair: int, start: datetime, n: int, use_reserved: bool = True) -> bool:
        for cell in self.cells(chair, start, n):
            if cell in self.busy or (use_reserved and cell in self.reserved):
                return False
        return True

    def take(self, chair: int, start: datetime, n: int, idx: int) -> None:
        for cell in self.cells(chair, start, n):
            self.busy[cell] = idx
            self.by_day[cell[1].date()] += 1


class Roster:
    """Patients' active appointments: the limit, overlaps, the "service + day" duplicate."""

    def __init__(self, default_cap: int) -> None:
        self.default_cap = default_cap
        self.caps: dict[int, int] = {}
        self.active: dict[int, list] = defaultdict(list)

    def ok(self, pid: int, service: str, start: datetime, end: datetime) -> bool:
        lst = self.active[pid]
        if len(lst) >= self.caps.get(pid, self.default_cap):
            return False
        for s, e, svc in lst:
            if s < end and start < e:
                return False
            if svc == service and s.date() == start.date():
                return False
        return True

    def add(self, pid: int, service: str, start: datetime, end: datetime) -> None:
        self.active[pid].append((start, end, service))


def _hhmm(t: str):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _window_starts(cal: ClinicCalendar, d: date, t0: str, t1: str) -> list[datetime]:
    return [s for s in cal.valid_starts(d, 1) if _hhmm(t0) <= s.hour * 60 + s.minute < _hhmm(t1)]


def build_snapshot(cfg: ClinicConfig) -> dict:
    syn = cfg.synthetic
    now = cfg.now()
    if now.isoformat() != fx.EXPECTED_NOW:
        raise ValueError(f"the fixtures are built for {fx.EXPECTED_NOW}, but the config has {now.isoformat()}")
    rng = random.Random(syn.seed)
    cal = ClinicCalendar(cfg)
    tz, slot = cal.tz, cal.slot
    chairs = list(range(1, cfg.clinic.chairs + 1))
    today = now.date()
    earliest = now + timedelta(hours=cfg.rules.min_lead_hours)

    # ---------- patients ----------
    fixture_names = {p["name"] for p in fx.FIXTURE_PATIENTS.values()}
    combos = [(s, g) for s in SURNAMES for g in GIVEN]
    rng.shuffle(combos)
    names = [(f"{s[0]} {g[0]}", f"{s[1]} {g[1]}") for s, g in combos]
    names = [n for n in names if n[0] not in fixture_names]
    name_iter = iter(names)

    def rand_dt(a: datetime, minutes: int) -> datetime:
        return a + timedelta(minutes=rng.randint(0, minutes))

    patients: list[dict] = []
    for key, p in fx.FIXTURE_PATIENTS.items():
        if p["id"] is None:
            continue
        patients.append(dict(
            id=p["id"], card_number=p["card"], name=p["name"], name_kana=p["name_kana"], phone=p["phone"],
            birth_date=p["birth_date"],
            is_synthetic=True, group="fixture", key=key,
            created_at=rand_dt(datetime(2024, 1, 1, 9, tzinfo=tz), 800 * 24 * 60).isoformat(),
        ))
    next_id = max(p["id"] for p in patients) + 1
    card, phone_n = 100100, 100
    n_test = syn.test_patients - len(fx.FIXTURE_PATIENTS)   # P_NEW counts towards the 60 but is not in the database
    test_ids, bg_ids = [], []
    for group, count, bucket in (("test", n_test, test_ids), ("background", syn.background_patients, bg_ids)):
        for _ in range(count):
            name, kana = next(name_iter)
            patients.append(dict(
                id=next_id, card_number=card, name=name, name_kana=kana, phone=f"070-0000-{phone_n:04d}",
                birth_date=(date(1940, 1, 1) + timedelta(days=rng.randint(0, 365 * 75))).isoformat(),
                is_synthetic=True, group=group, key=None,
                created_at=rand_dt(datetime(2024, 1, 1, 9, tzinfo=tz), 1000 * 24 * 60).isoformat(),
            ))
            bucket.append(next_id)
            next_id += 1; card += 1; phone_n += 1
    pool = test_ids + bg_ids
    key_to_pid = {k: p["id"] for k, p in fx.FIXTURE_PATIENTS.items() if p["id"] is not None}

    # ---------- appointments ----------
    reserved = set()
    for d_iso, t0, t1 in fx.RESERVED_FREE:
        for s in _window_starts(cal, date.fromisoformat(d_iso), t0, t1):
            reserved.add((fx.RESERVED_CHAIR, s))
    board = Board(slot, reserved)
    roster = Roster(cfg.rules.max_active_per_patient)
    for pid in test_ids:
        roster.caps[pid] = TEST_CAP
    appts: list[dict] = []

    def add_appt(pid, service, chair, start, status, source, created_at=None, use_reserved=True):
        n = cfg.services[service].duration_slots
        end = start + n * slot
        if status == "active":
            if not board.free(chair, start, n, use_reserved=use_reserved):
                raise RuntimeError(f"cell taken: chair {chair}, {start.isoformat()}")
            board.take(chair, start, n, len(appts))
            roster.add(pid, service, start, end)
        if created_at is None:
            created_at = now - timedelta(minutes=rng.randint(60, 40 * 24 * 60))
        appts.append(dict(
            patient_id=pid, service_code=service, chair_id=chair, start=start.isoformat(), end=end.isoformat(),
            day=start.date().isoformat(), status=status, source=source, note=None,
            actor="admin", idempotency_key=None,
            created_at=created_at.isoformat(), cancelled_at=None, cancel_reason=None, rescheduled_from_id=None,
        ))

    def pick_patient(service, start, end):
        eligible = [pid for pid in pool if roster.ok(pid, service, start, end)]
        return rng.choice(eligible) if eligible else None

    for a in fx.FIXTURE_APPOINTMENTS:
        start = datetime.fromisoformat(a["start"]).replace(tzinfo=tz)
        add_appt(key_to_pid[a["patient"]], a["service"], a["chair"], start, a["status"], "phone",
                 created_at=now - timedelta(days=30))

    one_slot = [c for c, s in cfg.services.items() if s.duration_slots == 1]
    for d_iso, t0, t1, chair_list in fx.FORCED_BUSY:
        d = date.fromisoformat(d_iso)
        for start in _window_starts(cal, d, t0, t1):
            for chair in chair_list:
                service = rng.choice(one_slot)
                end = start + slot
                pid = pick_patient(service, start, end)
                if pid is None:
                    raise RuntimeError("patient pool exhausted (F_BUSY/F_FULL)")
                add_appt(pid, service, chair, start, "active", syn.background_source, use_reserved=False)

    codes = list(cfg.services)
    weights = [syn.service_mix[c] for c in codes]
    load = syn.load
    horizon_end = today + timedelta(days=syn.schedule_weeks * 7)

    def feasible(d: date, n: int):
        return [(c, s) for s in cal.valid_starts(d, n) if s >= earliest for c in chairs if board.free(c, s, n)]

    for d in cal.open_dates(today, horizon_end - timedelta(days=1)):
        w = (d - today).days // 7
        occ = min(load.cap, load.by_weeks_ahead[w] * load.weekday_multiplier.get(WEEKDAYS[d.weekday()], 1.0))
        avail = len([s for s in cal.valid_starts(d, 1) if s >= earliest]) * len(chairs)
        target = round(occ * avail)
        while board.by_day[d] < target:
            service = rng.choices(codes, weights=weights)[0]
            options = feasible(d, cfg.services[service].duration_slots)
            if not options:
                for alt in rng.sample(codes, len(codes)):
                    options = feasible(d, cfg.services[alt].duration_slots)
                    if options:
                        service = alt
                        break
                else:
                    break
            for _ in range(30):
                chair, start = rng.choice(options)
                end = start + cfg.services[service].duration_slots * slot
                pid = pick_patient(service, start, end)
                if pid is not None:
                    break
            else:
                raise RuntimeError("patient pool exhausted")
            add_appt(pid, service, chair, start, "active", syn.background_source)

    # far-off follow-up visits: cleaning and check-up beyond the 8 weeks (up to 200 days)
    jan = {date.fromisoformat(x) for x in fx.F_JAN_DAYS}
    for _ in range(syn.far_recall_count):
        service = rng.choice(["cleaning", "kenshin"])
        last = today + timedelta(days=cfg.horizon_days(service))
        days = [d for d in cal.open_dates(horizon_end, last) if d not in jan]
        for _ in range(30):
            d = rng.choice(days)
            start = rng.choice(cal.valid_starts(d, cfg.services[service].duration_slots))
            end = start + cfg.services[service].duration_slots * slot
            chair = rng.choice(chairs)
            pid = pick_patient(service, start, end)
            if pid is not None and board.free(chair, start, 1):
                add_appt(pid, service, chair, start, "active", syn.background_source)
                break
        else:
            raise RuntimeError("could not place a far-off appointment")

    appts.sort(key=lambda a: (a["start"], a["chair_id"], a["patient_id"]))
    for i, a in enumerate(appts, 1):
        a["id"] = i

    services = {}
    for code, s in cfg.services.items():
        services[code] = dict(
            label_ja=s.label_ja, duration_slots=s.duration_slots, new_patient_only=s.new_patient_only,
            recall_months=s.recall_months, horizon_days=cfg.horizon_days(code),
            min_lead_hours=cfg.rules.min_lead_hours if s.min_lead_hours is None else s.min_lead_hours,
            cancel_before_hours=cfg.rules.cancel_before_hours if s.cancel_before_hours is None else s.cancel_before_hours,
            allowed_windows=None if s.allowed_windows is None else [[a.strftime("%H:%M"), b.strftime("%H:%M")] for a, b in s.allowed_windows],
            is_active=s.is_active,
        )
    snap = dict(
        meta=dict(generator_version=GENERATOR_VERSION, seed=syn.seed, frozen_now=now.isoformat(),
                  config_sha=config_sha(), snapshot_sha=None),
        settings=dict(
            timezone=cfg.clinic.timezone, chairs=cfg.clinic.chairs, slot_minutes=cfg.clinic.slot_minutes,
            closed_weekdays=cfg.clinic.closed_weekdays, closed_on_holidays=cfg.clinic.closed_on_holidays,
            hours=[dict(weekdays=h.weekdays, open=h.open.strftime("%H:%M"), close=h.close.strftime("%H:%M")) for h in cfg.hours],
            rules=cfg.rules.model_dump(),
        ),
        chairs=chairs,
        services=services,
        patients=patients,
        appointments=appts,
        fixtures=fx.FIXTURE_PATIENTS,
    )
    snap["meta"]["snapshot_sha"] = hashlib.sha256(
        json.dumps(snap, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    return snap


def dumps(snap: dict) -> str:
    return json.dumps(snap, sort_keys=True, ensure_ascii=False, indent=1)


def main(argv: list[str] | None = None) -> int:
    from dental.synth.verify import load_stats, verify_snapshot

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "data" / "snapshot.json"))
    ap.add_argument("--seed", type=int, help="override the seed from clinic.toml (to check robustness)")
    args = ap.parse_args(argv)

    cfg = load_config(CONFIG_PATH)
    if args.seed is not None:
        cfg.synthetic.seed = args.seed
    snap = build_snapshot(cfg)
    problems = verify_snapshot(snap, cfg)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dumps(snap), encoding="utf-8")

    st = load_stats(snap, cfg)
    print(f"seed={snap['meta']['seed']}  config={snap['meta']['config_sha']}  snapshot={snap['meta']['snapshot_sha']}")
    print(f"patients in the database: {len(snap['patients'])}  appointments: {st['appointments_total']} "
          f"(active {st['active']}, far-off {st['far']})")
    print("occupancy by week:", " ".join(f"{x:.0%}" for x in st["occupancy_by_week"]),
          f"| whole window {st['occupancy_all']:.0%}")
    print(f"file: {out}")
    if problems:
        print(f"\nPROBLEMS ({len(problems)}):")
        for p in problems[:30]:
            print(" -", p)
        return 1
    print("checks: all fixture guarantees and invariants hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())

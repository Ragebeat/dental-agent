import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from dental.calendar import ClinicCalendar
from dental.config import load_config
from dental.core import errors as E
from dental.core.db import connect
from dental.synth.fixtures import FIXTURE_PATIENTS

CFG = load_config()
NOW = CFG.now()                      # 2026-10-07 10:00 JST
TZ = ZoneInfo("Asia/Tokyo")
CAL = ClinicCalendar(CFG)
PID = {k: v["id"] for k, v in FIXTURE_PATIENTS.items() if v["id"] is not None}
KW = dict(source="web", actor="patient_web", now=NOW)


def jst(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


def run_parallel(n, fn):
    """n threads start at once; domain errors are collected, other exceptions fail the test."""
    barrier = threading.Barrier(n)

    def task(i):
        barrier.wait(timeout=30)
        try:
            return ("ok", fn(i))
        except E.DentalError as e:
            return ("err", e)

    with ThreadPoolExecutor(n) as ex:
        return list(ex.map(task, range(n)))


def free_patients(db_url, day: date, n: int):
    """Patients with exactly one active appointment and none on this day (they can book once more)."""
    with connect(db_url) as c:
        rows = c.execute(
            "SELECT p.id FROM patients p WHERE p.id >= 6"
            " AND (SELECT count(*) FROM appointments a WHERE a.patient_id = p.id AND a.status = 'active') = 1"
            " AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.patient_id = p.id AND a.day = %s)"
            " ORDER BY p.id LIMIT %s", [day, n]).fetchall()
    assert len(rows) == n
    return [r["id"] for r in rows]


def assert_invariants(db_url):
    """What must never be violated: chairs, patients, duplicates, hours, the limit."""
    with connect(db_url) as c:
        q = lambda sql: c.execute(sql).fetchone()["n"]  # noqa: E731
        assert q("SELECT count(*) AS n FROM appointments a JOIN appointments b ON a.id < b.id AND a.status='active'"
                 " AND b.status='active' AND a.chair_id = b.chair_id AND a.during && b.during") == 0, "chair double-booked"
        assert q("SELECT count(*) AS n FROM appointments a JOIN appointments b ON a.id < b.id AND a.status='active'"
                 " AND b.status='active' AND a.patient_id = b.patient_id AND a.during && b.during") == 0, "patient in two places"
        assert q("SELECT count(*) AS n FROM (SELECT 1 FROM appointments WHERE status='active' GROUP BY patient_id,"
                 " service_code, day HAVING count(*) > 1) t") == 0, "duplicate"
        assert q("SELECT count(*) AS n FROM (SELECT 1 FROM appointments WHERE status='active' GROUP BY patient_id"
                 " HAVING count(*) > 2) t") == 0, "more than 2 active"
        for r in c.execute("SELECT id, service_code, during FROM appointments WHERE status='active'").fetchall():
            n = CFG.services[r["service_code"]].duration_slots
            s = r["during"].lower.astimezone(TZ)
            assert s in CAL.valid_starts(s.date(), n), f"appointment {r['id']} outside hours: {s}"
            assert s >= NOW, f"appointment {r['id']} in the past"

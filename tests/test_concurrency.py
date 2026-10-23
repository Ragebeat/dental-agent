from datetime import date

import pytest

from dental.core import errors as E
from dental.core.db import connect
from helpers import KW, NOW, PID, assert_invariants, free_patients, jst, run_parallel

SLOT = jst(2026, 10, 14, 16, 0)       # Wednesday, chair 4 free


def count(db_url, sql, *a):
    with connect(db_url) as c:
        return c.execute(sql, list(a)).fetchone()["n"]


def test_20_threads_one_slot_pinned_chair(core, db_url):
    pats = free_patients(db_url, date(2026, 10, 14), 20)
    res = run_parallel(20, lambda i: core.book(pats[i], "cleaning", SLOT, chair_id=4, **KW))
    ok = [r for k, r in res if k == "ok"]
    assert len(ok) == 1
    assert all(isinstance(r, E.SlotTaken) for k, r in res if k == "err")
    assert count(db_url, "SELECT count(*) AS n FROM appointments WHERE chair_id=4 AND status='active'"
                         " AND lower(during)=%s", SLOT) == 1
    assert_invariants(db_url)


def test_unpinned_exactly_four(core, db_url):
    """A slot free on all 4 chairs: 20 takers → exactly 4 appointments."""
    with connect(db_url) as c:
        c.execute("UPDATE appointments SET status='cancelled', cancelled_at=%s WHERE status='active' AND lower(during) <= %s"
                  " AND upper(during) > %s AND day = %s", [NOW, SLOT, SLOT, SLOT.date()])
        c.commit()
    pats = free_patients(db_url, date(2026, 10, 14), 20)
    res = run_parallel(20, lambda i: core.book(pats[i], "cleaning", SLOT, **KW))
    assert sum(k == "ok" for k, _ in res) == 4
    assert all(isinstance(r, E.SlotTaken) for k, r in res if k == "err")
    assert_invariants(db_url)


def test_same_patient_many_clicks(core, db_url):
    res = run_parallel(10, lambda i: core.book(PID["P_CLEAN"], "cleaning", SLOT, **KW))
    assert sum(k == "ok" for k, _ in res) == 1
    assert count(db_url, "SELECT count(*) AS n FROM appointments WHERE patient_id=%s AND status='active'",
                 PID["P_CLEAN"]) == 1
    assert_invariants(db_url)


def test_same_patient_same_key_idempotent(core, db_url):
    res = run_parallel(10, lambda i: core.book(PID["P_CLEAN"], "cleaning", SLOT, idempotency_key="k", **KW))
    assert all(k == "ok" for k, _ in res)
    assert len({r.id for _, r in res}) == 1
    assert count(db_url, "SELECT count(*) AS n FROM appointments WHERE patient_id=%s AND status='active'",
                 PID["P_CLEAN"]) == 1


def test_limit_under_load(core, db_url):
    days = [jst(2026, 10, d, 16, 0) for d in (13, 14, 16, 17, 20, 21, 22, 23)]
    days = [d for d in days if d.weekday() != 3][:10]
    res = run_parallel(len(days), lambda i: core.book(PID["P_CLEAN"], "cleaning", days[i], **KW))
    ok = sum(k == "ok" for k, _ in res)
    assert ok == 2
    assert all(isinstance(r, (E.LimitReached, E.SlotTaken)) for k, r in res if k == "err")
    assert_invariants(db_url)


def test_cancel_vs_reschedule_race(core, db_url):
    with connect(db_url) as c:
        aid = c.execute("SELECT id FROM appointments WHERE patient_id=%s AND status='active'",
                        [PID["P_ONE"]]).fetchone()["id"]

    def op(i):
        if i % 2:
            return core.cancel(PID["P_ONE"], aid, actor="patient_web", now=NOW)
        return core.reschedule(PID["P_ONE"], aid, jst(2026, 10, 14, 16, 0), actor="patient_web", now=NOW)

    res = run_parallel(2, op)
    assert sum(k == "ok" for k, _ in res) == 1
    assert all(isinstance(r, E.AlreadyCancelled) for k, r in res if k == "err")
    n = count(db_url, "SELECT count(*) AS n FROM appointments WHERE patient_id=%s AND status='active'", PID["P_ONE"])
    assert n in (0, 1)
    assert_invariants(db_url)


def test_double_cancel_race(core, db_url):
    with connect(db_url) as c:
        aid = c.execute("SELECT id FROM appointments WHERE patient_id=%s AND status='active'",
                        [PID["P_ONE"]]).fetchone()["id"]
    res = run_parallel(8, lambda i: core.cancel(PID["P_ONE"], aid, actor="patient_web", now=NOW))
    assert sum(k == "ok" for k, _ in res) == 1


def test_registration_race(core, db_url):
    def reg(i):
        return core.register_patient(name="新 患者", name_kana="アラタ カンジャ", phone="070-0000-0006",
                                     birth_date=date(1995, 5, 17), now=NOW)
    res = run_parallel(10, reg)
    assert sum(k == "ok" for k, _ in res) == 1
    assert all(isinstance(r, E.AlreadyRegistered) for k, r in res if k == "err")
    assert count(db_url, "SELECT count(*) AS n FROM patients WHERE phone='070-0000-0006'") == 1


def test_bruteforce_lockout(core, db_url):
    def attempt(i):
        return core.verify_patient(100001, date(2000, 1, 1 + i % 28), channel="web", client_key="c1", now=NOW)
    res = run_parallel(30, attempt)
    assert not [r for k, r in res if k == "ok"]
    bad = sum(isinstance(r, E.PatientNotVerified) for k, r in res if k == "err")
    locked = sum(isinstance(r, E.Locked) for k, r in res if k == "err")
    assert bad <= 5 and bad + locked == 30
    assert count(db_url, "SELECT count(*) AS n FROM auth_attempts") == bad
    # the correct date is locked too
    with pytest.raises(E.Locked):
        core.verify_patient(100001, date(1988, 4, 12), channel="web", client_key="c2", now=NOW)

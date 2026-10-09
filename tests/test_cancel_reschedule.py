import pytest

from dental.core import errors as E
from dental.core.db import connect

from helpers import KW, NOW, PID, assert_invariants, jst


def appt_id(db_url, pid, status="active", order="ASC"):
    with connect(db_url) as c:
        return c.execute(f"SELECT id FROM appointments WHERE patient_id=%s AND status=%s ORDER BY during {order}",
                         [pid, status]).fetchone()["id"]


def test_cancel_ok(core, db_url):
    aid = appt_id(db_url, PID["P_ONE"])
    a = core.cancel(PID["P_ONE"], aid, actor="patient_web", now=NOW)
    assert a.status == "cancelled"
    with pytest.raises(E.AlreadyCancelled):
        core.cancel(PID["P_ONE"], aid, actor="patient_web", now=NOW)


def test_cancel_too_late(core, db_url):
    aid = appt_id(db_url, PID["P_SOON"])
    with pytest.raises(E.TooLateToCancel) as ei:
        core.cancel(PID["P_SOON"], aid, actor="patient_web", now=NOW)
    assert ei.value.details.get("handoff_reason") == "late_cancel"


def test_cannot_touch_others(core, db_url):
    aid = appt_id(db_url, PID["P_ONE"])
    with pytest.raises(E.NotFound):
        core.cancel(PID["P_CLEAN"], aid, actor="patient_web", now=NOW)
    with pytest.raises(E.NotFound):
        core.reschedule(PID["P_CLEAN"], aid, jst(2026, 10, 14, 16, 0), actor="patient_web", now=NOW)


def test_reschedule_ok(core, db_url):
    aid = appt_id(db_url, PID["P_ONE"])
    new = core.reschedule(PID["P_ONE"], aid, jst(2026, 10, 14, 16, 0), actor="patient_web", now=NOW)
    assert new.status == "active" and new.rescheduled_from_id == aid and new.service_code == "chiryo"
    with connect(db_url) as c:
        assert c.execute("SELECT status FROM appointments WHERE id=%s", [aid]).fetchone()["status"] == "cancelled"
    assert_invariants(db_url)


def test_reschedule_failure_keeps_old(core, db_url):
    aid = appt_id(db_url, PID["P_ONE"])
    with pytest.raises(E.ClosedDay):
        core.reschedule(PID["P_ONE"], aid, jst(2026, 10, 15, 10, 0), actor="patient_web", now=NOW)
    with pytest.raises(E.SlotTaken):
        core.reschedule(PID["P_ONE"], aid, jst(2026, 10, 9, 15, 0), actor="patient_web", now=NOW)
    with connect(db_url) as c:
        assert c.execute("SELECT status FROM appointments WHERE id=%s", [aid]).fetchone()["status"] == "active"


def test_reschedule_same_slot(core, db_url):
    aid = appt_id(db_url, PID["P_ONE"])
    with connect(db_url) as c:
        s = c.execute("SELECT lower(during) AS s FROM appointments WHERE id=%s", [aid]).fetchone()["s"]
    with pytest.raises(E.InvalidInput):
        core.reschedule(PID["P_ONE"], aid, s, actor="patient_web", now=NOW)


def test_reschedule_overlapping_own_window(core, db_url):
    """Сдвиг на 30 мин внутри собственного окна: старая запись отменяется первой, конфликта с собой нет."""
    aid = appt_id(db_url, PID["P_ONE"])
    new = core.reschedule(PID["P_ONE"], aid, jst(2026, 10, 13, 10, 0), actor="patient_web", now=NOW)
    assert new.start == jst(2026, 10, 13, 10, 0)
    assert_invariants(db_url)

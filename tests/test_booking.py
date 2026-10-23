from datetime import date

import pytest

from dental.core import errors as E

from helpers import KW, NOW, PID, assert_invariants, jst

W = jst(2026, 10, 14, 16, 0)   # Wednesday, chair 4 free


def test_success(core, db_url):
    a = core.book(PID["P_CLEAN"], "cleaning", W, **KW)
    assert a.status == "active" and a.start == W and a.chair_id in (1, 2, 3, 4)
    assert a.end - a.start == (core.clinic.calendar.slot)
    assert_invariants(db_url)


def test_pinned_chair(core):
    a = core.book(PID["P_CLEAN"], "cleaning", W, chair_id=4, **KW)
    assert a.chair_id == 4


def test_slot_taken_with_alternatives(core):
    core.book(PID["P_CLEAN"], "cleaning", W, chair_id=4, **KW)
    with pytest.raises(E.SlotTaken) as ei:
        core.book(PID["P_ONE"], "cleaning", W, chair_id=4, **KW)
    assert ei.value.details["alternatives"]


def test_busy_slot_is_taken(core):
    with pytest.raises(E.SlotTaken):
        core.book(PID["P_CLEAN"], "cleaning", jst(2026, 10, 9, 15, 0), **KW)


@pytest.mark.parametrize("start,exc", [
    (jst(2026, 10, 8, 10, 0), E.ClosedDay),            # Thursday
    (jst(2026, 10, 14, 12, 30), E.OutsideHours),        # break
    (jst(2026, 10, 14, 9, 15), E.OutsideHours),         # off the grid
    (jst(2026, 10, 7, 11, 0), E.TooSoon),
    (jst(2026, 12, 22, 10, 0), E.BeyondHorizon),        # >60 days
])
def test_validation(core, start, exc):
    with pytest.raises(exc):
        core.book(PID["P_CLEAN"], "chiryo" if exc is E.BeyondHorizon else "cleaning", start, **KW)


def test_two_slot_cannot_cross_lunch(core):
    with pytest.raises(E.OutsideHours):
        core.book(PID["P_ONE"], "chiryo", jst(2026, 10, 14, 12, 0), **KW)


def test_limit_reached(core):
    with pytest.raises(E.LimitReached):
        core.book(PID["P_OTHER"], "kenshin", W, **KW)


def test_duplicate_and_patient_busy(core):
    core.book(PID["P_CLEAN"], "cleaning", W, **KW)
    with pytest.raises(E.DuplicateBooking):
        core.book(PID["P_CLEAN"], "cleaning", jst(2026, 10, 14, 17, 0), **KW)
    with pytest.raises(E.PatientBusy):
        core.book(PID["P_CLEAN"], "kenshin", W, **KW)


def test_first_visit_rules(core):
    p = core.register_patient(name="新 患者", name_kana="アラタ カンジャ", phone="070-0000-0006",
                              birth_date=date(1995, 5, 17), now=NOW)
    with pytest.raises(E.FirstVisitRequired):
        core.book(p.id, "cleaning", W, **KW)
    a = core.book(p.id, "shoshin", jst(2026, 10, 14, 14, 0), **KW)
    assert a.service_code == "shoshin"
    with pytest.raises(E.NewPatientOnly):
        core.book(PID["P_CLEAN"], "shoshin", W, **KW)    # P_CLEAN has a completed visit


def test_idempotency(core, db_url):
    a = core.book(PID["P_CLEAN"], "cleaning", W, idempotency_key="k1", **KW)
    b = core.book(PID["P_CLEAN"], "cleaning", W, idempotency_key="k1", **KW)
    assert b.id == a.id and b.replayed
    with pytest.raises(E.IdempotencyConflict):
        core.book(PID["P_CLEAN"], "cleaning", jst(2026, 10, 14, 17, 0), idempotency_key="k1", **KW)
    assert_invariants(db_url)


def test_note_length_and_unknown_patient(core):
    with pytest.raises(E.InvalidInput):
        core.book(PID["P_CLEAN"], "cleaning", W, note="x" * 5000, **KW)
    with pytest.raises(E.NotFound):
        core.book(999999, "cleaning", W, **KW)


def test_register_duplicate_name_birth_different_phone(core):
    core.register_patient(name="山田 太郎", name_kana="ヤマダ タロウ", phone="090-1111-2222", birth_date=date(1990, 1, 1), now=NOW)
    for name in ("山田 太郎", "山田太郎", "山田　太郎"):
        with pytest.raises(E.AlreadyRegistered):
            core.register_patient(name=name, name_kana="ヤマダ タロウ", phone="080-9999-0000", birth_date=date(1990, 1, 1), now=NOW)

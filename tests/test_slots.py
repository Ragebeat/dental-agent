from datetime import date, time

import pytest

from dental.core import errors as E

from helpers import NOW, PID, jst


def times(core, code, d1, d2=None, **kw):
    return [s.astimezone(NOW.tzinfo).strftime("%m-%d %H:%M") for s in core.find_slots(code, d1, d2 or d1, now=NOW, **kw)]


def test_f_busy_hides_1500(core):
    t = times(core, "cleaning", date(2026, 10, 9))
    assert "10-09 15:00" not in t
    assert {"10-09 16:00", "10-09 16:30", "10-09 17:00"} <= set(t)


def test_f_full_evening_empty(core):
    t = times(core, "cleaning", date(2026, 10, 13))
    assert not [x for x in t if x[6:] >= "16:00"]


def test_two_slot_never_at_noon(core):
    t = times(core, "chiryo", date(2026, 10, 13), date(2026, 10, 17))
    assert t and not [x for x in t if x[6:] in ("12:00", "12:30", "18:00", "18:30")]
    assert all(x[6:] <= "17:30" for x in t)


def test_closed_days_empty(core):
    # четверг, воскресенье, праздник (2026-11-03 Культуры)
    for d in (date(2026, 10, 8), date(2026, 10, 11), date(2026, 11, 3)):
        assert core.find_slots("cleaning", d, d, now=NOW) == []


def test_min_lead(core):
    # сегодня в 10:00: раньше 12:00 нельзя, 14:00 можно
    t = times(core, "cleaning", date(2026, 10, 7))
    assert all(x[6:] >= "12:00" for x in t)


def test_horizon(core):
    far = date(2027, 1, 19)                                             # +104 дня, вторник
    assert core.find_slots("chiryo", far, far, now=NOW) == []          # горизонт 60
    assert core.find_slots("kenshin", far, far, now=NOW)               # горизонт 200
    assert core.find_slots("kenshin", date(2027, 5, 18), date(2027, 5, 18), now=NOW) == []   # +223 дня


def test_allowed_windows_and_inactive(core, db_url):
    from dental.core.db import connect
    with connect(db_url) as c:
        c.execute("UPDATE services SET is_active = false WHERE code = 'shishu'")
        c.commit()
    core.refresh()
    with pytest.raises(E.ServiceUnavailable):
        core.find_slots("shishu", date(2026, 10, 14), date(2026, 10, 14), now=NOW)

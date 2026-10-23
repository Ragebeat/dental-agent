from datetime import date, time, timedelta

from dental.calendar import ClinicCalendar
from dental.config import load_config

CFG = load_config()
CAL = ClinicCalendar(CFG)


def test_closed_and_open_days():
    assert not CAL.is_open(date(2026, 10, 8))    # Thursday
    assert not CAL.is_open(date(2026, 10, 11))   # Sunday
    assert not CAL.is_open(date(2026, 10, 12))   # スポーツの日
    assert not CAL.is_open(date(2027, 1, 7))     # Thursday
    assert not CAL.is_open(date(2027, 1, 11))    # 成人の日
    for d in (date(2026, 10, 9), date(2026, 10, 17), date(2027, 1, 5), date(2027, 1, 12)):
        assert CAL.is_open(d), d


def test_slot_counts():
    fri, sat = date(2026, 10, 9), date(2026, 10, 17)
    assert len(CAL.valid_starts(fri)) == 16
    assert CAL.valid_starts(fri)[0].time() == time(9, 0)
    assert CAL.valid_starts(fri)[-1].time() == time(18, 0)
    assert len(CAL.valid_starts(sat)) == 14
    assert CAL.valid_starts(sat)[-1].time() == time(17, 0)


def test_two_slot_never_crosses_the_break():
    starts = [s.time() for s in CAL.valid_starts(date(2026, 10, 9), 2)]
    assert time(12, 0) not in starts and time(12, 30) not in starts
    assert max(s for s in starts if s < time(13, 0)) == time(11, 30)
    assert max(starts) == time(17, 30)
    assert len(starts) == 14


def test_horizons():
    today = CFG.now().date()
    assert today + timedelta(days=CFG.horizon_days("chiryo")) == date(2026, 12, 6)
    assert today + timedelta(days=CFG.horizon_days("cleaning")) == date(2027, 4, 25)
    assert today + timedelta(days=CFG.horizon_days("kenshin")) == date(2027, 4, 25)


def test_config_basics():
    assert CFG.now().isoformat() == "2026-10-07T10:00:00+09:00"
    assert CFG.services["shoshin"].duration_slots == 2
    assert CFG.services["cleaning"].duration_slots == 1

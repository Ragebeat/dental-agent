"""Разбор дат кодом: без БД и без LLM. «Сегодня» — среда 2026-10-07 10:00 JST."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from dental.agent.dates import DateIntent, parse_ja, resolve

NOW = datetime(2026, 10, 7, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo"))


def win(text):
    return resolve(parse_ja(text), NOW)


@pytest.mark.parametrize("text, d1, d2, t1, t2", [
    ("明後日の夕方", date(2026, 10, 9), date(2026, 10, 9), time(16, 0), time(18, 0)),            # s03
    ("来週の金曜日の午前中", date(2026, 10, 16), date(2026, 10, 16), time(9, 0), time(11, 59)),   # s14
    ("来週の水曜日の午前", date(2026, 10, 14), date(2026, 10, 14), time(9, 0), time(11, 59)),     # s02: модель считала 12 октября
    ("来週の火曜", date(2026, 10, 13), date(2026, 10, 13), None, None),
    ("金曜", date(2026, 10, 9), date(2026, 10, 9), None, None),                                   # ближайшая после сегодня
    ("水曜日", date(2026, 10, 14), date(2026, 10, 14), None, None),                               # сегодня среда → следующая
    ("明日", date(2026, 10, 8), date(2026, 10, 8), None, None),
    ("今日の午後", date(2026, 10, 7), date(2026, 10, 7), time(12, 0), time(18, 30)),
    ("10月20日の午後3時", date(2026, 10, 20), date(2026, 10, 20), time(15, 0), time(15, 0)),
    ("１０月２０日　１５：３０", date(2026, 10, 20), date(2026, 10, 20), time(15, 30), time(15, 30)),   # полноширинные цифры
    ("10/20 10時半", date(2026, 10, 20), date(2026, 10, 20), time(10, 30), time(10, 30)),
    ("3か月後", date(2027, 1, 7), date(2027, 1, 20), None, None),                                 # s08
    ("半年後", date(2027, 4, 7), date(2027, 4, 20), None, None),
    ("2週間後の午前", date(2026, 10, 21), date(2026, 11, 3), time(9, 0), time(11, 59)),
    ("5日後", date(2026, 10, 12), date(2026, 10, 25), None, None),
    ("来週", date(2026, 10, 12), date(2026, 10, 18), None, None),
    ("今週", date(2026, 10, 7), date(2026, 10, 11), None, None),
    ("再来週の月曜", date(2026, 10, 19), date(2026, 10, 19), None, None),
    ("月末", date(2026, 10, 25), date(2026, 10, 31), None, None),
    ("月初", date(2026, 11, 1), date(2026, 11, 7), None, None),                                   # посреди месяца → начало следующего
    ("3月5日", date(2027, 3, 5), date(2027, 3, 5), None, None),                                   # в этом году прошло → следующий
    ("25日", date(2026, 10, 25), date(2026, 10, 25), None, None),
    ("3日", date(2026, 11, 3), date(2026, 11, 3), None, None),                                    # 3 октября прошло → ноябрь
])
def test_parse_and_resolve(text, d1, d2, t1, t2):
    w = win(text)
    assert (w.date_from, w.date_to, w.time_from, w.time_to) == (d1, d2, t1, t2), text


def test_month_range_is_capped_at_14_days():
    w = win("来月")
    assert (w.date_from, w.date_to, w.truncated) == (date(2026, 11, 1), date(2026, 11, 14), True)


def test_nothing_said_means_next_14_days_and_asap_flag():
    it = parse_ja("なるべく早くお願いします")
    assert it.asap and not it.is_empty()
    w = resolve(it, NOW)
    assert (w.date_from, w.date_to) == (date(2026, 10, 7), date(2026, 10, 20))
    assert parse_ja("こんにちは").is_empty()


def test_this_week_past_weekday_moves_to_next_week():
    w = win("今週の月曜")
    assert w.date_from == date(2026, 10, 12) and w.note


def test_invalid_date_raises():
    with pytest.raises(ValueError):
        resolve(DateIntent(month=2, day=30), NOW)
    # «31日» в ноябре (30 дней) → 31 декабря
    assert resolve(DateIntent(day=31), datetime(2026, 11, 5, tzinfo=ZoneInfo("Asia/Tokyo"))).date_from == date(2026, 12, 31)


def test_structure_from_llm_goes_through_the_same_resolver():
    it = DateIntent.model_validate({"week_offset": 1, "weekday": 4, "part_of_day": "morning"})
    assert resolve(it, NOW).date_from == date(2026, 10, 16)

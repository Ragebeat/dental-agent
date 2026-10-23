"""Fixtures: the contract with the scenarios (docs/FIXTURES.md). Dates are absolute, tied to frozen_now."""

from __future__ import annotations

EXPECTED_NOW = "2026-10-07T10:00:00+09:00"

# id=None: the patient is not in the database at the start (P_NEW). The phone exists: the scenario states it.
FIXTURE_PATIENTS: dict[str, dict] = {
    "P_CLEAN": dict(id=1, name="鈴木 健太", name_kana="すずき けんた", card=100001, phone="070-0000-0001", birth_date="1988-04-12"),
    "P_ONE":   dict(id=2, name="高橋 さくら", name_kana="たかはし さくら", card=100002, phone="070-0000-0002", birth_date="1979-11-03"),
    "P_SOON":  dict(id=3, name="伊藤 大輔", name_kana="いとう だいすけ", card=100003, phone="070-0000-0003", birth_date="1992-07-21"),
    "P_OTHER": dict(id=4, name="渡辺 優子", name_kana="わたなべ ゆうこ", card=100004, phone="070-0000-0004", birth_date="1985-02-28"),
    "P_TWO":   dict(id=5, name="小林 誠", name_kana="こばやし まこと", card=100005, phone="070-0000-0005", birth_date="1971-09-09"),
    "P_NEW":   dict(id=None, name="山本 美咲", name_kana="やまもと みさき", card=None, phone="070-0000-0006", birth_date="1995-05-17"),
}

# Fixture appointments (chair 1; the free-window reserve below lives on chair 4).
FIXTURE_APPOINTMENTS = [
    dict(patient="P_CLEAN", service="cleaning", start="2026-06-05T10:00", chair=1, status="completed"),
    dict(patient="P_ONE",   service="chiryo",   start="2026-10-13T10:30", chair=1, status="active"),
    dict(patient="P_SOON",  service="chiryo",   start="2026-10-07T16:30", chair=1, status="active"),
    dict(patient="P_OTHER", service="chiryo",   start="2026-10-09T10:00", chair=1, status="active"),
    dict(patient="P_OTHER", service="cleaning", start="2026-10-14T14:30", chair=1, status="active"),
    dict(patient="P_TWO",   service="cleaning", start="2026-10-20T09:30", chair=1, status="active"),
    dict(patient="P_TWO",   service="kenshin",  start="2026-10-27T09:30", chair=1, status="active"),
]

# Forcibly occupied on all listed chairs: (date, from, to (exclusive), chairs).
FORCED_BUSY = [
    ("2026-10-09", "15:00", "15:30", [1, 2, 3, 4]),   # F_BUSY
    ("2026-10-13", "16:00", "18:30", [1, 2, 3, 4]),   # F_FULL: all 5 starts 16:00–18:00
]

# Windows the background never takes (chair 4): guarantees for F_FRI09, F_WED14, F_FRI16, F_NEXTWEEK and the scenarios.
RESERVED_CHAIR = 4
RESERVED_FREE = [
    ("2026-10-09", "14:00", "15:00"), ("2026-10-09", "16:00", "17:30"),
    ("2026-10-13", "09:00", "11:00"), ("2026-10-13", "14:00", "16:00"),
    ("2026-10-14", "09:00", "11:00"), ("2026-10-14", "14:00", "17:30"),
    ("2026-10-16", "09:00", "11:00"), ("2026-10-16", "14:00", "16:00"),
    ("2026-10-17", "09:00", "11:00"), ("2026-10-17", "14:00", "17:30"),
]

# F_JAN: nothing lands on these days (neither background nor far-off follow-up appointments).
F_JAN_DAYS = ["2027-01-05", "2027-01-06", "2027-01-08", "2027-01-09", "2027-01-12"]

# F_NEXTWEEK: on these days at least this many starts with at least one free chair.
NEXTWEEK_DAYS = ["2026-10-13", "2026-10-14", "2026-10-16", "2026-10-17"]
NEXTWEEK_MIN_STARTS = 8

# Expected number of active appointments of the fixture patients.
FIXTURE_ACTIVE_COUNT = {"P_CLEAN": 0, "P_ONE": 1, "P_SOON": 1, "P_OTHER": 2, "P_TWO": 2}

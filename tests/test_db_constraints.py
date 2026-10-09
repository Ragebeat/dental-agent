"""Гарантии на уровне БД: их не заменяет ни ядро, ни промпт."""

import psycopg
import pytest
from psycopg.types.range import Range

from dental.core.db import connect
from helpers import jst

INSERT = ("INSERT INTO appointments (patient_id, service_code, chair_id, during, day, status, source, created_at)"
          " VALUES (%s, %s, %s, %s, %s, %s, 'phone', %s)")


def ins(c, pid, svc, chair, s, e, status="active"):
    c.execute(INSERT, [pid, svc, chair, Range(s, e, "[)"), s.date(), status, jst(2026, 10, 1)])


def test_loaded(db_url):
    with connect(db_url) as c:
        assert c.execute("SELECT count(*) AS n FROM patients").fetchone()["n"] == 509
        assert c.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 638


def test_chair_cannot_be_double_booked(db_url):
    with connect(db_url) as c:
        # кресло 4 вт 2026-10-14 16:00 свободно (резерв). Первая запись проходит, вторая на то же кресло нет.
        ins(c, 6, "cleaning", 4, jst(2026, 10, 14, 16), jst(2026, 10, 14, 16, 30))
        with pytest.raises(psycopg.errors.ExclusionViolation) as e:
            ins(c, 7, "cleaning", 4, jst(2026, 10, 14, 16), jst(2026, 10, 14, 16, 30))
        assert e.value.diag.constraint_name == "appointments_no_double_booking"


def test_partial_overlap_also_blocked_and_adjacent_allowed(db_url):
    with connect(db_url) as c:
        ins(c, 6, "chiryo", 4, jst(2026, 10, 14, 14), jst(2026, 10, 14, 15))
        with pytest.raises(psycopg.errors.ExclusionViolation):
            ins(c, 7, "cleaning", 4, jst(2026, 10, 14, 14, 30), jst(2026, 10, 14, 15))
        c.rollback()
        ins(c, 6, "chiryo", 4, jst(2026, 10, 14, 14), jst(2026, 10, 14, 15))
        ins(c, 7, "cleaning", 4, jst(2026, 10, 14, 15), jst(2026, 10, 14, 15, 30))   # встык: можно


def test_cancelled_does_not_block(db_url):
    with connect(db_url) as c:
        c.execute("INSERT INTO appointments (patient_id, service_code, chair_id, during, day, status, source, created_at,"
                  " cancelled_at) VALUES (6,'cleaning',4,%s,%s,'cancelled','phone',%s,%s)",
                  [Range(jst(2026, 10, 14, 16), jst(2026, 10, 14, 16, 30), "[)"), jst(2026, 10, 14).date(),
                   jst(2026, 10, 1), jst(2026, 10, 2)])
        ins(c, 7, "cleaning", 4, jst(2026, 10, 14, 16), jst(2026, 10, 14, 16, 30))


def test_patient_cannot_be_in_two_places(db_url):
    with connect(db_url) as c:
        ins(c, 6, "cleaning", 4, jst(2026, 10, 14, 16), jst(2026, 10, 14, 16, 30))
        with pytest.raises(psycopg.errors.ExclusionViolation) as e:
            ins(c, 6, "kenshin", 4, jst(2026, 10, 14, 16), jst(2026, 10, 14, 16, 30))
        assert e.value.diag.constraint_name in ("appointments_patient_no_overlap", "appointments_no_double_booking")


def test_duplicate_service_same_day(db_url):
    with connect(db_url) as c:
        ins(c, 6, "cleaning", 4, jst(2026, 10, 14, 16), jst(2026, 10, 14, 16, 30))
        with pytest.raises(psycopg.errors.UniqueViolation) as e:
            ins(c, 6, "cleaning", 4, jst(2026, 10, 14, 17), jst(2026, 10, 14, 17, 30))
        assert e.value.diag.constraint_name == "appointments_no_duplicate"


def test_grid_is_enforced(db_url):
    with connect(db_url) as c:
        with pytest.raises(psycopg.errors.CheckViolation):
            ins(c, 6, "cleaning", 4, jst(2026, 10, 14, 16, 15), jst(2026, 10, 14, 16, 45))

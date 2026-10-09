"""Миграции и заливка снимка синтетики в Postgres.

    uv run python -m dental.core.loader --reset      # пересоздать схему и залить data/snapshot.json
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from dental.config import ROOT, load_config
from dental.core.db import connect, database_url, with_dbname

MIGRATIONS = ROOT / "migrations"


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                 "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
    applied = []
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name in done:
            continue
        conn.execute(f.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", [f.name])
        applied.append(f.name)
    conn.commit()
    return applied


def reset_schema(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.commit()


def _many(conn: psycopg.Connection, sql: str, rows: list) -> None:
    with conn.cursor() as cur:
        cur.executemany(sql, rows)


def load_snapshot(conn: psycopg.Connection, snap: dict) -> dict:
    now = snap["meta"]["frozen_now"]
    st = snap["settings"]
    with conn.transaction():
        schedule = {k: v for k, v in st.items() if k != "rules"}
        conn.execute("INSERT INTO clinic_settings (key, value, updated_at) VALUES ('schedule', %s, %s::timestamptz)",
                     [Jsonb(schedule), now])
        conn.execute("INSERT INTO clinic_settings (key, value, updated_at) VALUES ('rules', %s, %s::timestamptz)",
                     [Jsonb(st["rules"]), now])
        _many(conn, "INSERT INTO chairs (id, label) VALUES (%s, %s)", [(c, f"chair {c}") for c in snap["chairs"]])
        _many(conn, 
            "INSERT INTO services (code, label_ja, duration_slots, horizon_days, min_lead_hours, cancel_before_hours,"
            " allowed_windows, recall_months, new_patient_only, is_active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [(c, s["label_ja"], s["duration_slots"], s["horizon_days"], s["min_lead_hours"], s["cancel_before_hours"],
              None if s["allowed_windows"] is None else Jsonb(s["allowed_windows"]), s["recall_months"],
              s["new_patient_only"], s["is_active"]) for c, s in snap["services"].items()])
        _many(conn, 
            "INSERT INTO patients (id, card_number, name, name_kana, phone, birth_date, is_synthetic, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s::date,%s,%s::timestamptz)",
            [(p["id"], p["card_number"], p["name"], p["name_kana"], p["phone"], p["birth_date"], p["is_synthetic"],
              p["created_at"]) for p in snap["patients"]])
        _many(conn, 
            "INSERT INTO patient_intake (patient_id, status, answers, updated_at) VALUES (%s,'not_started','{}',%s::timestamptz)",
            [(p["id"], now) for p in snap["patients"]])
        _many(conn, 
            "INSERT INTO appointments (id, patient_id, service_code, chair_id, during, day, status, source, note, actor,"
            " idempotency_key, created_at, cancelled_at, cancel_reason, rescheduled_from_id)"
            " VALUES (%s,%s,%s,%s,tstzrange(%s::timestamptz, %s::timestamptz, '[)'),%s::date,%s,%s,%s,%s,%s,"
            "%s::timestamptz,%s::timestamptz,%s,%s)",
            [(a["id"], a["patient_id"], a["service_code"], a["chair_id"], a["start"], a["end"], a["day"], a["status"],
              a["source"], a["note"], a["actor"], a["idempotency_key"], a["created_at"], a["cancelled_at"],
              a["cancel_reason"], a["rescheduled_from_id"]) for a in snap["appointments"]])
        conn.execute("SELECT setval('patients_id_seq', (SELECT max(id) FROM patients))")
        conn.execute("SELECT setval('patient_card_seq', (SELECT max(card_number) FROM patients))")
        conn.execute("SELECT setval('appointments_id_seq', (SELECT max(id) FROM appointments))")
    return dict(patients=len(snap["patients"]), appointments=len(snap["appointments"]))


# ---------- базы для тестов: шаблон + копии ----------
def _admin(url: str) -> psycopg.Connection:
    return connect(with_dbname(url, "postgres"), autocommit=True)


def create_database(url: str, name: str, template: str | None = None) -> None:
    with _admin(url) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else ""))


def drop_database(url: str, name: str) -> None:
    with _admin(url) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def build_template(url: str, name: str, snap: dict) -> None:
    create_database(url, name)
    with connect(with_dbname(url, name)) as conn:
        apply_migrations(conn)
        load_snapshot(conn, snap)


def unique_name(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", default=str(ROOT / "data" / "snapshot.json"))
    ap.add_argument("--reset", action="store_true", help="удалить схему public и создать заново")
    args = ap.parse_args(argv)

    path = Path(args.snapshot)
    if path.exists():
        snap = json.loads(path.read_text(encoding="utf-8"))
    else:
        from dental.synth.generate import build_snapshot
        print(f"{path} нет, строю снимок из clinic.toml в памяти")
        snap = build_snapshot(load_config())
    url = database_url()
    with connect(url) as conn:
        if args.reset:
            reset_schema(conn)
        print("миграции:", apply_migrations(conn) or "нечего применять")
        have = conn.execute("SELECT count(*) AS n FROM patients").fetchone()["n"]
        if have:
            print(f"в БД уже {have} пациентов; для чистой заливки запусти с --reset")
            return 1
        print("залито:", load_snapshot(conn, snap), "| снимок", snap["meta"]["snapshot_sha"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

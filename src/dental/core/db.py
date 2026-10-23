"""Postgres connection. DATABASE_URL from the environment, else from .env, else from POSTGRES_* (port 5441)."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from dental.config import ROOT

DEFAULT_PORT = 5441


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def database_url() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env = _read_env(ROOT / ".env")
    if env.get("DATABASE_URL"):
        return env["DATABASE_URL"]
    user = os.environ.get("POSTGRES_USER") or env.get("POSTGRES_USER", "dental")
    password = os.environ.get("POSTGRES_PASSWORD") or env.get("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError("neither DATABASE_URL nor POSTGRES_PASSWORD is set (see .env.example)")
    db = os.environ.get("POSTGRES_DB") or env.get("POSTGRES_DB", "dental")
    return f"postgresql://{quote(user)}:{quote(password)}@127.0.0.1:{DEFAULT_PORT}/{db}"


def with_dbname(url: str, dbname: str) -> str:
    d = conninfo_to_dict(url)
    d["dbname"] = dbname
    return make_conninfo(**d)


def connect(url: str | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(url or database_url(), autocommit=autocommit, row_factory=dict_row, connect_timeout=10)

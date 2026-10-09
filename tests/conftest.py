"""Тесты ядра идут на живом Postgres: для сессии строится шаблонная БД (миграции + снимок),
для каждого теста из неё делается копия (CREATE DATABASE ... TEMPLATE). Нет БД: тесты пропускаются."""

import pytest

from dental.config import load_config
from dental.core.db import connect, database_url, with_dbname
from dental.core.loader import build_template, create_database, drop_database, unique_name
from dental.core.service import ClinicCore
from dental.synth.generate import build_snapshot

CFG = load_config()


@pytest.fixture(scope="session")
def admin_url():
    try:
        url = database_url()
        with connect(with_dbname(url, "postgres")):
            pass
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"нет доступа к Postgres ({type(e).__name__}); подними docker compose и задай .env")
    return url


@pytest.fixture(scope="session")
def template_db(admin_url):
    name = unique_name("dental_tpl")
    build_template(admin_url, name, build_snapshot(CFG))
    yield name
    drop_database(admin_url, name)


@pytest.fixture
def db_url(admin_url, template_db):
    name = unique_name("dental_t")
    create_database(admin_url, name, template=template_db)
    yield with_dbname(admin_url, name)
    drop_database(admin_url, name)


@pytest.fixture
def core(db_url):
    return ClinicCore(db_url)

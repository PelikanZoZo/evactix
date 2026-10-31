import os
import pathlib
import uuid

import psycopg
import pytest

FIX = pathlib.Path(__file__).parent / "fixtures"
ADMIN_DSN = os.getenv("TEST_ADMIN_DSN", "host=127.0.0.1 port=5432 user=evactix dbname=postgres")


@pytest.fixture
def fixture_bytes():
    return lambda name: (FIX / name).read_bytes()


@pytest.fixture
def db():
    """Свежая база на каждый тест: миграции + справочники. Нужен локальный Postgres 16 с TimescaleDB."""
    name = f"evx_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as a:
        a.execute(f'create database "{name}"')
    dsn = ADMIN_DSN.replace("dbname=postgres", f"dbname={name}")
    from evactix.catalog import sync_catalog
    with psycopg.connect(dsn, autocommit=True) as conn:
        migrate_test(conn, name)
        sync_catalog(conn)
        yield conn
    with psycopg.connect(ADMIN_DSN, autocommit=True) as a:
        a.execute(f'drop database "{name}" with (force)')


def migrate_test(conn, dbname):
    from importlib import resources
    conn.execute("create table if not exists schema_migrations (name text primary key, applied_at timestamptz not null default now())")
    for f in sorted(p for p in resources.files("evactix.migrations").iterdir() if p.name.endswith(".sql")):
        conn.execute(f.read_text(encoding="utf-8").replace("on database evactix", f'on database "{dbname}"'))
        conn.execute("insert into schema_migrations(name) values (%s)", (f.name,))


@pytest.fixture
def raw(tmp_path):
    from evactix.store import RawStore
    return RawStore(str(tmp_path / "raw"))

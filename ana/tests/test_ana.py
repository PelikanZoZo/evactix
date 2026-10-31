"""Контрактные тесты: Ана читает схему ядра (миграции из ../core) под ролью ana и ведёт своё состояние."""
import os
import pathlib
import uuid

import psycopg
import pytest

from ana import checks
from ana.config import Settings
from ana.main import Ana
from ana.state import State

ADMIN_DSN = os.getenv("TEST_ADMIN_DSN", "host=127.0.0.1 port=5432 user=evactix dbname=postgres")
CORE_MIGRATIONS = pathlib.Path(__file__).resolve().parents[2] / "core" / "evactix" / "migrations"


class FakeTG:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


@pytest.fixture
def core_db():
    name = f"ana_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as a:
        a.execute(f'create database "{name}"')
    dsn = ADMIN_DSN.replace("dbname=postgres", f"dbname={name}")
    with psycopg.connect(dsn, autocommit=True) as c:
        for f in sorted(CORE_MIGRATIONS.glob("*.sql")):
            c.execute(f.read_text(encoding="utf-8").replace("on database evactix", f'on database "{name}"'))
        c.execute("""insert into sources (id, name, source_class, base_confidence, license, max_age)
                     values ('cbr.fx', 'ЦБ РФ: курсы', 'open', 0.98, 'x', '2 days'),
                            ('metno.forecast', 'Met Norway', 'open', 0.8, 'x', '6 hours')""")
        yield name, c
    with psycopg.connect(ADMIN_DSN, autocommit=True) as a:
        a.execute(f'drop database "{name}" with (force)')


def make_ana(dbname, tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "edge", lambda url: [])
    s = Settings(db_host="127.0.0.1", db_name=dbname, db_user="ana", db_password="",
                 state_path=str(tmp_path / "ana.db"), disk_path=str(tmp_path), digest_hour_msk=0)
    tg = FakeTG()
    return Ana(s, tg=tg, state=State(s.state_path)), tg


def test_ana_reads_core_as_readonly_role(core_db):
    name, _ = core_db
    with psycopg.connect(f"host=127.0.0.1 port=5432 user=ana dbname={name}", autocommit=True) as a:
        assert checks.sources(a) == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            a.execute("insert into rejects (reason, content_hash) values ('x','y')")


def test_stale_alert_lifecycle(core_db, tmp_path, monkeypatch):
    name, c = core_db
    c.execute("""insert into collector_runs (collector, source_id, started_at, finished_at, status, error) values
                 ('cbr.fx','cbr.fx', now() - interval '5 days', now() - interval '5 days', 'ok', null),
                 ('cbr.fx','cbr.fx', now() - interval '1 hour', now() - interval '1 hour', 'error', 'ConnectError')""")
    ana, tg = make_ana(name, tmp_path, monkeypatch)
    ana.tick()
    assert any("нет свежих данных" in t and "ConnectError" in t for t in tg.sent)
    assert any(t.startswith("Ана · сводка") for t in tg.sent)          # сводка раз в сутки
    n = len(tg.sent)
    ana.tick()
    assert len(tg.sent) == n                                            # без повторов и без второй сводки
    c.execute("insert into collector_runs (collector, source_id, started_at, finished_at, status) values ('cbr.fx','cbr.fx', now(), now(), 'ok')")
    ana.tick()
    assert any(t.startswith("🟢 В норме") for t in tg.sent[n:])


def test_three_errors_and_rejects(core_db, tmp_path, monkeypatch):
    name, c = core_db
    c.execute("""insert into collector_runs (collector, source_id, started_at, finished_at, status, error)
                 select 'metno.forecast','metno.forecast', now() - (i || ' min')::interval, now(), s, e
                 from (values (1,'error','HTTP 503'),(2,'error','HTTP 503'),(3,'error','HTTP 503'),(30,'ok',null)) v(i,s,e)""")
    c.execute("insert into rejects (source_id, reason, content_hash) select 'cbr.fx', 'модель: ошибка', 'h' from generate_series(1,25)")
    ana, tg = make_ana(name, tmp_path, monkeypatch)
    ana.tick()
    text = "\n".join(tg.sent)
    assert "три ошибки подряд" in text and "25 отказов" in text


def test_core_db_down_keeps_data_alerts(core_db, tmp_path, monkeypatch):
    name, c = core_db
    c.execute("""insert into collector_runs (collector, source_id, started_at, finished_at, status) values
                 ('cbr.fx','cbr.fx', now() - interval '5 days', now() - interval '5 days', 'ok')""")
    ana, tg = make_ana(name, tmp_path, monkeypatch)
    ana.tick()
    assert ana.state.alert("stale:cbr.fx")["resolved_at"] is None
    object.__setattr__(ana.s, "db_port", 1)                              # база недоступна
    ana.tick()
    assert any("База ядра недоступна" in t for t in tg.sent)
    assert ana.state.alert("stale:cbr.fx")["resolved_at"] is None        # не закрыли ложным «в норме»
    object.__setattr__(ana.s, "db_port", 5432)
    ana.tick()
    assert any(t.startswith("🟢 В норме: База ядра недоступна") for t in tg.sent)

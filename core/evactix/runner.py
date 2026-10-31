"""Планировщик сборщиков: каждый по своему интервалу, каждый запуск — в журнале collector_runs."""
from __future__ import annotations

import logging
import signal
import sys
import time
import traceback
from datetime import datetime, timezone

import httpx
import psycopg

from .collectors import ALL
from .config import settings
from .store import Counts, RawStore, Store

log = logging.getLogger("evactix.runner")
_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


def run_once(conn: psycopg.Connection, col) -> Counts:
    started = datetime.now(timezone.utc)
    with conn.transaction():
        run_id = conn.execute(
            "insert into collector_runs (collector, source_id, started_at, status) values (%s,%s,%s,'running') returning id",
            (col.name, col.source_id, started)).fetchone()[0]
    try:
        batch = col.collect(conn)          # сеть — вне транзакции
        with conn.transaction():
            store = Store(conn, col.name, col.version, col.source_id)
            c = store.write_events(batch.events)
            c.add(store.write_plans(batch.plans))
        with conn.transaction():
            conn.execute(
                """update collector_runs set finished_at=now(), status='ok', written=%s, skipped=%s, rejected=%s,
                   error=%s where id=%s""",
                (c.written, c.skipped, c.rejected, "; ".join(sorted(set(c.reasons)))[:1000] or None, run_id))
        return c
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:1000]
        log.error("сборщик %s: %s\n%s", col.name, err, traceback.format_exc(limit=3))
        with conn.transaction():
            conn.execute("update collector_runs set finished_at=now(), status='error', error=%s where id=%s",
                         (err, run_id))
        raise


CONDUCTOR_FRESH = 600   # секунд: пульс Дирижёра свежее — работаем по его командам


def conductor_alive(conn) -> bool:
    """Есть ли живой Дирижёр. Нет схемы, прав или пульс старый — сборщики работают по своему расписанию."""
    try:
        r = conn.execute("select extract(epoch from now() - at) from jobs.heartbeat where id=1").fetchone()
    except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName, psycopg.errors.InsufficientPrivilege):
        return False
    return bool(r) and r[0] is not None and r[0] < CONDUCTOR_FRESH


def take_command(conn):
    with conn.transaction():
        return conn.execute("""update jobs.commands set status='running', taken_at=now()
                               where id = (select id from jobs.commands where target='collector' and status='pending'
                                           order by created_at limit 1 for update skip locked)
                               returning id, name""").fetchone()


def finish_command(conn, cmd_id: int, ok: bool, result: str) -> None:
    conn.execute("update jobs.commands set status=%s, finished_at=now(), result=%s where id=%s",
                 ("done" if ok else "failed", result[:500], cmd_id))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    s = settings()
    http = httpx.Client(timeout=httpx.Timeout(30.0, connect=15.0), follow_redirects=True,
                        headers={"User-Agent": s.user_agent})
    raw = RawStore(s.raw_dir)
    collectors = [cls(http, raw) for cls in ALL]
    now = time.monotonic()
    # Разносим первые запуски, чтобы не нагружать маленький сервер одновременно
    due = {c.name: now + i * 20 for i, c in enumerate(collectors)}
    conn = None
    while not _stop:
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(s.dsn(), autocommit=True)
            if conductor_alive(conn):
                # Режим по командам: когда собирать, решает Дирижёр (системные задания «Сбор: …»)
                by_name = {c.name: c for c in collectors}
                while not _stop and (cmd := take_command(conn)):
                    cmd_id, name = cmd
                    c = by_name.get(name)
                    if c is None:
                        finish_command(conn, cmd_id, False, f"сборщика {name} нет")
                        continue
                    try:
                        cnt = run_once(conn, c)
                        finish_command(conn, cmd_id, True, f"записано {cnt.written}, без изменений {cnt.skipped}, отклонено {cnt.rejected}")
                        log.info("%s (команда %s): записано %d", name, cmd_id, cnt.written)
                    except Exception as e:
                        finish_command(conn, cmd_id, False, f"{type(e).__name__}: {e}")
                    due[name] = time.monotonic() + c.interval.total_seconds()
                time.sleep(5)
                continue
            for c in collectors:
                if _stop or time.monotonic() < due[c.name]:
                    continue
                try:
                    cnt = run_once(conn, c)
                    log.info("%s: записано %d, без изменений %d, отклонено %d", c.name, cnt.written, cnt.skipped, cnt.rejected)
                    due[c.name] = time.monotonic() + c.interval.total_seconds()
                except Exception:
                    # Ошибка: повтор через 10 минут или через интервал, если он короче
                    due[c.name] = time.monotonic() + min(600, c.interval.total_seconds())
                    if conn.closed or conn.broken:
                        conn = None
                        break
        except psycopg.OperationalError as e:
            log.error("нет связи с базой: %s", e)
            conn = None
            time.sleep(15)
            continue
        time.sleep(5)
    if conn is not None:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

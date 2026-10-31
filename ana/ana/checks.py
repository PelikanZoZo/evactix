"""Проверки Аны. Каждая возвращает список находок; пустой список — всё в порядке.

Контракт с ядром — только чтение таблиц sources, collector_runs, rejects, events, plans.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

MSK = ZoneInfo("Europe/Moscow")


@dataclass
class Finding:
    key: str
    level: str      # 'warn' | 'crit'
    message: str


def disk(path: str, warn_pct: float, crit_pct: float) -> list[Finding]:
    u = shutil.disk_usage(path)
    pct = u.used / u.total * 100
    free = u.free / 2**30
    if pct >= crit_pct:
        return [Finding("disk", "crit", f"Диск заполнен на {pct:.0f}% (свободно {free:.1f} ГБ). Нужен переезд или вынос старого в S3.")]
    if pct >= warn_pct:
        return [Finding("disk", "warn", f"Диск заполнен на {pct:.0f}% (свободно {free:.1f} ГБ). Порог предупреждения {warn_pct:.0f}%.")]
    return []


def sources(conn) -> list[Finding]:
    out = []
    rows = conn.execute("""
        select s.id, s.name, s.max_age,
               (select max(finished_at) from collector_runs r where r.source_id = s.id and r.status = 'ok') as last_ok,
               (select array_agg(status order by started_at desc) from (
                    select status, started_at from collector_runs r where r.source_id = s.id
                    order by started_at desc limit 3) t) as last3,
               (select error from collector_runs r where r.source_id = s.id and r.status = 'error'
                    order by started_at desc limit 1) as last_err
        from sources s where s.max_age is not null""").fetchall()
    now = datetime.now(timezone.utc)
    for sid, name, max_age, last_ok, last3, last_err in rows:
        if last_ok is None:
            if last3:
                out.append(Finding(f"stale:{sid}", "crit", f"{name}: ни одного успешного сбора. Последняя ошибка: {last_err or '—'}"))
            continue
        if now - last_ok > max_age:
            hours = (now - last_ok).total_seconds() / 3600
            out.append(Finding(f"stale:{sid}", "crit",
                               f"{name}: нет свежих данных {hours:.0f} ч (допустимо {max_age}). Последняя ошибка: {last_err or '—'}"))
        elif last3 and len(last3) == 3 and all(x == "error" for x in last3):
            out.append(Finding(f"errors:{sid}", "warn", f"{name}: три ошибки подряд. {last_err or ''}"))
    return out


def rejects(conn, per_hour: int = 20) -> list[Finding]:
    rows = conn.execute("""select source_id, count(*), min(reason) from rejects
                           where recorded_at > now() - interval '1 hour' group by source_id having count(*) >= %s""",
                        (per_hour,)).fetchall()
    return [Finding(f"rejects:{sid}", "warn", f"{sid}: {n} отказов валидатора за час. Пример: {reason}")
            for sid, n, reason in rows]


def conductor(conn) -> list[Finding]:
    """Дирижёр: пульс, зависшие задания, серии ошибок, размножение заданий, застрявшая отправка."""
    import psycopg
    try:
        hb = conn.execute("select extract(epoch from now() - at) from jobs.heartbeat where id=1").fetchone()
    except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName, psycopg.errors.InsufficientPrivilege):
        return []                                        # Дирижёр ещё не выкачен (нет схемы или доступа к ней)
    out = []
    if not hb or hb[0] is None or hb[0] > 300:
        mins = (hb[0] / 60) if hb and hb[0] is not None else None
        out.append(Finding("conductor:pulse", "crit",
                           f"Дирижёр молчит {mins:.0f} мин. Задания не выполняются." if mins else "Дирижёр не подаёт пульс. Задания не выполняются."))
        return out
    stuck = conn.execute("""select count(*), min(id) from jobs.jobs where status='active' and exec_status='running'
                            and lock_until < now() - interval '5 minutes'""").fetchone()
    if stuck[0]:
        out.append(Finding("conductor:stuck", "warn", f"Зависших заданий: {stuck[0]} (например №{stuck[1]})."))
    failed = conn.execute("""select id, title from jobs.jobs where status='active' and exec_status='failed'
                             and fail_streak >= 3 order by id limit 3""").fetchall()
    for jid, title in failed:
        out.append(Finding(f"conductor:failed:{jid}", "warn", f"Задание №{jid} «{title}» не выполняется: три ошибки подряд."))
    spawn = conn.execute("""select count(*) from jobs.jobs where author_type in ('agent','service')
                            and created_at > now() - interval '1 hour'""").fetchone()[0]
    if spawn >= 10:
        out.append(Finding("conductor:spawn", "warn", f"За час агенты и сервисы поставили {spawn} заданий. Проверь, не размножаются ли."))
    stale = conn.execute("""select count(*) from jobs.outbox where status='pending'
                            and created_at < now() - interval '15 minutes'""").fetchone()[0]
    if stale:
        out.append(Finding("conductor:outbox", "warn", f"Сообщений застряло в отправке: {stale}."))
    return out


def edge(url: str) -> list[Finding]:
    try:
        r = httpx.get(url, timeout=5)
    except Exception as e:
        return [Finding("edge", "crit", f"Входная точка ядра не отвечает ({type(e).__name__})")]
    if r.status_code != 200:
        return [Finding("edge", "crit", f"Входная точка ядра отвечает {r.status_code}")]
    return []


def core_release(conn) -> str:
    """Версия ядра из его паспорта (system_info)."""
    try:
        rows = dict(conn.execute("select key, value from system_info").fetchall())
    except Exception:
        return "Evactix (версия неизвестна)"
    return f"{rows.get('product', 'Evactix')} v{rows.get('release', '?')} ({rows.get('commit', '?')[:7]})"


def digest(conn, disk_path: str) -> str:
    u = shutil.disk_usage(disk_path)
    rows = conn.execute("""
        select s.name,
               coalesce(sum(r.written) filter (where r.started_at > now() - interval '24 hours'), 0),
               count(*) filter (where r.status = 'error' and r.started_at > now() - interval '24 hours')
        from sources s left join collector_runs r on r.source_id = s.id
        group by s.name order by s.name""").fetchall()
    total_e = conn.execute("select count(*) from events").fetchone()[0]
    total_p = conn.execute("select count(*) from plans").fetchone()[0]
    rej = conn.execute("select count(*) from rejects where recorded_at > now() - interval '24 hours'").fetchone()[0]
    dbsize = conn.execute("select pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]
    sp = lambda n: f"{n:,}".replace(",", " ")
    lines = [f"Ана · сводка за сутки ({datetime.now(MSK):%d.%m %H:%M} МСК)", core_release(conn), ""]
    for name, written, errors in rows:
        lines.append(("⚠️ " if errors else "✅ ") + f"{name}: +{sp(written)}" + (f", ошибок {errors}" if errors else ""))
    lines += ["", f"Всего событий: {sp(total_e)}", f"Планов: {sp(total_p)}",
              f"Отказов валидатора за сутки: {rej}", f"База: {dbsize}",
              f"Диск: {u.used / u.total * 100:.0f}% (свободно {u.free / 2**30:.1f} ГБ)"]
    return "\n".join(lines)

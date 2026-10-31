"""Ана (Анализатор Надёжности и Аномалий): сторож ядра. Свои ключи, только чтение данных.

Проверяет каждые 5 минут: связь с базой, место на диске, свежесть каждого источника,
серии ошибок сборщиков, всплески отказов валидатора, входную точку.
Пишет в Telegram: новая тревога, напоминание раз в 6 часов, «вернулось в норму»,
и ежедневную сводку в 09:00 по Москве.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import psycopg
from psycopg.types.json import Jsonb

from .config import Settings, settings

log = logging.getLogger("evactix.ana")
MSK = ZoneInfo("Europe/Moscow")
CHECK_EVERY = 300
REMIND_EVERY = timedelta(hours=6)
_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


@dataclass
class Finding:
    key: str
    level: str      # 'warn' | 'crit'
    message: str


class Telegram:
    def __init__(self, s: Settings):
        self.token = s.telegram_token
        self.chat = s.telegram_chat
        self.http = httpx.Client(timeout=20)

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    def send(self, text: str) -> bool:
        if not self.enabled:
            log.info("telegram выключен: %s", text.splitlines()[0])
            return False
        try:
            r = self.http.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                               json={"chat_id": self.chat, "text": text[:4000], "disable_web_page_preview": True})
            if r.status_code != 200:
                log.error("telegram ответил %s", r.status_code)   # тело и адрес не логируем: в адресе токен
                return False
            return True
        except Exception as e:
            log.error("telegram недоступен: %s", type(e).__name__)
            return False


def check_disk(s: Settings) -> list[Finding]:
    u = shutil.disk_usage(s.disk_path)
    pct = u.used / u.total * 100
    if pct >= s.disk_crit_pct:
        return [Finding("disk", "crit", f"Диск заполнен на {pct:.0f}% (свободно {u.free / 2**30:.1f} ГБ). Нужен переезд или вынос старого в S3.")]
    if pct >= s.disk_warn_pct:
        return [Finding("disk", "warn", f"Диск заполнен на {pct:.0f}% (свободно {u.free / 2**30:.1f} ГБ). Порог предупреждения {s.disk_warn_pct:.0f}%.")]
    return []


def check_sources(conn) -> list[Finding]:
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
            if last3:   # запуски были, но ни одного успешного
                out.append(Finding(f"stale:{sid}", "crit", f"{name}: ни одного успешного сбора. Последняя ошибка: {last_err or '—'}"))
            continue
        if now - last_ok > max_age:
            hours = (now - last_ok).total_seconds() / 3600
            out.append(Finding(f"stale:{sid}", "crit", f"{name}: нет свежих данных {hours:.0f} ч (допустимо {max_age}). Последняя ошибка: {last_err or '—'}"))
        elif last3 and len(last3) == 3 and all(x == "error" for x in last3):
            out.append(Finding(f"errors:{sid}", "warn", f"{name}: три ошибки подряд. {last_err or ''}"))
    return out


def check_rejects(conn) -> list[Finding]:
    rows = conn.execute("""select source_id, count(*), min(reason) from rejects
                           where recorded_at > now() - interval '1 hour' group by source_id having count(*) >= 20""").fetchall()
    return [Finding(f"rejects:{sid}", "warn", f"{sid}: {n} отказов валидатора за час. Пример: {reason}") for sid, n, reason in rows]


def check_edge() -> list[Finding]:
    try:
        r = httpx.get("http://edge/health", timeout=5)
        if r.status_code == 200:
            return []
        return [Finding("edge", "crit", f"Входная точка отвечает {r.status_code}")]
    except Exception as e:
        return [Finding("edge", "crit", f"Входная точка не отвечает ({type(e).__name__})")]


def digest(conn, s: Settings) -> str:
    u = shutil.disk_usage(s.disk_path)
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
    lines = [f"Ана · сводка за сутки ({datetime.now(MSK):%d.%m %H:%M} МСК)", ""]
    for name, written, errors in rows:
        mark = "⚠️" if errors else "✅"
        lines.append(f"{mark} {name}: +{written}" + (f", ошибок {errors}" if errors else ""))
    lines += ["", f"Всего событий: {total_e:,}".replace(",", " "), f"Планов: {total_p:,}".replace(",", " "),
              f"Отказов валидатора за сутки: {rej}", f"База: {dbsize}",
              f"Диск: {u.used / u.total * 100:.0f}% (свободно {u.free / 2**30:.1f} ГБ)"]
    return "\n".join(lines)


class Ana:
    def __init__(self, s: Settings):
        self.s = s
        self.tg = Telegram(s)
        self.db_down_since: datetime | None = None

    def handle(self, conn, findings: list[Finding]) -> None:
        now = datetime.now(timezone.utc)
        active = {f.key: f for f in findings}
        for f in findings:
            row = conn.execute("select last_sent, resolved_at, level from ana_alerts where key=%s", (f.key,)).fetchone()
            icon = "🔴" if f.level == "crit" else "🟠"
            if row is None or row[1] is not None:
                if self.tg.send(f"{icon} {f.message}"):
                    sent = now
                else:
                    sent = None
                conn.execute("""insert into ana_alerts (key, level, message, first_seen, last_sent, resolved_at)
                                values (%s,%s,%s,now(),%s,null)
                                on conflict (key) do update set level=excluded.level, message=excluded.message,
                                  first_seen=now(), last_sent=excluded.last_sent, resolved_at=null""",
                             (f.key, f.level, f.message, sent))
            elif row[0] is None or now - row[0] > REMIND_EVERY or (row[2] != f.level and f.level == "crit"):
                if self.tg.send(f"{icon} Всё ещё: {f.message}"):
                    conn.execute("update ana_alerts set last_sent=now(), level=%s, message=%s where key=%s",
                                 (f.level, f.message, f.key))
        for key, msg in conn.execute("select key, message from ana_alerts where resolved_at is null").fetchall():
            if key not in active:
                self.tg.send(f"🟢 В норме: {msg.split('.')[0]}")
                conn.execute("update ana_alerts set resolved_at=now() where key=%s", (key,))

    def maybe_digest(self, conn) -> None:
        now = datetime.now(MSK)
        if now.hour < 9:
            return
        row = conn.execute("select value->>'date' from ana_state where key='digest'").fetchone()
        today = now.date().isoformat()
        if row and row[0] == today:
            return
        if self.tg.send(digest(conn, self.s)):
            conn.execute("""insert into ana_state (key, value, updated_at) values ('digest', %s, now())
                            on conflict (key) do update set value=excluded.value, updated_at=now()""",
                         (Jsonb({"date": today}),))

    def tick(self) -> None:
        try:
            with psycopg.connect(self.s.dsn(), autocommit=True, connect_timeout=10) as conn:
                if self.db_down_since is not None:
                    self.tg.send("🟢 База снова доступна")
                    self.db_down_since = None
                findings = check_disk(self.s) + check_sources(conn) + check_rejects(conn) + check_edge()
                self.handle(conn, findings)
                self.maybe_digest(conn)
        except psycopg.OperationalError as e:
            log.error("база недоступна: %s", type(e).__name__)
            now = datetime.now(timezone.utc)
            if self.db_down_since is None:
                self.db_down_since = now
                self.tg.send("🔴 База недоступна. Сборщики стоят, данные не пишутся.")
                disk = check_disk(self.s)
                if disk:
                    self.tg.send(f"🔴 {disk[0].message}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    s = settings()
    ana = Ana(s)
    ver = os.getenv("EVACTIX_VERSION", "dev")[:7]
    ana.tg.send(f"👋 Ана на связи. Версия ядра {ver}. Слежу за базой, диском и {len(__import__('evactix.catalog', fromlist=['SOURCES']).SOURCES)} источниками.")
    while not _stop:
        try:
            ana.tick()
        except Exception as e:
            log.exception("ошибка проверки: %s", e)
        for _ in range(CHECK_EVERY):
            if _stop:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())

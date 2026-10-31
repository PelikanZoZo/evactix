"""Ана (Анализатор Надёжности и Аномалий) — сторож рядом с ядром Evactix.

Отдельный проект: свой образ, свой compose-проект, своё состояние. К ядру — только ключ на чтение.
Каждые 5 минут: база ядра, диск, свежесть каждого источника, серии ошибок, всплески отказов, входная точка.
В Telegram: новая тревога, напоминание раз в 6 часов, «в норме», ежедневная сводка.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg

from . import __version__, checks
from .config import Settings
from .state import State
from .telegram import Telegram

log = logging.getLogger("ana")
REMIND_EVERY = timedelta(hours=6)
_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


class Ana:
    def __init__(self, s: Settings, tg: Telegram | None = None, state: State | None = None):
        self.s = s
        self.tg = tg or Telegram(s.telegram_token, s.telegram_chat, s.telegram_api)
        os.makedirs(os.path.dirname(s.state_path) or ".", exist_ok=True)
        self.state = state or State(s.state_path)

    def handle(self, findings: list[checks.Finding], keep: tuple[str, ...] = ()) -> None:
        """keep — префиксы тревог, которые нельзя закрывать в этом цикле (нет данных, чтобы судить)."""
        now = datetime.now(timezone.utc)
        active = {f.key for f in findings}
        for f in findings:
            a = self.state.alert(f.key)
            icon = "🔴" if f.level == "crit" else "🟠"
            if a is None or a["resolved_at"] is not None:
                self.state.open_alert(f.key, f.level, f.message, self.tg.send(f"{icon} {f.message}"))
            elif a["last_sent"] is None or now - a["last_sent"] > REMIND_EVERY or (a["level"] != f.level and f.level == "crit"):
                if self.tg.send(f"{icon} Всё ещё: {f.message}"):
                    self.state.touch_alert(f.key, f.level, f.message)
        for key, msg in self.state.active_alerts():
            if key not in active and not key.startswith(keep):
                self.tg.send(f"🟢 В норме: {msg.split('.')[0]}")
                self.state.resolve(key)

    def maybe_digest(self, conn) -> None:
        now = datetime.now(checks.MSK)
        if now.hour < self.s.digest_hour_msk:
            return
        today = now.date().isoformat()
        if self.state.get("digest_date") == today:
            return
        if self.tg.send(checks.digest(conn, self.s.disk_path)):
            self.state.set("digest_date", today)

    def tick(self) -> None:
        findings = checks.disk(self.s.disk_path, self.s.disk_warn_pct, self.s.disk_crit_pct) + checks.edge(self.s.edge_url)
        try:
            with psycopg.connect(self.s.dsn(), autocommit=True) as conn:
                findings += checks.sources(conn) + checks.rejects(conn) + checks.conductor(conn)
                self.handle(findings)
                self.maybe_digest(conn)
        except psycopg.OperationalError as e:
            log.error("база ядра недоступна: %s", type(e).__name__)
            findings.append(checks.Finding("core_db", "crit", "База ядра недоступна. Сборщики стоят, данные не пишутся."))
            # Тревоги по данным не закрываем: без связи с базой не знаем, исправились ли они
            self.handle(findings, keep=("stale:", "errors:", "rejects:", "conductor:"))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    s = Settings()
    ana = Ana(s)
    try:
        with psycopg.connect(s.dsn(), autocommit=True) as conn:
            core = checks.core_release(conn)
    except Exception:
        core = "Evactix (ядро пока недоступно)"
    ana.tg.send(f"👋 Ана v{__version__} ({s.commit}) на связи.\nСлежу за ядром {core}: база, диск, источники, Дирижёр, входная точка.")
    while not _stop:
        try:
            ana.tick()
        except Exception as e:
            log.exception("ошибка проверки: %s", e)
        for _ in range(s.check_every):
            if _stop:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())

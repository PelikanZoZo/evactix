"""Дирижёр Evactix: исполнитель заданий, отправщик и собеседник в Telegram.

Три потока, у каждого своё подключение к базе:
- планировщик: пульс, созревшие задания, запуски от планов;
- отправщик: очередь jobs.outbox → Telegram / webhook;
- Telegram: читает сообщения из разрешённых чатов, отвечает через агента.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from importlib import resources

import httpx
import psycopg
from psycopg.types.json import Jsonb

from . import __version__, agent, jobs, outbox
from .config import COLLECTORS, Settings
from .llm import DeepSeek, LLMError

log = logging.getLogger("conductor")
_stop = threading.Event()
SYSTEM = jobs.Author("system", "система", "system")


# --- запуск

def migrate(conn) -> list[str]:
    conn.execute("create table if not exists jobs.schema_migrations (name text primary key, applied_at timestamptz not null default now())")
    done = {r[0] for r in conn.execute("select name from jobs.schema_migrations")}
    applied = []
    for f in sorted(p for p in resources.files("conductor.migrations").iterdir() if p.name.endswith(".sql")):
        if f.name not in done:
            with conn.transaction():
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("insert into jobs.schema_migrations(name) values (%s)", (f.name,))
            applied.append(f.name)
    return applied


def ensure_owner(conn, s: Settings) -> int | None:
    if not s.owner_chat:
        return None
    r = conn.execute("""insert into jobs.recipients (kind, name, address, created_by) values ('telegram', 'владелец', %s, 'система')
                        on conflict (kind, address) do update set active=true returning id""", (s.owner_chat,)).fetchone()
    conn.execute("""insert into jobs.kv (key, value) values ('owner_recipient', %s)
                    on conflict (key) do update set value=excluded.value, updated_at=now()""", (Jsonb({"id": r[0]}),))
    return r[0]


def ensure_system_jobs(conn) -> list[int]:
    made = []
    for name, minutes in COLLECTORS.items():
        key = f"collect:{name}"
        if conn.execute("select 1 from jobs.jobs where system_key=%s", (key,)).fetchone():
            continue
        with conn.transaction():
            made.append(jobs.create(conn, {"title": f"Сбор: {name}", "action": "builtin", "handler": key,
                                           "every_minutes": minutes}, SYSTEM, system_key=key))
    return made


def heartbeat(conn) -> None:
    conn.execute("""insert into jobs.heartbeat (id, at, version) values (1, now(), %s)
                    on conflict (id) do update set at=now(), version=excluded.version""", (__version__,))


def owner_ids(conn) -> list[int]:
    r = conn.execute("select (value->>'id')::bigint from jobs.kv where key='owner_recipient'").fetchone()
    return [r[0]] if r and r[0] else []


# --- исполнение

def check_plan(conn, j: dict) -> bool:
    """Для заданий от плана: план исчез, отменён или исполнен — закрыть; перенесён — пересчитать. True — запуск не нужен."""
    p = jobs.plan_lookup(conn, j["plan_source"], j["plan_key"])
    if p is None:
        jobs.close(conn, j, "план исчез из ядра", "система")
        return True
    target, status, _code, fulfilled = p
    if status != "active" or fulfilled:
        jobs.close(conn, j, f"план {'исполнен' if fulfilled else jobs.STATUS_RU.get(status, status)} — задание не нужно", "система")
        return True
    if target != j["plan_target_at"]:
        nxt = target + jobs.timedelta(minutes=j["plan_offset_min"] or 0)
        jobs._set(conn, j, {"plan_target_at": target, "next_run_at": nxt, "lock_until": None, "exec_status": "scheduled"},
                  "update", "система (план перенесён)", "system")
        return nxt > jobs.now()
    return False


def execute(conn, j: dict, llm: DeepSeek, manual: bool = False) -> str:
    with conn.transaction():
        if j["schedule"] == "plan" and check_plan(conn, j):
            return "plan"
        if not manual and jobs.skip_late(conn, j):
            return "skipped"
    try:
        if j["action"] == "remind":
            ids = jobs.recipient_ids(conn, j)
            with conn.transaction():
                n = outbox.enqueue(conn, j["message"] or j["title"], ids, j["id"])
                state = jobs.finish(conn, j, True, f"отправлено получателям: {n}" if n else "некому отправить", n, manual)
        elif j["action"] == "do":
            send, text, trace, created = agent.run_do(llm, conn, j)
            fresh = jobs.get(conn, j["id"])
            with conn.transaction():
                n = outbox.enqueue(conn, text, jobs.recipient_ids(conn, j), j["id"]) if send else 0
                res = (f"написал получателям: {n}" if n else "писать нечего") + \
                      (f"; новых заданий: {len(created)}" if created else "") + \
                      (f"; шаги: {', '.join(trace[:12])}" if trace else "")
                if fresh["status"] != "active":       # агент сам закрыл задание
                    conn.execute("insert into jobs.runs (job_id, status, manual, result, sent) values (%s,'succeeded',%s,%s,%s)",
                                 (j["id"], manual, res, n))
                    conn.execute("update jobs.jobs set lock_until=null, exec_status='succeeded', run_count=run_count+1, last_run_at=now() where id=%s", (j["id"],))
                    state = "succeeded"
                else:
                    state = jobs.finish(conn, j, True, res, n, manual)
        elif j["action"] == "builtin" and (j["handler"] or "").startswith("collect:"):
            name = j["handler"].split(":", 1)[1]
            with conn.transaction():
                busy = conn.execute("select 1 from jobs.commands where target='collector' and name=%s and status in ('pending','running')",
                                    (name,)).fetchone()
                if not busy:
                    conn.execute("insert into jobs.commands (target, name, job_id) values ('collector', %s, %s)", (name, j["id"]))
                state = jobs.finish(conn, j, True, "команда сборщику" + (" уже в очереди" if busy else " поставлена"), 0, manual)
        else:
            raise jobs.JobError(f"неизвестное действие {j['action']} {j['handler']}")
    except (LLMError, jobs.JobError, httpx.HTTPError, psycopg.Error) as e:
        err = f"{type(e).__name__}: {e}"
        with conn.transaction():
            state = jobs.finish(conn, j, False, err, 0, manual)
            if state == "failed" and j["action"] != "builtin":
                outbox.enqueue(conn, f"⚠️ Задание №{j['id']} «{j['title']}» не выполнилось три раза подряд: {err}",
                               owner_ids(conn), j["id"])
    return state


def scheduler_tick(conn, llm: DeepSeek, limit: int = 20) -> int:
    heartbeat(conn)
    done = 0
    for _ in range(limit):
        with conn.transaction():
            j = jobs.claim(conn)
        if j is None:
            break
        state = execute(conn, j, llm)
        log.info("задание №%s «%s»: %s", j["id"], j["title"], state)
        done += 1
    return done


# --- Telegram

class TelegramPoller:
    def __init__(self, s: Settings, llm: DeepSeek, http: httpx.Client | None = None):
        self.s = s
        self.llm = llm
        self.http = http or httpx.Client(timeout=httpx.Timeout(40.0, connect=15.0))
        self.allowed = s.allowed()

    def poll_once(self, conn, timeout: int = 25) -> int:
        r = conn.execute("select (value->>'offset')::bigint from jobs.kv where key='tg_offset'").fetchone()
        offset = r[0] if r and r[0] else 0
        try:
            resp = self.http.get(f"{self.s.telegram_api}/bot{self.s.telegram_token}/getUpdates",
                                 params={"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'})
        except httpx.HTTPError as e:
            log.warning("Telegram недоступен: %s", type(e).__name__)
            return 0
        if resp.status_code != 200:
            log.warning("Telegram ответил %s", resp.status_code)
            return 0
        n = 0
        for u in resp.json().get("result", []):
            offset = max(offset, u["update_id"] + 1)
            conn.execute("""insert into jobs.kv (key, value) values ('tg_offset', %s)
                            on conflict (key) do update set value=excluded.value, updated_at=now()""",
                         (Jsonb({"offset": offset}),))
            m = u.get("message") or {}
            text = (m.get("text") or "").strip()
            chat_id = str((m.get("chat") or {}).get("id", ""))
            if not text or not chat_id:
                continue
            if chat_id not in self.allowed:
                log.info("сообщение из чата без доступа — пропущено")
                continue
            self.handle(conn, chat_id, (m.get("from") or {}).get("first_name") or "собеседник", text)
            n += 1
        return n

    def handle(self, conn, chat_id: str, name: str, text: str) -> str:
        conn.execute("insert into jobs.chat_log (chat_id, role, text) values (%s,'user',%s)", (chat_id, text[:4000]))
        if not self.llm.enabled:
            reply = "Я Дирижёр Evactix. Разговор словами включится, когда будет ключ DeepSeek."
        else:
            try:
                reply = agent.chat(self.llm, conn, chat_id, name, text, is_owner=(chat_id == self.s.owner_chat))
            except LLMError as e:
                reply = f"Сейчас не могу разобрать сообщение: {e}. Попробуй чуть позже."
        conn.execute("insert into jobs.chat_log (chat_id, role, text) values (%s,'assistant',%s)", (chat_id, reply[:4000]))
        outbox.enqueue_chat(conn, chat_id, reply)
        return reply


# --- потоки

def _loop(name: str, s: Settings, body, pause: float):
    conn = None
    while not _stop.is_set():
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(s.dsn(), autocommit=True)
            body(conn)
        except psycopg.OperationalError as e:
            log.error("%s: нет связи с базой: %s", name, type(e).__name__)
            conn = None
            _stop.wait(15)
            continue
        except Exception:
            log.exception("%s: ошибка", name)
            if conn is not None and conn.broken:
                conn = None
        _stop.wait(pause)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _stop.set())
    s = Settings()
    llm = DeepSeek(s.deepseek_key, s.deepseek_model)
    with psycopg.connect(s.dsn(), autocommit=True) as conn:
        applied = migrate(conn)
        owner = ensure_owner(conn, s)
        made = ensure_system_jobs(conn)
        heartbeat(conn)
        if owner:
            outbox.enqueue(conn, f"🎼 Дирижёр v{__version__} ({s.commit}) на связи. Задания, расписания и разговор словами"
                                 f"{'' if llm.enabled else ' (без DeepSeek)'}. Напиши, что напомнить или за чем следить.", [owner])
    log.info("Дирижёр v%s: миграции %s, системных заданий создано %d", __version__, applied or "—", len(made))
    sender = outbox.Sender(s.telegram_token, api_base=s.telegram_api)
    threads = [threading.Thread(target=_loop, args=("планировщик", s, lambda c: scheduler_tick(c, llm), s.tick_seconds), daemon=True),
               threading.Thread(target=_loop, args=("отправщик", s, lambda c: sender.send_pending(c), 3), daemon=True)]
    if s.poll_telegram and s.telegram_token:
        poller = TelegramPoller(s, llm)
        threads.append(threading.Thread(target=_loop, args=("telegram", s, lambda c: poller.poll_once(c, timeout=20), 1), daemon=True))
    for t in threads:
        t.start()
    while not _stop.is_set():
        _stop.wait(1)
    for t in threads:
        t.join(timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())

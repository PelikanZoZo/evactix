"""Задания Дирижёра: создание, правка, отмена, расписание, захват, завершение запуска.

Концепция — из заданий Киры: всё, что платформа делает без прямой просьбы, идёт через задания.
У задания есть автор и получатели; ничего не удаляется; есть журнал запусков и история «было → стало».
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from .cron import MSK, Cron, CronError, describe

MAX_ACTIVE = 300          # активных заданий всего
MIN_INTERVAL = 5          # минут
AGENT_NEW_PER_RUN = 3     # новых заданий агентом за один запуск
LATE_SKIP = timedelta(hours=6)
LOCK = timedelta(minutes=10)
RETRY = [timedelta(minutes=5), timedelta(minutes=10)]

STATUS_RU = {"active": "активно", "done": "выполнено", "cancelled": "отменено"}
EXEC_RU = {"scheduled": "ожидает", "running": "выполняется", "succeeded": "успешно", "failed": "ошибка",
           "retrying": "повтор", "skipped": "пропущено"}
ACTION_RU = {"remind": "напомнить", "do": "выполнить", "builtin": "встроенное"}

FIELDS = ["id", "title", "instruction", "message", "action", "handler", "author_type", "author_id", "author_name",
          "source", "parent_job_id", "schedule", "run_at", "cron", "interval_min", "plan_source", "plan_key",
          "plan_offset_min", "plan_target_at", "until_at", "max_runs", "next_run_at", "last_run_at", "run_count",
          "fail_streak", "exec_status", "last_result", "lock_until", "recipients", "status", "system_key", "version",
          "created_at", "updated_at", "cancelled_by", "cancelled_at"]
COLS = ", ".join(FIELDS)


class JobError(ValueError):
    pass


@dataclass(frozen=True)
class Author:
    type: str            # user | agent | ana | system | service
    name: str
    source: str          # telegram | console | agent | system | api
    id: str | None = None


def now() -> datetime:
    return datetime.now(timezone.utc)


def row_to_job(r) -> dict:
    return dict(zip(FIELDS, r))


def get(conn, job_id: int) -> dict | None:
    r = conn.execute(f"select {COLS} from jobs.jobs where id=%s", (job_id,)).fetchone()
    return row_to_job(r) if r else None


def _parse_time(v) -> datetime:
    if isinstance(v, datetime):
        dt = v
    else:
        s = str(v).strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                raise JobError(f"не понимаю время «{v}»")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)           # без пояса — московское время
    return dt.astimezone(timezone.utc)


def plan_lookup(conn, source: str, key: str):
    """Текущая версия плана в ядре: (target_at, status, code) или None."""
    r = conn.execute("""select target_at, status, value->>'code', fulfilled_by from public.plans_current
                        where source_id=%s and dedup_key=%s""", (source, key)).fetchone()
    return r


def next_run(j: dict, after: datetime) -> datetime | None:
    """Следующий запуск после after, с учётом ограничений. None — запусков больше нет."""
    if j["max_runs"] and j["run_count"] >= j["max_runs"]:
        return None
    s = j["schedule"]
    if s == "once":
        nxt = j["run_at"] if j["run_count"] == 0 else None
    elif s == "cron":
        nxt = Cron(j["cron"]).next_after(after)
    elif s == "interval":
        # первый запуск — сразу; дальше — через интервал от прошлого, но не в прошлом
        nxt = after if j["last_run_at"] is None else max(j["last_run_at"] + timedelta(minutes=j["interval_min"]), after)
    elif s == "plan":
        nxt = (j["plan_target_at"] + timedelta(minutes=j["plan_offset_min"] or 0)) if j["run_count"] == 0 else None
    else:
        nxt = None
    if nxt and j["until_at"] and nxt > j["until_at"]:
        return None
    return nxt


def schedule_text(j: dict) -> str:
    s = j["schedule"]
    if s == "once":
        base = f"разово {j['run_at'].astimezone(MSK):%d.%m.%Y %H:%M}"
    elif s == "cron":
        base = describe(j["cron"])
    elif s == "interval":
        m = j["interval_min"]
        base = f"каждые {m // 60} ч" if m % 60 == 0 else f"каждые {m} мин"
    else:
        off = j["plan_offset_min"] or 0
        when = "в срок плана" if off == 0 else (f"за {-off} мин до срока плана" if off < 0 else f"через {off} мин после срока плана")
        base = f"{when} {j['plan_key']}"
    extra = []
    if j["until_at"]:
        extra.append(f"до {j['until_at'].astimezone(MSK):%d.%m.%Y}")
    if j["max_runs"]:
        extra.append(f"не больше {j['max_runs']} раз")
    return ", ".join([base] + extra)


def recipient_ids(conn, j: dict) -> list[int]:
    rs = j["recipients"] or []
    if any(r.get("all") for r in rs):
        return [r[0] for r in conn.execute("select id from jobs.recipients where active order by id")]
    ids = [int(r["id"]) for r in rs if "id" in r]
    if not ids:
        return []
    return [r[0] for r in conn.execute("select id from jobs.recipients where active and id = any(%s) order by id", (ids,))]


def resolve_recipients(conn, to, author: Author) -> list[dict]:
    """to: ['me', 'all', 'Имя получателя', 3, ...] → [{"id": ..}] / [{"all": true}]."""
    if not to:
        to = ["me"]
    out = []
    for t in to:
        if t in ("all", "все"):
            return [{"all": True}]
        if isinstance(t, int) or (isinstance(t, str) and t.isdigit()):
            r = conn.execute("select id from jobs.recipients where id=%s and active", (int(t),)).fetchone()
        elif t in ("me", "мне", "я"):
            if author.source == "telegram" and author.id:
                r = conn.execute("select id from jobs.recipients where kind='telegram' and address=%s and active",
                                 (author.id,)).fetchone()
            else:
                r = conn.execute("select (value->>'id')::bigint from jobs.kv where key='owner_recipient'").fetchone()
        else:
            r = conn.execute("select id from jobs.recipients where active and lower(name)=lower(%s)", (str(t),)).fetchone()
        if not r or r[0] is None:
            raise JobError(f"получатель «{t}» не найден")
        out.append({"id": int(r[0])})
    return out


def _history(conn, job_id: int, version: int, action: str, by: str, source: str, diff: dict | None = None):
    conn.execute("insert into jobs.history (job_id, version, action, by_name, source, diff) values (%s,%s,%s,%s,%s,%s)",
                 (job_id, version, action, by, source, Jsonb(diff or {})))


def create(conn, spec: dict, author: Author, *, parent_job_id: int | None = None, system_key: str | None = None) -> int:
    """spec: title, action, message|instruction|handler, и одно из run_at | cron | every_minutes | plan; until, max_runs, to."""
    title = (spec.get("title") or "").strip()
    if not title or len(title) > 200:
        raise JobError("нужно короткое название задания")
    action = spec.get("action") or ("remind" if spec.get("message") else "do")
    if action not in ACTION_RU:
        raise JobError("действие — напомнить или выполнить")
    message = (spec.get("message") or "").strip() or None
    instruction = (spec.get("instruction") or "").strip() or None
    handler = spec.get("handler")
    if action == "remind" and not message:
        message = title
    if action == "do" and not instruction:
        raise JobError("для «выполнить» нужно, что именно сделать")
    if action == "builtin" and (author.type != "system" or not handler):
        raise JobError("встроенные задания ставит только система")

    j = {"run_at": None, "cron": None, "interval_min": None, "plan_source": None, "plan_key": None,
         "plan_offset_min": None, "plan_target_at": None, "run_count": 0, "last_run_at": None,
         "until_at": _parse_time(spec["until"]) if spec.get("until") else None,
         "max_runs": int(spec["max_runs"]) if spec.get("max_runs") else None}
    t = now()
    if spec.get("cron"):
        try:
            Cron(spec["cron"])
        except CronError as e:
            raise JobError(f"расписание: {e}")
        j.update(schedule="cron", cron=spec["cron"])
    elif spec.get("every_minutes"):
        m = int(spec["every_minutes"])
        if m < MIN_INTERVAL:
            raise JobError(f"интервал не меньше {MIN_INTERVAL} минут")
        j.update(schedule="interval", interval_min=m)
    elif spec.get("plan"):
        p = spec["plan"]
        found = plan_lookup(conn, p.get("source"), p.get("key"))
        if not found:
            raise JobError("план не найден в ядре")
        j.update(schedule="plan", plan_source=p["source"], plan_key=p["key"],
                 plan_offset_min=int(p.get("offset_min") or 0), plan_target_at=found[0])
    elif spec.get("run_at"):
        j.update(schedule="once", run_at=_parse_time(spec["run_at"]))
        if j["run_at"] < t - timedelta(minutes=1):
            raise JobError("это время уже прошло")
    else:
        raise JobError("когда выполнять: разово, по расписанию, каждые N минут или от плана")

    active = conn.execute("select count(*) from jobs.jobs where status='active'").fetchone()[0]
    if active >= MAX_ACTIVE:
        raise JobError(f"активных заданий уже {MAX_ACTIVE} — это предел")
    if parent_job_id is not None:
        # текущий запуск начался, когда задание захватили: lock_until - LOCK
        made = conn.execute("""select count(*) from jobs.jobs c, jobs.jobs p where p.id=%s and c.parent_job_id=p.id
                               and c.created_at >= coalesce(p.lock_until - %s, now() - interval '1 hour')""",
                            (parent_job_id, LOCK)).fetchone()[0]
        if made >= AGENT_NEW_PER_RUN:
            raise JobError(f"лимит: не больше {AGENT_NEW_PER_RUN} новых заданий за один запуск")

    recipients = [] if action == "builtin" else resolve_recipients(conn, spec.get("to"), author)
    nxt = next_run(j, t)
    if nxt is None:
        raise JobError("по такому расписанию запусков не будет")
    row = conn.execute(
        """insert into jobs.jobs (title, instruction, message, action, handler, author_type, author_id, author_name,
             source, parent_job_id, schedule, run_at, cron, interval_min, plan_source, plan_key, plan_offset_min,
             plan_target_at, until_at, max_runs, next_run_at, recipients, system_key)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
        (title, instruction, message, action, handler, author.type, author.id, author.name, author.source,
         parent_job_id, j["schedule"], j["run_at"], j["cron"], j["interval_min"], j["plan_source"], j["plan_key"],
         j["plan_offset_min"], j["plan_target_at"], j["until_at"], j["max_runs"], nxt, Jsonb(recipients),
         system_key)).fetchone()
    _history(conn, row[0], 1, "create", author.name, author.source)
    return row[0]


def _set(conn, j: dict, changes: dict, action: str, by: str, source: str) -> None:
    diff = {k: [_plain(j.get(k)), _plain(v)] for k, v in changes.items() if j.get(k) != v}
    if not diff and action == "update":
        return
    sets = ", ".join(f"{k}=%s" for k in changes)
    vals = [Jsonb(v) if k == "recipients" else v for k, v in changes.items()]
    conn.execute(f"update jobs.jobs set {sets}, version=version+1, updated_at=now() where id=%s", (*vals, j["id"]))
    _history(conn, j["id"], j["version"] + 1, action, by, source, diff)


def _plain(v):
    if isinstance(v, datetime):
        return v.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    return v


def cancel(conn, job_id: int, by: str, source: str) -> dict:
    j = get(conn, job_id)
    if not j:
        raise JobError(f"задания №{job_id} нет")
    if j["status"] != "active":
        raise JobError(f"задание №{job_id} уже {STATUS_RU[j['status']]}")
    _set(conn, j, {"status": "cancelled", "cancelled_by": by, "cancelled_at": now(), "next_run_at": None},
         "cancel", by, source)
    return get(conn, job_id)


def restore(conn, job_id: int, by: str, source: str) -> dict:
    j = get(conn, job_id)
    if not j or j["status"] != "cancelled":
        raise JobError("вернуть можно только отменённое задание")
    nxt = next_run(j, now())
    if nxt is None:
        raise JobError("по расписанию запусков уже не будет")
    _set(conn, j, {"status": "active", "cancelled_by": None, "cancelled_at": None, "next_run_at": nxt,
                   "exec_status": "scheduled", "fail_streak": 0}, "restore", by, source)
    return get(conn, job_id)


def close(conn, j: dict, result: str, by: str, source: str = "system") -> None:
    _set(conn, j, {"status": "done", "next_run_at": None, "last_result": result, "lock_until": None},
         "close", by, source)


def update(conn, job_id: int, changes: dict, by: str, source: str) -> dict:
    allowed = {"title", "message", "instruction", "cron", "interval_min", "until_at", "max_runs", "recipients", "status"}
    bad = set(changes) - allowed
    if bad:
        raise JobError(f"нельзя менять: {', '.join(sorted(bad))}")
    j = get(conn, job_id)
    if not j:
        raise JobError(f"задания №{job_id} нет")
    if "interval_min" in changes and int(changes["interval_min"]) < MIN_INTERVAL:
        raise JobError(f"интервал не меньше {MIN_INTERVAL} минут")
    if "cron" in changes:
        Cron(changes["cron"])
    merged = {**j, **changes}
    if changes.get("status") == "done":
        changes["next_run_at"] = None
    elif {"cron", "interval_min", "until_at", "max_runs"} & set(changes):
        changes["next_run_at"] = next_run(merged, now())
    _set(conn, j, changes, "update", by, source)
    return get(conn, job_id)


# --- исполнение

def claim(conn, job_id: int | None = None) -> dict | None:
    """Атомарно забирает одно созревшее задание (или указанное — для «выполнить сейчас»)."""
    cond = "id=%s" if job_id else "next_run_at <= now()"
    args = (job_id,) if job_id else ()
    r = conn.execute(f"""update jobs.jobs set lock_until = now() + %s, exec_status='running'
                         where id = (select id from jobs.jobs where status='active' and {cond}
                                     and (lock_until is null or lock_until < now())
                                     order by next_run_at limit 1 for update skip locked)
                         returning {COLS}""", (LOCK, *args)).fetchone()
    return row_to_job(r) if r else None


def skip_late(conn, j: dict) -> bool:
    """Регулярное задание опоздало больше чем на 6 часов — не догоняем, ставим следующий."""
    if j["schedule"] not in ("cron", "interval") or not j["next_run_at"]:
        return False
    t = now()
    if t - j["next_run_at"] <= LATE_SKIP:
        return False
    conn.execute("insert into jobs.runs (job_id, status, result, finished_at) values (%s,'skipped',%s,now())",
                 (j["id"], f"опоздание {(t - j['next_run_at']).total_seconds() / 3600:.0f} ч — пропущено"))
    nxt = next_run({**j, "last_run_at": t}, t)
    conn.execute("""update jobs.jobs set exec_status='skipped', lock_until=null, next_run_at=%s,
                    status = case when %s::timestamptz is null then 'done' else status end where id=%s""",
                 (nxt, nxt, j["id"]))
    return True


def finish(conn, j: dict, ok: bool, result: str, sent: int = 0, manual: bool = False) -> str:
    """Записывает запуск, считает следующий. Возвращает новое состояние исполнения."""
    t = now()
    conn.execute("""insert into jobs.runs (job_id, status, manual, result, sent, started_at, finished_at)
                    values (%s,%s,%s,%s,%s,%s,now())""",
                 (j["id"], "succeeded" if ok else "failed", manual, (result or "")[:2000], sent, t))
    if ok:
        done = {**j, "run_count": j["run_count"] + 1, "last_run_at": t}
        nxt = j["next_run_at"] if manual and j["schedule"] != "once" else next_run(done, t)
        if manual and j["schedule"] == "once":
            nxt = None
        status = "active" if nxt else "done"
        conn.execute("""update jobs.jobs set run_count=run_count+1, last_run_at=%s, fail_streak=0, exec_status='succeeded',
                        last_result=%s, lock_until=null, next_run_at=%s, status=%s, updated_at=now() where id=%s""",
                     (t, (result or "")[:2000], nxt, status, j["id"]))
        return "succeeded"
    streak = j["fail_streak"] + 1
    if streak <= len(RETRY):
        conn.execute("""update jobs.jobs set fail_streak=%s, exec_status='retrying', last_result=%s, lock_until=null,
                        next_run_at=%s, updated_at=now() where id=%s""",
                     (streak, (result or "")[:2000], t + RETRY[streak - 1], j["id"]))
        return "retrying"
    nxt = next_run({**j, "last_run_at": t, "run_count": j["run_count"] + (0 if j["schedule"] == "once" else 1)}, t) \
        if j["schedule"] != "once" else None
    conn.execute("""update jobs.jobs set fail_streak=%s, exec_status='failed', last_result=%s, lock_until=null,
                    next_run_at=%s, status=%s, updated_at=now() where id=%s""",
                 (streak, (result or "")[:2000], nxt, "active" if nxt else "done", j["id"]))
    return "failed"


def brief(j: dict) -> str:
    """Строка для списков: «№12 Курс доллара — каждый день в 09:00 · активно»."""
    return f"№{j['id']} {j['title']} — {schedule_text(j)} · {STATUS_RU[j['status']]}"


def active_list(conn, limit: int = 30) -> list[dict]:
    return [row_to_job(r) for r in conn.execute(
        f"select {COLS} from jobs.jobs where status='active' and action <> 'builtin' order by id limit %s", (limit,))]


def to_json(j: dict) -> str:
    return json.dumps({k: _plain(v) for k, v in j.items()}, ensure_ascii=False, default=str)

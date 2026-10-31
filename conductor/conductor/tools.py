"""Инструменты агента. Данные ядра — только чтение и с лимитами. Задания — через jobs с проверками."""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg

from . import jobs
from .cron import MSK

MAX_ROWS = 200


def _t(v):
    if isinstance(v, datetime):
        return v.astimezone(MSK).strftime("%Y-%m-%d %H:%M")
    return v


def _rows(cur) -> list[dict]:
    names = [d.name for d in cur.description]
    return [{n: _t(v) for n, v in zip(names, r)} for r in cur.fetchall()]


def _int(v, default, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


# --- чтение ядра

def metrics_catalog(conn, **_):
    return {"metrics": _rows(conn.execute("select id, value_type, unit, description from public.metrics order by id")),
            "sources": _rows(conn.execute("select id, name, source_class, license from public.sources order by id")),
            "points": [r[0] for r in conn.execute("select id from public.entities where type='point' order by id")]}


def latest(conn, metric: str, subject: str | None = None, limit: int = 30, **_):
    limit = _int(limit, 30, 1, MAX_ROWS)
    sql = """select distinct on (subject) subject, num, value, occurred_at, valid_from, kind
             from public.events where metric=%s and occurred_at <= now() + interval '1 day'"""
    args = [metric]
    if subject:
        sql += " and subject @> %s::jsonb"
        args.append(json.dumps([{"id": subject}]))
    sql += " order by subject, occurred_at desc, recorded_at desc limit %s"
    args.append(limit)
    return _rows(conn.execute(sql, args))


def series(conn, metric: str, subject: str | None = None, hours_back: int = 168, hours_ahead: int = 0, limit: int = 200, **_):
    hb, ha, limit = _int(hours_back, 168, 1, 24 * 800), _int(hours_ahead, 0, 0, 24 * 400), _int(limit, 200, 1, MAX_ROWS)
    sql = """select distinct on (occurred_at) occurred_at, num, kind from public.events
             where metric=%s and occurred_at between now() - make_interval(hours => %s) and now() + make_interval(hours => %s)"""
    args = [metric, hb, ha]
    if subject:
        sql += " and subject @> %s::jsonb"
        args.append(json.dumps([{"id": subject}]))
    sql += " order by occurred_at, recorded_at desc limit %s"
    args.append(limit)
    return _rows(conn.execute(sql, args))


def recent_events(conn, type_prefix: str | None = None, hours_back: int = 24, limit: int = 50, **_):
    hb, limit = _int(hours_back, 24, 1, 24 * 30), _int(limit, 50, 1, MAX_ROWS)
    sql = """select type, occurred_at, lat, lon, region, subject, num, value, payload, source_id
             from public.events where recorded_at > now() - make_interval(hours => %s)"""
    args: list = [hb]
    if type_prefix:
        sql += " and type like %s"
        args.append(type_prefix.replace("%", "") + "%")
    sql += " order by occurred_at desc limit %s"
    args.append(limit)
    return _rows(conn.execute(sql, args))


def upcoming_plans(conn, days_ahead: int = 14, subject: str | None = None, limit: int = 50, **_):
    d, limit = _int(days_ahead, 14, 1, 400), _int(limit, 50, 1, MAX_ROWS)
    sql = """select source_id, dedup_key, parameter, metric, value, target_at, status, subject
             from public.plans_current where target_at between now() and now() + make_interval(days => %s)"""
    args: list = [d]
    if subject:
        sql += " and subject @> %s::jsonb"
        args.append(json.dumps([{"id": subject}]))
    sql += " order by target_at limit %s"
    args.append(limit)
    return _rows(conn.execute(sql, args))


def sources_status(conn, **_):
    return _rows(conn.execute("""
        select s.id, s.name,
               (select max(finished_at) from public.collector_runs r where r.source_id=s.id and r.status='ok') as last_ok,
               (select count(*) from public.events e where e.source_id=s.id and e.recorded_at > now() - interval '24 hours') as events_24h
        from public.sources s order by s.id"""))


READ_TOOLS = {
    "metrics_catalog": (metrics_catalog, "Справочник: какие метрики, источники и опорные точки есть в ядре", {}),
    "latest": (latest, "Последнее значение метрики по каждому субъекту (например курс каждой валюты)",
               {"metric": {"type": "string"}, "subject": {"type": "string", "description": "например currency:USD, point:moscow"},
                "limit": {"type": "integer"}}),
    "series": (series, "Ряд значений метрики за период (прошлое и/или прогноз вперёд)",
               {"metric": {"type": "string"}, "subject": {"type": "string"}, "hours_back": {"type": "integer"},
                "hours_ahead": {"type": "integer"}, "limit": {"type": "integer"}}),
    "recent_events": (recent_events, "Свежие события по типу: hazard.alert, quake.detected, fx.rate.published…",
                      {"type_prefix": {"type": "string"}, "hours_back": {"type": "integer"}, "limit": {"type": "integer"}}),
    "upcoming_plans": (upcoming_plans, "Планы с плановым сроком в ближайшие дни (календарь, объявленные изменения)",
                       {"days_ahead": {"type": "integer"}, "subject": {"type": "string"}, "limit": {"type": "integer"}}),
    "sources_status": (sources_status, "Состояние источников: когда собирали, сколько событий за сутки", {}),
}

JOB_SPEC = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "короткое название"},
        "action": {"type": "string", "enum": ["remind", "do"], "description": "remind — отправить готовый текст; do — проверить данные и написать по делу"},
        "message": {"type": "string", "description": "для remind: текст, который уйдёт дословно"},
        "instruction": {"type": "string", "description": "для do: что проверить и когда писать"},
        "run_at": {"type": "string", "description": "разово: 'YYYY-MM-DD HH:MM' по Москве"},
        "cron": {"type": "string", "description": "регулярно: cron из 5 полей по Москве, например '0 11 * * 1' — по понедельникам в 11:00"},
        "every_minutes": {"type": "integer", "description": "каждые N минут (не меньше 5)"},
        "plan": {"type": "object", "properties": {"source": {"type": "string"}, "key": {"type": "string"},
                                                  "offset_min": {"type": "integer"}},
                 "description": "от плана ядра: источник, ключ плана, смещение в минутах (минус — до срока)"},
        "until": {"type": "string", "description": "до даты 'YYYY-MM-DD HH:MM'"},
        "max_runs": {"type": "integer"},
        "to": {"type": "array", "items": {"type": "string"}, "description": "получатели: 'me', 'all' или имена"},
    },
    "required": ["title"],
}


def tool_schemas(names: list[str], extra: dict) -> list[dict]:
    out = []
    for n in names:
        if n in READ_TOOLS:
            _, desc, props = READ_TOOLS[n]
            params = {"type": "object", "properties": props}
        else:
            desc, params = extra[n]
        out.append({"type": "function", "function": {"name": n, "description": desc, "parameters": params}})
    return out


@dataclass
class JobTools:
    """Инструменты заданий для конкретного разговора или запуска."""
    conn: object
    author: jobs.Author
    parent_job_id: int | None = None      # для автономного запуска: лимит новых заданий
    self_job_id: int | None = None
    is_owner: bool = False
    created: list[int] = field(default_factory=list)

    def job_create(self, **spec):
        jid = jobs.create(self.conn, spec, self.author, parent_job_id=self.parent_job_id)
        self.created.append(jid)
        j = jobs.get(self.conn, jid)
        return {"ok": True, "id": jid, "schedule": jobs.schedule_text(j),
                "next_run": _t(j["next_run_at"])}

    def job_cancel(self, job_id: int, **_):
        j = jobs.cancel(self.conn, int(job_id), self.author.name, self.author.source)
        return {"ok": True, "id": j["id"], "status": jobs.STATUS_RU[j["status"]]}

    def job_list(self, **_):
        return [{"id": j["id"], "title": j["title"], "action": jobs.ACTION_RU[j["action"]],
                 "schedule": jobs.schedule_text(j), "next_run": _t(j["next_run_at"]), "author": j["author_name"]}
                for j in jobs.active_list(self.conn)]

    def job_close_self(self, result: str = "", **_):
        if not self.self_job_id:
            raise jobs.JobError("закрыть можно только своё задание")
        j = jobs.get(self.conn, self.self_job_id)
        jobs.close(self.conn, j, result or "закрыто агентом: больше не нужно", f"агент (задание №{self.self_job_id})", "agent")
        return {"ok": True}

    def recipient_add(self, kind: str, name: str, address: str, **_):
        if not self.is_owner:
            raise jobs.JobError("добавлять получателей может только владелец")
        if kind not in ("telegram", "webhook"):
            raise jobs.JobError("канал: telegram или webhook")
        if kind == "webhook" and not address.startswith("https://"):
            raise jobs.JobError("webhook — только https://")
        secret = secrets.token_hex(24) if kind == "webhook" else None
        r = self.conn.execute("""insert into jobs.recipients (kind, name, address, secret, created_by) values (%s,%s,%s,%s,%s)
                                 on conflict (kind, address) do update set name=excluded.name, active=true
                                 returning id""", (kind, name, address, secret, self.author.name)).fetchone()
        out = {"ok": True, "id": r[0], "name": name}
        if secret:
            out["secret"] = secret
            out["note"] = "Секрет для проверки подписи X-Evactix-Signature: HMAC-SHA256(secret, timestamp + '.' + body)"
        return out

    def recipients(self, **_):
        return _rows(self.conn.execute("select id, kind, name, active from jobs.recipients order by id"))

    SCHEMAS = {
        "job_create": ("Поставить задание", JOB_SPEC),
        "job_cancel": ("Отменить задание по номеру", {"type": "object", "properties": {"job_id": {"type": "integer"}}, "required": ["job_id"]}),
        "job_list": ("Список активных заданий", {"type": "object", "properties": {}}),
        "job_close_self": ("Закрыть текущее задание, если оно больше не нужно", {"type": "object", "properties": {"result": {"type": "string"}}}),
        "recipient_add": ("Добавить получателя (только владелец)", {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["telegram", "webhook"]}, "name": {"type": "string"},
            "address": {"type": "string", "description": "chat_id для telegram или https-адрес для webhook"}},
            "required": ["kind", "name", "address"]}),
        "recipients": ("Список получателей", {"type": "object", "properties": {}}),
    }


def call(conn, jt: JobTools | None, name: str, args_json: str) -> str:
    try:
        args = json.loads(args_json or "{}")
        if not isinstance(args, dict):
            args = {}
    except json.JSONDecodeError:
        return json.dumps({"error": "аргументы не JSON"}, ensure_ascii=False)
    try:
        if name in READ_TOOLS:
            res = READ_TOOLS[name][0](conn, **args)
        elif jt is not None and name in JobTools.SCHEMAS:
            with conn.transaction():
                res = getattr(jt, name)(**args)
        else:
            res = {"error": f"нет инструмента {name}"}
    except jobs.JobError as e:
        res = {"error": str(e)}
    except TypeError as e:
        res = {"error": f"аргументы: {e}"}
    except psycopg.Error as e:
        res = {"error": f"запрос не выполнен: {type(e).__name__}"}
    out = json.dumps(res, ensure_ascii=False, default=str)
    return out[:12000]

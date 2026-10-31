import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
import psycopg
import pytest

from conductor import jobs, outbox, service
from conductor.cron import MSK, Cron, describe
from conductor.jobs import Author, JobError

from .conftest import tool_call

USER = Author("user", "Денис (Telegram)", "telegram", "1001")


def due(conn, jid):
    conn.execute("update jobs.jobs set next_run_at = now() - interval '1 minute' where id=%s", (jid,))


def tick(conn, llm):
    return service.scheduler_tick(conn, llm)


# ---------- расписания

def test_cron_next_and_words():
    sat = datetime(2026, 10, 31, 20, 0, tzinfo=timezone.utc)                # суббота, 23:00 МСК
    assert Cron("0 11 * * 1").next_after(sat) == datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    assert Cron("0 9 * * 1-5").next_after(sat) == datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    assert Cron("0 10 1 * *").next_after(sat) == datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)
    assert describe("0 11 * * 1") == "по понедельникам в 11:00"
    assert describe("0 9 * * 1-5") == "по будням в 09:00"
    with pytest.raises(ValueError):
        Cron("61 * * * *")


# ---------- создание и правила

def test_create_rules(conn, settings):
    service.ensure_owner(conn, settings)
    jid = jobs.create(conn, {"title": "Позвонить", "action": "remind", "message": "Позвонить Иванову",
                             "run_at": (datetime.now(MSK) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")}, USER)
    j = jobs.get(conn, jid)
    assert j["schedule"] == "once" and j["author_name"] == "Денис (Telegram)" and j["recipients"] == [{"id": 1}]
    with pytest.raises(JobError, match="не меньше 5"):
        jobs.create(conn, {"title": "x", "message": "x", "every_minutes": 2}, USER)
    with pytest.raises(JobError, match="прошло"):
        jobs.create(conn, {"title": "x", "message": "x", "run_at": "2020-01-01 10:00"}, USER)
    with pytest.raises(JobError, match="что именно"):
        jobs.create(conn, {"title": "x", "action": "do", "cron": "0 9 * * *"}, USER)
    with pytest.raises(JobError, match="получатель"):
        jobs.create(conn, {"title": "x", "message": "x", "cron": "0 9 * * *", "to": ["Петя"]}, USER)
    with pytest.raises(JobError, match="встроенные"):
        jobs.create(conn, {"title": "x", "action": "builtin", "handler": "collect:x", "every_minutes": 10}, USER)
    assert conn.execute("select action from jobs.history where job_id=%s", (jid,)).fetchone()[0] == "create"


def test_cancel_restore_history(conn, settings):
    service.ensure_owner(conn, settings)
    jid = jobs.create(conn, {"title": "Отчёт", "message": "Отчёт", "cron": "0 14 * * 2"}, USER)
    jobs.cancel(conn, jid, "Гость (Telegram)", "telegram")
    j = jobs.get(conn, jid)
    assert j["status"] == "cancelled" and j["cancelled_by"] == "Гость (Telegram)" and j["next_run_at"] is None
    jobs.restore(conn, jid, "владелец", "console")
    assert jobs.get(conn, jid)["status"] == "active"
    jobs.update(conn, jid, {"cron": "0 15 * * 2"}, "владелец", "console")
    diff = conn.execute("select diff from jobs.history where job_id=%s and action='update'", (jid,)).fetchone()[0]
    assert diff["cron"] == ["0 14 * * 2", "0 15 * * 2"]
    assert [r[0] for r in conn.execute("select action from jobs.history where job_id=%s order by version", (jid,))] == \
        ["create", "cancel", "restore", "update"]


def test_active_limit(conn, settings, monkeypatch):
    service.ensure_owner(conn, settings)
    monkeypatch.setattr(jobs, "MAX_ACTIVE", 2)
    jobs.create(conn, {"title": "a", "message": "a", "cron": "0 9 * * *"}, USER)
    jobs.create(conn, {"title": "b", "message": "b", "cron": "0 9 * * *"}, USER)
    with pytest.raises(JobError, match="предел"):
        jobs.create(conn, {"title": "c", "message": "c", "cron": "0 9 * * *"}, USER)


# ---------- исполнение

def test_remind_once_and_send(conn, settings, make_llm):
    service.ensure_owner(conn, settings)
    llm, _ = make_llm([])
    jid = jobs.create(conn, {"title": "Позвонить", "message": "Позвонить Иванову",
                             "run_at": (datetime.now(MSK) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")}, USER)
    due(conn, jid)
    assert tick(conn, llm) == 1
    j = jobs.get(conn, jid)
    assert j["status"] == "done" and j["exec_status"] == "succeeded" and j["run_count"] == 1
    assert conn.execute("select text, status from jobs.outbox").fetchone() == ("Позвонить Иванову", "pending")
    got = []
    tg = httpx.Client(transport=httpx.MockTransport(lambda r: got.append(json.loads(r.content)) or httpx.Response(200, json={"ok": True})))
    assert outbox.Sender("TOKEN", tg).send_pending(conn) == 1
    assert got == [{"chat_id": "1001", "text": "Позвонить Иванову", "disable_web_page_preview": True}]
    assert conn.execute("select status from jobs.outbox").fetchone()[0] == "sent"


def test_retries_then_failed_and_owner_told(conn, settings, make_llm):
    service.ensure_owner(conn, settings)
    jid = jobs.create(conn, {"title": "Сводка", "action": "do", "instruction": "проверь курс", "cron": "0 9 * * *"}, USER)
    llm, _ = make_llm([500, 500, 500])
    states = []
    for _ in range(3):
        due(conn, jid)
        tick(conn, llm)
        j = jobs.get(conn, jid)
        states.append(j["exec_status"])
    assert states == ["retrying", "retrying", "failed"]
    assert j["status"] == "active" and j["next_run_at"] > datetime.now(timezone.utc)   # регулярное — дальше по расписанию
    assert "три раза подряд" in conn.execute("select text from jobs.outbox order by id desc limit 1").fetchone()[0]


def test_late_regular_skipped(conn, settings, make_llm):
    service.ensure_owner(conn, settings)
    llm, _ = make_llm([])
    jid = jobs.create(conn, {"title": "Пинг", "message": "пинг", "every_minutes": 60}, USER)
    conn.execute("update jobs.jobs set next_run_at = now() - interval '7 hours' where id=%s", (jid,))
    tick(conn, llm)
    j = jobs.get(conn, jid)
    assert j["exec_status"] == "skipped" and j["run_count"] == 0 and j["next_run_at"] > datetime.now(timezone.utc)
    assert conn.execute("select count(*) from jobs.outbox").fetchone()[0] == 0


def test_claim_is_exclusive(conn, settings, dbname):
    service.ensure_owner(conn, settings)
    jid = jobs.create(conn, {"title": "Одно", "message": "одно", "cron": "0 9 * * *"}, USER)
    due(conn, jid)
    with psycopg.connect(f"host=127.0.0.1 port=5432 user=conductor dbname={dbname}", autocommit=True) as other:
        a = jobs.claim(conn)
        b = jobs.claim(other)
    assert a["id"] == jid and b is None


# ---------- от плана

def _plan(admin, key, target, status="active", rev=1):
    admin.execute("""insert into plans (id, parameter, metric, value, subject, target_at, basis, asserted_by, source_id,
                     revision, status, collector, dedup_key, content_hash, signature)
                     values (gen_random_uuid(), 'category', 'calendar.day_type', '{"t":"category","code":"nonworking","dictionary":"x"}',
                     '[]', %s, 'norm', 'gov', 'xmlcalendar', %s, %s, 'test', %s, md5(random()::text), 'sig')""",
                  (target, rev, status, key))


def test_plan_trigger_moves_and_closes(conn, admin, settings, make_llm):
    service.ensure_owner(conn, settings)
    llm, _ = make_llm([])
    t1 = datetime.now(timezone.utc) + timedelta(days=2)
    _plan(admin, "cal:RU:x", t1)
    jid = jobs.create(conn, {"title": "Перед праздником", "message": "Завтра нерабочий день",
                             "plan": {"source": "xmlcalendar", "key": "cal:RU:x", "offset_min": -1440}}, USER)
    assert abs((jobs.get(conn, jid)["next_run_at"] - (t1 - timedelta(days=1))).total_seconds()) < 1
    # план перенесли на день позже → задание пересчиталось, не выполнилось
    t2 = t1 + timedelta(days=1)
    _plan(admin, "cal:RU:x", t2, rev=2)
    due(conn, jid)
    tick(conn, llm)
    j = jobs.get(conn, jid)
    assert j["run_count"] == 0 and abs((j["next_run_at"] - (t2 - timedelta(days=1))).total_seconds()) < 1
    # план отменили → задание закрылось само
    _plan(admin, "cal:RU:x", t2, status="cancelled", rev=3)
    due(conn, jid)
    tick(conn, llm)
    j = jobs.get(conn, jid)
    assert j["status"] == "done" and "не нужно" in j["last_result"]


# ---------- встроенные: сборщики по командам

def test_collect_jobs_make_commands(conn, settings, make_llm, dbname):
    llm, _ = make_llm([])
    made = service.ensure_system_jobs(conn)
    assert len(made) == 8 and service.ensure_system_jobs(conn) == []
    jid = conn.execute("select id from jobs.jobs where system_key='collect:cbr.fx'").fetchone()[0]
    due(conn, jid)
    tick(conn, llm)
    due(conn, jid)
    tick(conn, llm)                                   # вторая команда не ставится, пока первая в очереди
    assert conn.execute("select count(*) from jobs.commands where name='cbr.fx'").fetchone()[0] == 1
    # сборщик (роль collector) видит пульс и забирает команду
    from evactix import runner
    with psycopg.connect(f"host=127.0.0.1 port=5432 user=collector dbname={dbname}", autocommit=True) as col:
        assert runner.conductor_alive(col)
        cmd = runner.take_command(col)
        assert cmd[1] in service.COLLECTORS
        runner.finish_command(col, cmd[0], True, "записано 3")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            col.execute("insert into jobs.commands (target, name) values ('collector','x')")
    assert conn.execute("select status from jobs.commands where id=%s", (cmd[0],)).fetchone()[0] == "done"


# ---------- агент «выполнить»

def test_do_agent_reads_core_and_writes(conn, admin, settings, make_llm):
    service.ensure_owner(conn, settings)
    admin.execute("""insert into events (id, type, kind, occurred_at, observed_at, subject, source_id, source_class, asserted_by,
                     confidence, license, metric, value, num, collector, collector_version, dedup_key, content_hash, signature)
                     values (gen_random_uuid(), 'fx.rate.published', 'fact', now(), now(), '[{"id":"currency:USD"}]', 'cbr.fx', 'open',
                     'cbr', 0.98, 'x', 'fx.rate_rub', '{"t":"money","amount":81.5}', 81.5, 't', '1', 'k1', 'h', 's')""")
    llm, script = make_llm([tool_call("latest", {"metric": "fx.rate_rub", "subject": "currency:USD"}),
                            {"content": '{"send": true, "text": "Доллар 81,50 ₽"}'}])
    jid = jobs.create(conn, {"title": "Курс доллара", "action": "do", "instruction": "сообщи курс доллара", "cron": "0 9 * * *"}, USER)
    due(conn, jid)
    tick(conn, llm)
    assert conn.execute("select text from jobs.outbox").fetchone()[0] == "Доллар 81,50 ₽"
    tool_msg = [m for m in script.requests[-1]["messages"] if m["role"] == "tool"][0]
    assert "81.5" in tool_msg["content"]
    assert "latest" in jobs.get(conn, jid)["last_result"]


def test_do_agent_silent_and_spawn_limit(conn, settings, make_llm):
    service.ensure_owner(conn, settings)
    spec = lambda i: {"title": f"Дочка {i}", "message": "x", "cron": "0 9 * * *"}
    llm, _ = make_llm([tool_call("job_create", spec(1), "a"), tool_call("job_create", spec(2), "b"),
                       tool_call("job_create", spec(3), "c"), tool_call("job_create", spec(4), "d"),
                       {"content": '{"send": false, "text": ""}'}])
    jid = jobs.create(conn, {"title": "Плодовитое", "action": "do", "instruction": "наставь заданий", "cron": "0 9 * * *"}, USER)
    due(conn, jid)
    tick(conn, llm)
    kids = conn.execute("select author_type, parent_job_id from jobs.jobs where title like 'Дочка%%'").fetchall()
    assert len(kids) == 3 and all(k == ("agent", jid) for k in kids)
    assert conn.execute("select count(*) from jobs.outbox").fetchone()[0] == 0          # писать нечего — молчит


# ---------- разговор в Telegram

def test_chat_creates_and_cancels(conn, settings, make_llm):
    service.ensure_owner(conn, settings)
    llm, _ = make_llm([
        tool_call("job_create", {"title": "Позвонить Иванову", "action": "remind", "message": "Позвонить Иванову",
                                 "cron": "0 11 * * 1", "to": ["me"]}),
        {"content": "Поставила: №1, по понедельникам в 11:00."},
        tool_call("job_cancel", {"job_id": 1}),
        {"content": "Отменил задание №1."},
    ])
    p = service.TelegramPoller(settings, llm, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    reply = p.handle(conn, "1001", "Денис", "каждый понедельник в 11 напоминай позвонить Иванову")
    j = jobs.get(conn, 1)
    assert j["cron"] == "0 11 * * 1" and j["source"] == "telegram" and j["author_name"] == "Денис (Telegram)" and "№1" in reply
    p.handle(conn, "1001", "Денис", "отмени задание 1")
    assert jobs.get(conn, 1)["cancelled_by"] == "Денис (Telegram)"
    assert conn.execute("select count(*) from jobs.outbox where chat_id='1001'").fetchone()[0] == 2
    assert conn.execute("select count(*) from jobs.chat_log").fetchone()[0] == 4


def test_poller_ignores_strangers(conn, settings, make_llm):
    llm, _ = make_llm([])
    upd = {"ok": True, "result": [{"update_id": 5, "message": {"text": "привет", "chat": {"id": 999}, "from": {"first_name": "X"}}}]}
    p = service.TelegramPoller(settings, llm, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=upd))))
    assert p.poll_once(conn, timeout=0) == 0
    assert conn.execute("select (value->>'offset')::int from jobs.kv where key='tg_offset'").fetchone()[0] == 6
    assert conn.execute("select count(*) from jobs.outbox").fetchone()[0] == 0


# ---------- webhook

def test_webhook_recipient_signed(conn, settings, make_llm):
    service.ensure_owner(conn, settings)
    from conductor.tools import JobTools
    jt = JobTools(conn, USER, is_owner=True)
    r = jt.recipient_add(kind="webhook", name="CRM клиента", address="https://hooks.example.com/evactix")
    secret = r["secret"]
    jid = jobs.create(conn, {"title": "В CRM", "message": "Сигнал", "every_minutes": 30, "to": ["CRM клиента"]}, USER)
    due(conn, jid)
    tick(conn, make_llm([])[0])
    got = []
    t = httpx.Client(transport=httpx.MockTransport(lambda req: got.append(req) or httpx.Response(204)))
    assert outbox.Sender("", t).send_pending(conn) == 1
    req = got[0]
    expect = "sha256=" + hmac.new(secret.encode(), req.headers["X-Evactix-Timestamp"].encode() + b"." + req.content,
                                  hashlib.sha256).hexdigest()
    assert req.headers["X-Evactix-Signature"] == expect and json.loads(req.content)["text"] == "Сигнал"
    with pytest.raises(JobError, match="владелец"):
        JobTools(conn, USER, is_owner=False).recipient_add(kind="telegram", name="x", address="5")

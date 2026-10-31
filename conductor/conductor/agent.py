"""Агент на DeepSeek: «выполнить» по заданию и разговор в Telegram.

Агент читает ядро только через инструменты (только чтение, с лимитами) и пишет только получателям
своего задания — отправляет не он сам, а Дирижёр через очередь. Новые задания — не больше 3 за запуск.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import jobs, tools
from .cron import MSK
from .llm import DeepSeek
from .persona import PERSONA

MAX_ROUNDS = 8
READ = list(tools.READ_TOOLS)


def _now_text() -> str:
    d = datetime.now(timezone.utc).astimezone(MSK)
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    return f"{d:%Y-%m-%d %H:%M}, {days[d.weekday()]} (Москва)"


def loop(llm: DeepSeek, conn, messages: list[dict], names: list[str], jt: tools.JobTools | None) -> tuple[str, list[str]]:
    schemas = tools.tool_schemas(names, tools.JobTools.SCHEMAS)
    trace = []
    for _ in range(MAX_ROUNDS):
        msg = llm.chat(messages, schemas)
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or "").strip(), trace
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for c in calls:
            fn = c.get("function", {})
            name = fn.get("name", "")
            trace.append(name)
            out = tools.call(conn, jt, name, fn.get("arguments", "{}")) if name in names else \
                json.dumps({"error": f"инструмент {name} недоступен"}, ensure_ascii=False)
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out})
    return "", trace + ["лимит шагов"]


DO_SYSTEM = """Ты — агент Дирижёра платформы Evactix. Выполняешь задание №{id} «{title}».
Инструкция: {instruction}
Сейчас {now}.

Правила:
- Данные бери только из инструментов (ядро, только чтение). Не выдумывай чисел и фактов.
- Пиши получателям, только если есть что сказать по делу. Нечего сказать — не пиши.
- Текст — коротко, по-русски, обычным текстом без разметки, с конкретными числами и датами.
- Можешь поставить не больше 3 новых заданий (job_create) и закрыть это задание (job_close_self), если оно больше не нужно.
- Персональных данных в системе нет и не запрашивай.

В конце ответь строго одной строкой JSON: {{"send": true или false, "text": "сообщение получателям"}}"""


def parse_decision(content: str) -> tuple[bool, str]:
    m = re.search(r"\{.*\}", content, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            return bool(d.get("send")), str(d.get("text") or "").strip()
        except json.JSONDecodeError:
            pass
    return bool(content.strip()), content.strip()


def run_do(llm: DeepSeek, conn, j: dict) -> tuple[bool, str, list[str], list[int]]:
    jt = tools.JobTools(conn, jobs.Author("agent", f"агент (задание №{j['id']})", "agent", str(j["id"])),
                        parent_job_id=j["id"], self_job_id=j["id"])
    messages = [{"role": "system", "content": DO_SYSTEM.format(id=j["id"], title=j["title"],
                                                               instruction=j["instruction"], now=_now_text())},
                {"role": "user", "content": "Выполни задание."}]
    content, trace = loop(llm, conn, messages, READ + ["job_create", "job_list", "job_close_self"], jt)
    send, text = parse_decision(content)
    return send and bool(text), text, trace, jt.created


CHAT_SYSTEM = PERSONA + """
Сейчас ты разговариваешь в Telegram с {who}. Сейчас {now}.
Задания ставит и исполняет Дирижёр (отдельный сервис) — ты передаёшь их ему через инструменты и так и говоришь:
«поставила Дирижёру», «Дирижёр пришлёт в понедельник в 11:00».

Что ты умеешь:
- ставить задания (job_create): «напомни…» — action remind, message — точный текст напоминания;
  «присылай / проверяй / следи…» — action do, instruction — что проверить и когда писать;
- показывать задания (job_list) и отменять их по номеру (job_cancel);
- отвечать на вопросы по данным ядра через инструменты (курсы ЦБ, ключевая ставка, металлы, погода по опорным точкам,
  календарь, бедствия, землетрясения, состояние источников);{owner_extra}

Правила:
- Время — московское. Регулярное расписание — cron из 5 полей по Москве: «по понедельникам в 11:00» = '0 11 * * 1',
  «по будням в 9:00» = '0 9 * * 1-5', «каждый день в 8:30» = '30 8 * * *'. Разово — run_at 'YYYY-MM-DD HH:MM'.
- Получатель по умолчанию — тот, кто пишет ('me'). Если всё понятно — ставь сразу, не переспрашивай.
  Если непонятно время или что делать — спроси коротко.
- После постановки подтверди: номер, расписание словами, ближайший запуск.
- Числа и факты — только из инструментов, ничего не выдумывай. Если данных нет — так и скажи.
- Отвечай коротко, обычным текстом без разметки."""


def chat(llm: DeepSeek, conn, chat_id: str, user_name: str, text: str, is_owner: bool) -> str:
    author = jobs.Author("user", f"{user_name} (Telegram)", "telegram", str(chat_id))
    jt = tools.JobTools(conn, author, is_owner=is_owner)
    history = conn.execute("select role, text from jobs.chat_log where chat_id=%s order by at desc limit 10",
                           (str(chat_id),)).fetchall()
    extra = "\n- добавлять получателей (recipient_add): Telegram-чат или webhook; показывать их (recipients)." if is_owner else ""
    messages = [{"role": "system", "content": CHAT_SYSTEM.format(who="владельцем" if is_owner else user_name,
                                                                 now=_now_text(), owner_extra=extra)}]
    messages += [{"role": r, "content": t} for r, t in reversed(history)]
    messages.append({"role": "user", "content": text})
    names = READ + ["job_create", "job_list", "job_cancel"] + (["recipient_add", "recipients"] if is_owner else [])
    reply, _ = loop(llm, conn, messages, names, jt)
    return reply or "Не получилось ответить, попробуй сформулировать иначе."

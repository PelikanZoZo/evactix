"""Отправщик: единственный выход Дирижёра наружу. Telegram и webhook (с подписью HMAC-SHA256).

Всё, что уходит, сначала ложится в очередь jobs.outbox, потом отправляется с повторами.
Токен Telegram — часть адреса запроса, поэтому адрес и тело ответа в логи не пишем.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
from psycopg.types.json import Jsonb

log = logging.getLogger("conductor.outbox")
BACKOFF = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15), timedelta(hours=1)]
TG_LIMIT = 4000


def enqueue(conn, text: str, recipient_ids: list[int], job_id: int | None = None, payload: dict | None = None) -> int:
    for rid in recipient_ids:
        conn.execute("insert into jobs.outbox (recipient_id, job_id, text, payload) values (%s,%s,%s,%s)",
                     (rid, job_id, text, Jsonb(payload) if payload else None))
    return len(recipient_ids)


def enqueue_chat(conn, chat_id: str, text: str) -> None:
    conn.execute("insert into jobs.outbox (chat_id, text) values (%s,%s)", (str(chat_id), text))


def sign(secret: str, ts: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()


class Sender:
    def __init__(self, tg_token: str, http: httpx.Client | None = None):
        self.tg_token = tg_token
        self.http = http or httpx.Client(timeout=20)

    def _telegram(self, chat_id: str, text: str) -> None:
        if not self.tg_token:
            raise RuntimeError("Telegram не настроен")
        for i in range(0, len(text), TG_LIMIT):
            try:
                r = self.http.post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                                   json={"chat_id": chat_id, "text": text[i:i + TG_LIMIT],
                                         "disable_web_page_preview": True})
            except httpx.HTTPError as e:
                raise RuntimeError(f"Telegram недоступен: {type(e).__name__}") from None
            if r.status_code != 200:
                raise RuntimeError(f"Telegram ответил {r.status_code}")

    def _webhook(self, url: str, secret: str, item: dict) -> None:
        body = json.dumps(item, ensure_ascii=False, default=str).encode()
        ts = str(int(time.time()))
        try:
            r = self.http.post(url, content=body, headers={
                "Content-Type": "application/json", "User-Agent": "Evactix-Conductor/1.0",
                "X-Evactix-Timestamp": ts, "X-Evactix-Signature": sign(secret or "", ts, body)})
        except httpx.HTTPError as e:
            raise RuntimeError(f"webhook недоступен: {type(e).__name__}") from None
        if r.status_code >= 300:
            raise RuntimeError(f"webhook ответил {r.status_code}")

    def send_pending(self, conn, limit: int = 20) -> int:
        sent = 0
        with conn.transaction():
            rows = conn.execute("""
                select o.id, o.chat_id, o.text, o.payload, o.attempts, o.job_id, o.created_at,
                       r.kind, r.address, r.secret, j.title
                from jobs.outbox o left join jobs.recipients r on r.id = o.recipient_id
                left join jobs.jobs j on j.id = o.job_id
                where o.status='pending' and o.next_try_at <= now()
                order by o.id limit %s for update of o skip locked""", (limit,)).fetchall()
            for oid, chat_id, text, payload, attempts, job_id, created, kind, address, secret, title in rows:
                try:
                    if kind == "webhook":
                        self._webhook(address, secret, {"event": "evactix.message", "id": oid, "job_id": job_id,
                                                        "title": title, "text": text, "data": payload,
                                                        "created_at": created.isoformat()})
                    else:
                        self._telegram(address or chat_id, text)
                    conn.execute("update jobs.outbox set status='sent', sent_at=now(), attempts=attempts+1 where id=%s", (oid,))
                    sent += 1
                except Exception as e:
                    err = str(e)[:300]
                    if attempts < len(BACKOFF):
                        conn.execute("""update jobs.outbox set attempts=attempts+1, last_error=%s, next_try_at=now()+%s
                                        where id=%s""", (err, BACKOFF[attempts], oid))
                    else:
                        conn.execute("update jobs.outbox set status='failed', attempts=attempts+1, last_error=%s where id=%s",
                                     (err, oid))
                    log.warning("отправка %s не удалась: %s", oid, err)
        return sent

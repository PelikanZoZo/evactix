"""Запись в ядро: валидация, дедупликация, поправки через supersedes, подпись цепочки, сырые данные."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import h3
import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from .model import EventIn, PlanIn
from .validate import ALLOWED_ENTITY_TYPES, Rejected, check_event, check_plan

H3_RES = 6   # ~36 км² — уровень района/города


def uuid7() -> uuid.UUID:
    """UUIDv7: сортируется по времени создания."""
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(secrets.token_bytes(10), "big")
    n = (ms & ((1 << 48) - 1)) << 80
    n |= 0x7 << 76
    n |= ((rand >> 62) & 0xFFF) << 64
    n |= 0b10 << 62
    n |= rand & ((1 << 62) - 1)
    return uuid.UUID(int=n)


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@dataclass
class Counts:
    written: int = 0
    skipped: int = 0
    rejected: int = 0
    reasons: list[str] = field(default_factory=list)

    def add(self, other: "Counts") -> None:
        self.written += other.written
        self.skipped += other.skipped
        self.rejected += other.rejected
        self.reasons += other.reasons


class RawStore:
    """Исходники на диске: <raw_dir>/<source>/<YYYY>/<MM>/<DD>/<sha256>.<ext>.gz. Одинаковые не дублируются."""

    def __init__(self, root: str):
        self.root = root

    def save(self, source_id: str, data: bytes, ext: str) -> tuple[str, str]:
        h = sha256(data)
        d = datetime.now(timezone.utc)
        rel = f"{source_id}/{d:%Y/%m/%d}/{h}.{ext}.gz"
        path = os.path.join(self.root, rel)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with gzip.open(tmp, "wb", compresslevel=6) as f:
                f.write(data)
            os.replace(tmp, path)
        return rel, h


class Store:
    def __init__(self, conn: psycopg.Connection, collector: str, collector_version: str, source_id: str):
        self.conn = conn
        self.collector = collector
        self.version = collector_version
        self.source_id = source_id
        row = conn.execute("select source_class, base_confidence, license from sources where id=%s",
                           (source_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"источник {source_id} не в справочнике")
        self.source_class, self.base_conf, self.license = row
        self.metrics = dict(conn.execute("select id, value_type from metrics").fetchall())
        self.entities = {r[0] for r in conn.execute("select id from entities")}

    # --- общие части
    def _reject(self, reason: str, content: bytes) -> None:
        self.conn.execute("insert into rejects (source_id, collector, reason, content_hash) values (%s,%s,%s,%s)",
                          (self.source_id, self.collector, reason[:500], sha256(content)))

    def _ensure_entities(self, subject) -> None:
        for s in subject:
            if s.id not in self.entities:
                etype = s.id.split(":", 1)[0]
                if etype in ALLOWED_ENTITY_TYPES:
                    self.conn.execute("insert into entities (id, type) values (%s,%s) on conflict do nothing",
                                      (s.id, etype))
                    self.entities.add(s.id)

    def _sign(self, stream: str, content_hash: str) -> str:
        row = self.conn.execute("select signature from chain_heads where stream=%s", (stream,)).fetchone()
        prev = row[0] if row else "genesis"
        sig = sha256(f"{prev}:{content_hash}".encode())
        self.conn.execute(
            """insert into chain_heads (stream, signature, updated_at) values (%s,%s,now())
               on conflict (stream) do update set signature=excluded.signature, updated_at=now()""",
            (stream, sig))
        return sig

    # --- события
    def write_events(self, items: list[EventIn | dict]) -> Counts:
        c = Counts()
        for item in items:
            try:
                ev = item if isinstance(item, EventIn) else EventIn.model_validate(item)
                check_event(ev, self.metrics)
            except (ValidationError, Rejected) as e:
                reason = str(e).splitlines()[0] if isinstance(e, Rejected) else f"модель: {e.errors()[0]['msg']} ({e.errors()[0]['loc']})"
                raw = canonical(item.model_dump(mode='json') if isinstance(item, EventIn) else item)
                self._reject(reason, raw)
                c.rejected += 1
                c.reasons.append(reason)
                continue
            if self._write_event(ev):
                c.written += 1
            else:
                c.skipped += 1
        return c

    def _write_event(self, ev: EventIn) -> bool:
        value = ev.value.model_dump(mode="json", by_alias=True) if ev.value else None
        content = {
            "type": ev.type, "kind": ev.kind, "occurred_at": ev.occurred_at.isoformat(),
            "valid_from": ev.valid_from.isoformat() if ev.valid_from else None,
            "valid_to": ev.valid_to.isoformat() if ev.valid_to else None,
            "place": ev.place.model_dump(mode="json") if ev.place else None,
            "subject": [s.model_dump(mode="json") for s in ev.subject],
            "metric": ev.metric, "value": value, "payload": ev.payload,
            "aggregation": ev.aggregation, "period": str(ev.period) if ev.period else None,
            "asserted_by": ev.asserted_by, "tenant": ev.tenant,
        }
        chash = sha256(canonical(content))
        last = self.conn.execute(
            "select id, content_hash from events where dedup_key=%s and tenant=%s order by recorded_at desc limit 1",
            (ev.dedup_key, ev.tenant)).fetchone()
        if last and last[1] == chash:
            return False
        self._ensure_entities(ev.subject)
        p = ev.place
        cell = h3.latlng_to_cell(p.lat, p.lon, H3_RES) if p and p.lat is not None and p.lon is not None else None
        self.conn.execute(
            """insert into events (id, type, kind, occurred_at, valid_from, valid_to, observed_at,
                 lat, lon, h3, region, tz, subject, source_id, source_class, asserted_by, confidence, license,
                 metric, value, num, aggregation, period, precision, quality, payload,
                 chain_id, plan_id, caused_by, supersedes, raw_ref, raw_hash,
                 tenant, collector, collector_version, dedup_key, content_hash, signature)
               values (%s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s,%s,%s,
                       %s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s)""",
            (uuid7(), ev.type, ev.kind, ev.occurred_at, ev.valid_from, ev.valid_to, ev.observed_at,
             p.lat if p else None, p.lon if p else None, cell, p.region if p else None, p.tz if p else None,
             Jsonb([s.model_dump(mode="json", exclude_none=True) for s in ev.subject]),
             self.source_id, self.source_class, ev.asserted_by,
             ev.confidence if ev.confidence is not None else self.base_conf, self.license,
             ev.metric, Jsonb(value) if value else None, ev.value.main_number() if ev.value else None,
             ev.aggregation, ev.period, ev.precision, ev.quality, Jsonb(ev.payload),
             ev.chain_id, ev.plan_id, ev.caused_by, last[0] if last else None, ev.raw_ref, ev.raw_hash,
             ev.tenant, self.collector, self.version, ev.dedup_key, chash,
             self._sign(f"events:{self.source_id}", chash)))
        return True

    # --- планы
    def write_plans(self, items: list[PlanIn | dict]) -> Counts:
        c = Counts()
        for item in items:
            try:
                pl = item if isinstance(item, PlanIn) else PlanIn.model_validate(item)
                check_plan(pl, self.metrics)
            except (ValidationError, Rejected) as e:
                reason = str(e).splitlines()[0] if isinstance(e, Rejected) else f"модель: {e.errors()[0]['msg']}"
                raw = canonical(item.model_dump(mode='json') if isinstance(item, PlanIn) else item)
                self._reject(reason, raw)
                c.rejected += 1
                c.reasons.append(reason)
                continue
            if self._write_plan(pl):
                c.written += 1
            else:
                c.skipped += 1
        return c

    def _write_plan(self, pl: PlanIn) -> bool:
        value = pl.value.model_dump(mode="json", by_alias=True) if pl.value else None
        content = {
            "parameter": pl.parameter, "metric": pl.metric, "value": value,
            "window_from": pl.window_from.isoformat() if pl.window_from else None,
            "window_to": pl.window_to.isoformat() if pl.window_to else None,
            "tolerance": pl.tolerance, "subject": [s.model_dump(mode="json") for s in pl.subject],
            "target_at": pl.target_at.isoformat(), "basis": pl.basis, "asserted_by": pl.asserted_by,
            "tenant": pl.tenant,
        }
        chash = sha256(canonical(content))
        last = self.conn.execute(
            "select id, content_hash, revision from plans where dedup_key=%s and tenant=%s order by recorded_at desc limit 1",
            (pl.dedup_key, pl.tenant)).fetchone()
        if last and last[1] == chash:
            return False
        self._ensure_entities(pl.subject)
        self.conn.execute(
            """insert into plans (id, parameter, metric, value, window_from, window_to, tolerance, subject,
                 target_at, basis, asserted_by, source_id, raw_ref, raw_hash, announced_at,
                 revision, supersedes, tenant, collector, dedup_key, content_hash, signature)
               values (%s,%s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s,%s)""",
            (uuid7(), pl.parameter, pl.metric, Jsonb(value) if value else None, pl.window_from, pl.window_to,
             Jsonb(pl.tolerance) if pl.tolerance else None,
             Jsonb([s.model_dump(mode="json", exclude_none=True) for s in pl.subject]),
             pl.target_at, pl.basis, pl.asserted_by, self.source_id, pl.raw_ref, pl.raw_hash, pl.announced_at,
             (last[2] + 1) if last else 1, last[0] if last else None, pl.tenant, self.collector,
             pl.dedup_key, chash, self._sign(f"plans:{self.source_id}", chash)))
        return True

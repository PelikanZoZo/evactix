"""Каркас сборщика: получает исходник, сохраняет его в сырые данные, разбирает в события и планы."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import httpx

from ..model import EventIn, PlanIn
from ..store import RawStore


@dataclass
class Batch:
    events: list[EventIn | dict] = field(default_factory=list)
    plans: list[PlanIn | dict] = field(default_factory=list)


class Collector:
    name: str = ""            # уникальное имя сборщика
    source_id: str = ""       # источник из справочника
    version: str = "0.1"
    interval: timedelta = timedelta(hours=1)

    def __init__(self, http: httpx.Client, raw: RawStore):
        self.http = http
        self.raw = raw

    def fetch_raw(self, url: str, ext: str, **kw) -> tuple[bytes, str, str]:
        """Скачивает исходник и сохраняет его. Возвращает (данные, raw_ref, raw_hash)."""
        r = self.http.request(kw.pop("method", "GET"), url, **kw)
        r.raise_for_status()
        ref, h = self.raw.save(self.source_id, r.content, ext)
        return r.content, ref, h

    def collect(self, conn) -> Batch:   # conn — только для чтения состояния (например, backfill)
        raise NotImplementedError

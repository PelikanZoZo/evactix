"""Производственный календарь (xmlcalendar.ru): объявленные будущие нерабочие и сокращённые дни — это планы."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .base import Batch, Collector

COUNTRY_TZ = {"RU": "Europe/Moscow", "KZ": "Asia/Almaty", "BY": "Europe/Minsk", "UZ": "Asia/Tashkent"}
URL = "https://xmlcalendar.ru/data/{cc}/{year}/calendar.json"


class CalendarCollector(Collector):
    name = "xmlcalendar"
    source_id = "xmlcalendar"
    interval = timedelta(days=1)

    def parse(self, cc: str, data: bytes, ref: str, h: str) -> dict[str, dict]:
        """Возвращает dedup_key → план. Отметки: '+' перенесённый выходной, '*' сокращённый день."""
        d = json.loads(data)
        tz = ZoneInfo(COUNTRY_TZ[cc])
        year = int(d["year"])
        plans = {}
        for m in d["months"]:
            for tok in filter(None, (t.strip() for t in m["days"].split(","))):
                code = "nonworking"
                if tok.endswith("*"):
                    code = "shortened"
                elif tok.endswith("+"):
                    code = "nonworking_transfer"
                day = date(year, int(m["month"]), int(tok.rstrip("*+")))
                plans[f"cal:{cc}:{day.isoformat()}"] = self._plan(cc, day, tz, code, ref, h)
        return plans

    def _plan(self, cc, day, tz, code, ref, h) -> dict:
        start = datetime.combine(day, time(0), tzinfo=tz)
        return {
            "parameter": "category", "metric": "calendar.day_type",
            "value": {"t": "category", "code": code, "dictionary": "xmlcalendar"},
            "window_from": start, "window_to": start + timedelta(days=1),
            "subject": [{"id": f"country:{cc}"}],
            "target_at": start, "basis": "norm", "asserted_by": "government",
            "dedup_key": f"cal:{cc}:{day.isoformat()}",
            "raw_ref": ref, "raw_hash": h,
        }

    def collect(self, conn) -> Batch:
        b = Batch()
        this_year = datetime.now(timezone.utc).year
        for cc in COUNTRY_TZ:
            for year in (this_year, this_year + 1):
                try:
                    data, ref, h = self.fetch_raw(URL.format(cc=cc.lower(), year=year), "json")
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:   # следующий год ещё не опубликован
                        continue
                    raise
                plans = self.parse(cc, data, ref, h)
                # День, который был нерабочим, а в новой версии календаря исчез, стал рабочим — это поправка плана
                prev = conn.execute(
                    """select dedup_key from plans_current
                       where source_id=%s and dedup_key like %s and value->>'code' <> 'working'""",
                    (self.source_id, f"cal:{cc}:{year}-%")).fetchall()
                tz = ZoneInfo(COUNTRY_TZ[cc])
                for (key,) in prev:
                    if key not in plans:
                        day = date.fromisoformat(key.rsplit(":", 1)[1])
                        plans[key] = self._plan(cc, day, tz, "working", ref, h)
                b.plans += list(plans.values())
        return b

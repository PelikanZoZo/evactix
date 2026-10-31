"""Met Norway Locationforecast: прогноз погоды по опорным точкам на 72 часа (CC BY 4.0, атрибуция MET Norway).

Ключ дедупликации не включает время выпуска прогноза: новый выпуск с другим значением
дописывается как поправка (supersedes), неизменившиеся значения не пишутся повторно.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..catalog import POINTS
from .base import Batch, Collector

URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={lat:.4f}&lon={lon:.4f}"
HORIZON = timedelta(hours=72)


class MetnoCollector(Collector):
    name = "metno.forecast"
    source_id = "metno.forecast"
    interval = timedelta(minutes=60)

    def __init__(self, http, raw):
        super().__init__(http, raw)
        self.last_modified: dict[str, str] = {}

    def parse(self, slug: str, data: bytes, ref: str, h: str) -> list[dict]:
        p = next(x for x in POINTS if x.slug == slug)
        d = json.loads(data)
        issued = datetime.fromisoformat(d["properties"]["meta"]["updated_at"].replace("Z", "+00:00"))
        place = {"lat": p.lat, "lon": p.lon, "region": p.region, "tz": p.tz}
        out = []

        def add(at, metric, value, extra=None):
            e = {
                "type": "weather.forecast", "kind": "forecast",
                "occurred_at": at, "observed_at": issued,
                "place": place, "subject": [{"id": f"point:{slug}"}],
                "asserted_by": "met.no", "metric": metric, "value": value,
                "aggregation": "instant", "quality": "estimated",
                "dedup_key": f"metno:{slug}:{metric}:{at.isoformat()}",
                "raw_ref": ref, "raw_hash": h,
            }
            if extra:
                e.update(extra)
            out.append(e)

        for ts in d["properties"]["timeseries"]:
            at = datetime.fromisoformat(ts["time"].replace("Z", "+00:00"))
            if at - issued > HORIZON:
                break
            inst = ts["data"]["instant"]["details"]
            if "air_temperature" in inst:
                add(at, "weather.air_temperature", {"t": "number", "v": inst["air_temperature"], "unit": "Cel"})
            if "wind_speed" in inst:
                add(at, "weather.wind_speed", {"t": "number", "v": inst["wind_speed"], "unit": "m/s"})
            if "wind_from_direction" in inst:
                add(at, "weather.wind_from_direction",
                    {"t": "cyclic", "v": round(inst["wind_from_direction"] % 360.0, 1) % 360.0, "period": 360.0})
            n1 = ts["data"].get("next_1_hours", {}).get("details", {})
            if "precipitation_amount" in n1:
                add(at, "weather.precipitation", {"t": "number", "v": n1["precipitation_amount"], "unit": "mm"},
                    {"aggregation": "sum", "period": timedelta(hours=1), "valid_from": at,
                     "valid_to": at + timedelta(hours=1)})
        return out

    def collect(self, conn) -> Batch:
        b = Batch()
        for p in POINTS:
            headers = {}
            if p.slug in self.last_modified:
                headers["If-Modified-Since"] = self.last_modified[p.slug]
            r = self.http.get(URL.format(lat=p.lat, lon=p.lon), headers=headers)
            if r.status_code == 304:
                continue
            r.raise_for_status()
            if "last-modified" in r.headers:
                self.last_modified[p.slug] = r.headers["last-modified"]
            ref, h = self.raw.save(self.source_id, r.content, "json")
            b.events += self.parse(p.slug, r.content, ref, h)
        return b

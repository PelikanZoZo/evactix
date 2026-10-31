"""NOAA GFS 0.25°: глобальная модель США. Берём через фильтр NOMADS только наш район и нужные поля.

Общественное достояние. Лимит NOMADS — 120 обращений в минуту на IP, поэтому идём не чаще раза в секунду.
GRIB не храним (тяжёлый): в сырые данные кладём извлечённые по точкам значения и адреса запросов.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from .base import Batch, Collector
from .weather_common import forecast_event, grib_points, k_to_c, wind_from_uv

FILTER = ("https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl?dir=%2Fgfs.{d}%2F{hh}%2Fatmos"
          "&file=gfs.t{hh}z.pgrb2.0p25.f{step:03d}"
          "&var_TMP=on&var_UGRD=on&var_VGRD=on&var_PRATE=on&var_GUST=on"
          "&lev_2_m_above_ground=on&lev_10_m_above_ground=on&lev_surface=on"
          "&subregion=&toplat=61&leftlon=29&rightlon=133&bottomlat=42")
STEPS = range(0, 73, 3)


class GfsCollector(Collector):
    name = "noaa.gfs"
    source_id = "noaa.gfs"
    interval = timedelta(hours=3)
    pause = 1.0

    def _get(self, run: datetime, step: int) -> bytes | None:
        r = self.http.get(FILTER.format(d=f"{run:%Y%m%d}", hh=f"{run:%H}", step=step))
        time.sleep(self.pause)
        if r.status_code != 200 or not r.content.startswith(b"GRIB"):
            return None
        return r.content

    def latest_run(self, now: datetime) -> datetime | None:
        base = (now - timedelta(hours=4)).replace(minute=0, second=0, microsecond=0)
        base = base.replace(hour=base.hour - base.hour % 6)
        for back in range(4):
            run = base - timedelta(hours=6 * back)
            if self._get(run, STEPS[-1]) is not None:
                return run
        return None

    def to_events(self, run: datetime, fields: list[dict], ref: str | None, h: str | None) -> list[dict]:
        by = {}
        for f in fields:
            if f["short"] == "prate" and "-" in f["step"]:
                continue                                  # средняя за интервал — берём мгновенную
            by.setdefault(f["valid"], {})[f["short"]] = f["values"]
        out = []
        for at, d in sorted(by.items()):
            for slug in (d.get("2t") or {}):
                ev = lambda metric, value, **kw: out.append(
                    forecast_event("gfs", slug, metric, at, run, value, "noaa", ref, h, **kw))
                ev("weather.air_temperature", {"t": "number", "v": k_to_c(d["2t"][slug]), "unit": "Cel"})
                if "10u" in d and "10v" in d:
                    sp, dr = wind_from_uv(d["10u"][slug], d["10v"][slug])
                    ev("weather.wind_speed", {"t": "number", "v": round(sp, 2), "unit": "m/s"})
                    ev("weather.wind_from_direction", {"t": "cyclic", "v": dr, "period": 360.0})
                if "gust" in d:
                    ev("weather.wind_gust", {"t": "number", "v": round(d["gust"][slug], 2), "unit": "m/s"}, aggregation="max")
                if "prate" in d:                          # кг/м²/с → мм/ч, как количество за час
                    ev("weather.precipitation", {"t": "number", "v": round(max(0.0, d["prate"][slug]) * 3600, 2), "unit": "mm"},
                       aggregation="sum", period=timedelta(hours=1), valid_from=at, valid_to=at + timedelta(hours=1))
        return out

    def collect(self, conn) -> Batch:
        run = self.latest_run(datetime.now(timezone.utc))
        if run is None:
            raise RuntimeError("GFS: свежего прогона нет")
        if conn.execute("select 1 from events where source_id=%s and observed_at=%s limit 1", (self.source_id, run)).fetchone():
            return Batch()                                # этот прогон уже записан
        fields, urls = [], []
        for step in STEPS:
            data = self._get(run, step)
            if data is None:
                raise RuntimeError(f"GFS: шаг {step} прогона {run:%Y%m%d %H}z недоступен")
            fields += grib_points(data)
            urls.append(FILTER.format(d=f"{run:%Y%m%d}", hh=f"{run:%H}", step=step))
        raw = json.dumps({"run": run.isoformat(), "urls": urls,
                          "fields": [{**f, "valid": f["valid"].isoformat()} for f in fields]}, ensure_ascii=False).encode()
        ref, h = self.raw.save(self.source_id, raw, "json")
        return Batch(events=self.to_events(run, fields, ref, h))

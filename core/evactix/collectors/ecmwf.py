"""ECMWF IFS 0.25° (открытые данные, CC BY 4.0): лучшая глобальная модель.

Из каждого файла прогона берём по индексу только нужные поля (запросы по диапазону байт),
расшифровываем по одному полю и оставляем значения в наших точках. GRIB не храним.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .base import Batch, Collector
from .weather_common import forecast_event, grib_points, k_to_c, wind_from_uv

BASE = "https://data.ecmwf.int/forecasts/{d}/{hh}z/ifs/0p25/oper/{d}{hh}0000-{step}h-oper-fc"
PARAMS = ("2t", "10u", "10v", "tp", "10fg")
STEPS = range(0, 73, 3)


class EcmwfIfsCollector(Collector):
    name = "ecmwf.ifs"
    source_id = "ecmwf.ifs"
    interval = timedelta(hours=3)

    def _index(self, run: datetime, step: int) -> list[dict] | None:
        r = self.http.get(BASE.format(d=f"{run:%Y%m%d}", hh=f"{run:%H}", step=step) + ".index")
        if r.status_code != 200:
            return None
        return [json.loads(line) for line in r.text.splitlines() if line.strip()]

    def latest_run(self, now: datetime) -> datetime | None:
        base = (now - timedelta(hours=6)).replace(minute=0, second=0, microsecond=0)
        base = base.replace(hour=base.hour - base.hour % 6)
        for back in range(4):
            run = base - timedelta(hours=6 * back)
            if self._index(run, STEPS[-1]) is not None:
                return run
        return None

    def fetch_step(self, run: datetime, step: int) -> list[dict]:
        idx = self._index(run, step)
        if idx is None:
            raise RuntimeError(f"ECMWF: шаг {step} прогона {run:%Y%m%d %H}z недоступен")
        url = BASE.format(d=f"{run:%Y%m%d}", hh=f"{run:%H}", step=step) + ".grib2"
        out = []
        for rec in idx:
            if rec.get("param") in PARAMS and rec.get("levtype") == "sfc":
                a, n = rec["_offset"], rec["_length"]
                r = self.http.get(url, headers={"Range": f"bytes={a}-{a + n - 1}"})
                r.raise_for_status()
                for f in grib_points(r.content):
                    f["short"] = rec["param"]
                    out.append(f)
        return out

    def to_events(self, run: datetime, fields: list[dict], ref: str | None, h: str | None) -> list[dict]:
        by = {}
        for f in fields:
            by.setdefault(f["valid"], {})[f["short"]] = f["values"]
        out, prev_tp, prev_at = [], None, None
        for at, d in sorted(by.items()):
            for slug in (d.get("2t") or {}):
                ev = lambda metric, value, **kw: out.append(
                    forecast_event("ecmwf", slug, metric, at, run, value, "ecmwf", ref, h, **kw))
                ev("weather.air_temperature", {"t": "number", "v": k_to_c(d["2t"][slug]), "unit": "Cel"})
                if "10u" in d and "10v" in d:
                    sp, dr = wind_from_uv(d["10u"][slug], d["10v"][slug])
                    ev("weather.wind_speed", {"t": "number", "v": round(sp, 2), "unit": "m/s"})
                    ev("weather.wind_from_direction", {"t": "cyclic", "v": dr, "period": 360.0})
                if "10fg" in d and at > run:
                    ev("weather.wind_gust", {"t": "number", "v": round(d["10fg"][slug], 2), "unit": "m/s"}, aggregation="max")
                if "tp" in d and prev_tp is not None and slug in prev_tp:
                    # tp накапливается с начала прогона (м) → осадки за интервал, мм; время события = начало интервала
                    mm = round(max(0.0, d["tp"][slug] - prev_tp[slug]) * 1000, 2)
                    out.append(forecast_event("ecmwf", slug, "weather.precipitation", prev_at, run,
                                              {"t": "number", "v": mm, "unit": "mm"}, "ecmwf", ref, h,
                                              aggregation="sum", period=at - prev_at, valid_from=prev_at, valid_to=at))
            if "tp" in d:
                prev_tp, prev_at = d["tp"], at
        return out

    def collect(self, conn) -> Batch:
        run = self.latest_run(datetime.now(timezone.utc))
        if run is None:
            raise RuntimeError("ECMWF: свежего прогона нет")
        if conn.execute("select 1 from events where source_id=%s and observed_at=%s limit 1", (self.source_id, run)).fetchone():
            return Batch()
        fields = []
        for step in STEPS:
            fields += self.fetch_step(run, step)
        raw = json.dumps({"run": run.isoformat(), "base": BASE.format(d=f"{run:%Y%m%d}", hh=f"{run:%H}", step="{step}"),
                          "fields": [{**f, "valid": f["valid"].isoformat()} for f in fields]}, ensure_ascii=False).encode()
        ref, h = self.raw.save(self.source_id, raw, "json")
        return Batch(events=self.to_events(run, fields, ref, h))

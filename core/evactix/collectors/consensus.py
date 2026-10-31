"""Погодный консенсус: четыре независимые модели голосуют, ядро выдаёт итог и уверенность.

Числа — медиана (одна сломанная модель не сдвигает итог), разброс — максимум минус минимум.
Направление ветра — круговая средняя. Да/нет (осадки, мороз, сильный ветер) — доля моделей «за».
Уверенность падает с разбросом и с числом проголосовавших: clamp((1 − разброс/K) · min(1, n/3), 0.2, 0.95).
Голоса каждой модели лежат в payload, чтобы итог можно было проверить.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .base import Batch, Collector
from .weather_common import HORIZON, forecast_event

MODELS = {"metno.forecast": "metno", "dwd.mosmix": "dwd", "noaa.gfs": "gfs", "ecmwf.ifs": "ecmwf"}
SPREAD_K = {"weather.air_temperature": 8.0, "weather.wind_speed": 10.0, "weather.wind_gust": 15.0,
            "weather.precipitation": 5.0, "weather.wind_from_direction": 180.0}
STEP = timedelta(hours=3)
FRESH = timedelta(hours=24)           # голос модели старше суток не считаем
PRECIP_MM_H, FROST_C, STRONG_GUST = 0.1, 0.0, 15.0

QUERY = """
select distinct on (dedup_key) source_id, subject->0->>'id', metric, occurred_at, value, period, observed_at
from events
where type = 'weather.forecast' and source_id = any(%s) and occurred_at >= %s and occurred_at < %s
order by dedup_key, recorded_at desc
"""


def confidence(spread: float, n: int, k: float) -> float:
    return round(min(0.95, max(0.2, (1 - spread / k) * min(1.0, n / 3))), 3)


def circular_mean(degs: list[float]) -> tuple[float, float]:
    """Средняя по кругу и разброс (наибольшее угловое отклонение от средней ×2)."""
    s = sum(math.sin(math.radians(d)) for d in degs)
    c = sum(math.cos(math.radians(d)) for d in degs)
    mean = (math.degrees(math.atan2(s, c)) + 360.0) % 360.0
    dev = max(abs((d - mean + 180.0) % 360.0 - 180.0) for d in degs)
    return round(mean, 1) % 360.0, round(min(180.0, 2 * dev), 1)


def on_grid(t: datetime) -> bool:
    return t.minute == 0 and t.second == 0 and t.hour % 3 == 0


def votes_from_rows(rows, now: datetime) -> dict:
    """Строки событий → {(точка, метрика, время): {модель: значение}}. Осадки приводятся к мм/ч."""
    votes = defaultdict(dict)
    for source, subj, metric, at, value, period, observed in rows:
        if not on_grid(at) or not subj or not subj.startswith("point:") or now - observed > FRESH:
            continue
        v = value.get("v")
        if v is None:
            continue
        if metric == "weather.precipitation" and period:
            hours = period.total_seconds() / 3600
            v = v / hours if hours > 0 else v
        votes[(subj.split(":", 1)[1], metric, at)][MODELS[source]] = float(v)
    return votes


class WeatherConsensusCollector(Collector):
    name = "weather.consensus"
    source_id = "weather.consensus"
    interval = timedelta(hours=1)

    def build(self, votes: dict, issued: datetime) -> list[dict]:
        out = []
        by_slot = defaultdict(dict)                     # (точка, время) → метрика → голоса
        for (slug, metric, at), vs in votes.items():
            by_slot[(slug, at)][metric] = vs
        for (slug, at), metrics in sorted(by_slot.items()):
            def ev(metric, value, n, spread, vs, **kw):
                payload = {"n": n, "spread": round(spread, 2), **{f"v_{m}": round(x, 2) for m, x in sorted(vs.items())}}
                out.append(forecast_event("consensus", slug, metric, at, issued, value, "evactix", None, None,
                                          type="weather.consensus", quality="computed", payload=payload,
                                          confidence=confidence(spread, n, SPREAD_K[metric]), **kw))

            for metric in ("weather.air_temperature", "weather.wind_speed", "weather.wind_gust", "weather.precipitation"):
                vs = metrics.get(metric)
                if not vs:
                    continue
                xs = list(vs.values())
                med, spread = statistics.median(xs), max(xs) - min(xs)
                unit = {"weather.air_temperature": "Cel", "weather.precipitation": "mm"}.get(metric, "m/s")
                kw = {}
                if metric == "weather.precipitation":
                    kw = dict(aggregation="sum", period=timedelta(hours=1), valid_from=at, valid_to=at + timedelta(hours=1))
                elif metric == "weather.wind_gust":
                    kw = dict(aggregation="max")
                ev(metric, {"t": "number", "v": round(max(0.0, med) if metric == "weather.precipitation" else med, 2),
                            "unit": unit}, len(xs), spread, vs, **kw)

            if vs := metrics.get("weather.wind_from_direction"):
                mean, spread = circular_mean(list(vs.values()))
                ev("weather.wind_from_direction", {"t": "cyclic", "v": mean, "period": 360.0}, len(vs), spread, vs)

            for metric, src, test in (("weather.precip_agreement", "weather.precipitation", lambda x: x >= PRECIP_MM_H),
                                      ("weather.frost_agreement", "weather.air_temperature", lambda x: x <= FROST_C),
                                      ("weather.strong_wind_agreement", "weather.wind_gust", lambda x: x >= STRONG_GUST)):
                vs = metrics.get(src)
                if not vs:
                    continue
                yes = sum(1 for x in vs.values() if test(x))
                share = round(yes / len(vs), 3)
                out.append(forecast_event("consensus", slug, metric, at, issued,
                                          {"t": "ratio", "v": share, "as": "fraction"}, "evactix", None, None,
                                          type="weather.consensus", quality="computed",
                                          payload={"n": len(vs), "yes": yes},
                                          confidence=round(min(0.95, max(0.2, abs(2 * share - 1) * min(1.0, len(vs) / 3))), 3)))
        return out

    def collect(self, conn) -> Batch:
        now = datetime.now(timezone.utc)
        start = now.replace(minute=0, second=0, microsecond=0)
        start += timedelta(hours=(3 - start.hour % 3) % 3)
        rows = conn.execute(QUERY, (list(MODELS), start, start + HORIZON)).fetchall()
        return Batch(events=self.build(votes_from_rows(rows, now), now))

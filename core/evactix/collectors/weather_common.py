"""Общее для погодных моделей: опорные точки, единицы, извлечение значений из GRIB по точкам."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from ..catalog import POINTS

HORIZON = timedelta(hours=72)

# Станции DWD MOSMIX у опорных точек (WMO; расстояние 1–23 км). У Забайкальска станции нет.
DWD_STATIONS = {
    "moscow": "27612", "saint-petersburg": "26063", "novorossiysk": "37006", "krasnodar": "34929",
    "rostov-on-don": "34731", "kazan": "27595", "yekaterinburg": "28440", "novosibirsk": "29634",
    "vladivostok": "31960",
}


def place(p) -> dict:
    return {"lat": p.lat, "lon": p.lon, "region": p.region, "tz": p.tz}


def wind_from_uv(u: float, v: float) -> tuple[float, float]:
    """Скорость и направление, откуда дует (метеорологическое, градусы)."""
    speed = math.hypot(u, v)
    direction = (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0
    return speed, round(direction, 1) % 360.0


def k_to_c(k: float) -> float:
    return round(k - 273.15, 2)


def forecast_event(source_tag: str, slug: str, metric: str, at: datetime, issued: datetime, value: dict,
                   asserted_by: str, ref: str | None, h: str | None, **extra) -> dict:
    p = next(x for x in POINTS if x.slug == slug)
    e = {
        "type": "weather.forecast", "kind": "forecast", "occurred_at": at, "observed_at": issued,
        "place": place(p), "subject": [{"id": f"point:{slug}"}], "asserted_by": asserted_by,
        "metric": metric, "value": value, "aggregation": "instant", "quality": "estimated",
        "dedup_key": f"{source_tag}:{slug}:{metric}:{at.isoformat()}", "raw_ref": ref, "raw_hash": h,
    }
    e.update(extra)
    return e


def grib_points(data: bytes) -> list[dict]:
    """Все сообщения GRIB в данных → [{short, level, valid, step, values: {slug: value}}].

    Значение по точке — ближайший узел сетки. Память: одно поле за раз.
    """
    import eccodes
    out = []
    pos = 0
    while pos < len(data):
        start = data.find(b"GRIB", pos)
        if start < 0:
            break
        length = int.from_bytes(data[start + 8:start + 16], "big")   # GRIB2: полная длина сообщения
        msg = data[start:start + length]
        pos = start + length
        h = eccodes.codes_new_from_message(msg)
        try:
            vd, vt = eccodes.codes_get(h, "validityDate"), eccodes.codes_get(h, "validityTime")
            valid = datetime.strptime(f"{vd}{vt:04d}", "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
            vals = {}
            for p in POINTS:
                try:
                    vals[p.slug] = float(eccodes.codes_grib_find_nearest(h, p.lat, p.lon)[0]["value"])
                except eccodes.GribInternalError:      # точка вне вырезанного района — пропускаем
                    continue
            out.append({"short": eccodes.codes_get(h, "shortName"), "level": eccodes.codes_get(h, "level"),
                        "step": str(eccodes.codes_get(h, "stepRange")), "valid": valid, "values": vals})
        finally:
            eccodes.codes_release(h)
    return out

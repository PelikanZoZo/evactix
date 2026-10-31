"""DWD MOSMIX: точечные прогнозы Немецкой метеослужбы по станциям (ICON + IFS с постобработкой).

Открытые данные DWD, коммерческое использование с указанием источника (Deutscher Wetterdienst).
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timedelta, timezone

from .base import Batch, Collector
from .weather_common import DWD_STATIONS, HORIZON, forecast_event

URL = "https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/single_stations/{st}/kml/MOSMIX_L_LATEST_{st}.kmz"
ELEMENTS = ("TTT", "FF", "DD", "RR1c", "FX3")   # FX1 вне Германии пуст — берём порывы за 3 часа


def parse_kml(xml: str) -> tuple[datetime, list[datetime], dict[str, list[float | None]]]:
    issued = datetime.fromisoformat(re.search(r"<dwd:IssueTime>([^<]+)</dwd:IssueTime>", xml).group(1).replace("Z", "+00:00"))
    steps = [datetime.fromisoformat(t.replace("Z", "+00:00"))
             for t in re.findall(r"<dwd:TimeStep>([^<]+)</dwd:TimeStep>", xml)]
    series = {}
    for name, body in re.findall(r'<dwd:Forecast dwd:elementName="(\w+)">\s*<dwd:value>([^<]*)</dwd:value>', xml):
        if name in ELEMENTS:
            series[name] = [None if v in ("-", "") else float(v) for v in body.split()]
    return issued, steps, series


class DwdMosmixCollector(Collector):
    name = "dwd.mosmix"
    source_id = "dwd.mosmix"
    interval = timedelta(hours=3)

    def parse(self, slug: str, kmz: bytes, ref: str, h: str) -> list[dict]:
        z = zipfile.ZipFile(io.BytesIO(kmz))
        issued, steps, s = parse_kml(z.read(z.namelist()[0]).decode("latin-1"))
        out = []
        ev = lambda metric, at, value, **kw: out.append(
            forecast_event("dwd", slug, metric, at, issued, value, "dwd", ref, h, **kw))
        for i, at in enumerate(steps):
            if at - issued > HORIZON:
                break
            get = lambda k: s.get(k, [None] * len(steps))[i]
            if (t := get("TTT")) is not None:
                ev("weather.air_temperature", at, {"t": "number", "v": round(t - 273.15, 2), "unit": "Cel"})
            if (ff := get("FF")) is not None:
                ev("weather.wind_speed", at, {"t": "number", "v": round(ff, 2), "unit": "m/s"})
            if (dd := get("DD")) is not None:
                ev("weather.wind_from_direction", at, {"t": "cyclic", "v": round(dd % 360.0, 1) % 360.0, "period": 360.0})
            if (fx := get("FX3")) is not None:
                ev("weather.wind_gust", at, {"t": "number", "v": round(fx, 2), "unit": "m/s"}, aggregation="max",
                   period=timedelta(hours=3))
            if (rr := get("RR1c")) is not None:   # осадки за час, закончившийся в at → время события = начало часа
                start = at - timedelta(hours=1)
                ev("weather.precipitation", start, {"t": "number", "v": round(rr, 2), "unit": "mm"}, aggregation="sum",
                   period=timedelta(hours=1), valid_from=start, valid_to=at)
        return out

    def collect(self, conn) -> Batch:
        b = Batch()
        for slug, st in DWD_STATIONS.items():
            data, ref, h = self.fetch_raw(URL.format(st=st), "kmz")
            b.events += self.parse(slug, data, ref, h)
        return b

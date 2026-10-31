"""Справочники ядра: источники, метрики, базовые сущности и опорные точки.

Сущность — только ID и тип. Координаты точки — не реквизит, а место события,
поэтому они живут здесь, в коде, и попадают в поля места каждого события.
"""
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    source_class: str
    base_confidence: float
    license: str
    url: str
    max_age: str  # интервал Postgres


SOURCES = [
    Source("cbr.fx", "ЦБ РФ: официальные курсы валют", "open", 0.98, "cbr-public-attribution",
           "https://www.cbr.ru/development/SXML/", "2 days"),
    Source("cbr.metals", "ЦБ РФ: учётные цены драгметаллов", "open", 0.98, "cbr-public-attribution",
           "https://www.cbr.ru/development/SXML/", "4 days"),
    Source("cbr.keyrate", "ЦБ РФ: ключевая ставка", "open", 0.99, "cbr-public-attribution",
           "https://www.cbr.ru/development/dws/", "4 days"),
    Source("xmlcalendar", "Производственный календарь xmlcalendar.ru", "open", 0.95, "open-project-attribution",
           "https://xmlcalendar.ru/", "8 days"),
    Source("astro", "Астрономический расчёт (ядро Evactix)", "computed", 1.0, "internal",
           "", "2 days"),
    Source("metno.forecast", "Met Norway Locationforecast", "open", 0.80, "CC-BY-4.0",
           "https://api.met.no/weatherapi/locationforecast/2.0/documentation", "6 hours"),
    Source("usgs.quakes", "USGS: землетрясения M4.5+", "open", 0.95, "public-domain",
           "https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php", "1 day"),
    Source("gdacs.alerts", "GDACS: глобальные оповещения о бедствиях", "open", 0.90, "attribution-EC-JRC-UN",
           "https://www.gdacs.org/", "1 day"),
    Source("dwd.mosmix", "DWD MOSMIX: точечные прогнозы (Deutscher Wetterdienst)", "open", 0.82, "DWD-open-data-attribution",
           "https://opendata.dwd.de/weather/local_forecasts/mos/", "12 hours"),
    Source("noaa.gfs", "NOAA GFS 0.25°: глобальная модель", "open", 0.75, "public-domain",
           "https://nomads.ncep.noaa.gov/", "18 hours"),
    Source("ecmwf.ifs", "ECMWF IFS 0.25°: открытые данные", "open", 0.85, "CC-BY-4.0",
           "https://www.ecmwf.int/en/forecasts/datasets/open-data", "18 hours"),
    Source("weather.consensus", "Погодный консенсус четырёх моделей (ядро Evactix)", "computed", 0.85, "internal",
           "", "3 hours"),
]


@dataclass(frozen=True)
class Metric:
    id: str
    value_type: str
    unit: str | None
    aggregation: str | None
    description: str


METRICS = [
    Metric("fx.rate_rub", "money", "RUB", "instant", "Официальный курс ЦБ: рублей за 1 единицу валюты"),
    Metric("metal.price_rub_g", "money", "RUB", "instant", "Учётная цена драгметалла ЦБ: рублей за грамм"),
    Metric("rate.key", "ratio", "%", "instant", "Ключевая ставка ЦБ РФ, % годовых"),
    Metric("calendar.day_type", "category", None, None, "Тип дня производственного календаря"),
    Metric("astro.moon_phase", "cyclic", "fraction", "instant", "Фаза Луны: доля синодического месяца, 0 — новолуние, 0.5 — полнолуние"),
    Metric("astro.sun_event", "category", None, None, "Солнечное событие: восход, закат"),
    Metric("astro.day_length", "duration", "s", None, "Продолжительность светового дня"),
    Metric("weather.air_temperature", "number", "Cel", "instant", "Температура воздуха, °C"),
    Metric("weather.wind_speed", "number", "m/s", "instant", "Скорость ветра, м/с"),
    Metric("weather.wind_from_direction", "cyclic", "deg", "instant", "Направление ветра (откуда), градусы"),
    Metric("weather.precipitation", "number", "mm", "sum", "Осадки за период, мм"),
    Metric("weather.wind_gust", "number", "m/s", "max", "Порывы ветра, м/с"),
    Metric("weather.precip_agreement", "ratio", "fraction", "instant", "Доля моделей, ждущих осадки ≥0.1 мм/ч"),
    Metric("weather.frost_agreement", "ratio", "fraction", "instant", "Доля моделей, ждущих температуру ≤0 °C"),
    Metric("weather.strong_wind_agreement", "ratio", "fraction", "instant", "Доля моделей, ждущих порывы ≥15 м/с"),
    Metric("quake.magnitude", "number", "Mw", "instant", "Магнитуда землетрясения"),
    Metric("hazard.alert_level", "ordinal", None, "instant", "Уровень угрозы GDACS: 1 зелёный, 2 оранжевый, 3 красный"),
]


@dataclass(frozen=True)
class Point:
    slug: str
    lat: float
    lon: float
    tz: str
    region: str


# Опорные логистические точки первой очереди
POINTS = [
    Point("moscow", 55.7558, 37.6173, "Europe/Moscow", "RU-MOW"),
    Point("saint-petersburg", 59.9386, 30.3141, "Europe/Moscow", "RU-SPE"),
    Point("novorossiysk", 44.7239, 37.7686, "Europe/Moscow", "RU-KDA"),
    Point("krasnodar", 45.0355, 38.9753, "Europe/Moscow", "RU-KDA"),
    Point("rostov-on-don", 47.2357, 39.7015, "Europe/Moscow", "RU-ROS"),
    Point("kazan", 55.7887, 49.1221, "Europe/Moscow", "RU-TA"),
    Point("yekaterinburg", 56.8389, 60.6057, "Asia/Yekaterinburg", "RU-SVE"),
    Point("novosibirsk", 55.0084, 82.9357, "Asia/Novosibirsk", "RU-NVS"),
    Point("zabaykalsk", 49.6514, 117.3270, "Asia/Chita", "RU-ZAB"),
    Point("vladivostok", 43.1155, 131.8855, "Asia/Vladivostok", "RU-PRI"),
]

COUNTRIES = ["RU", "KZ", "BY", "UZ"]
COMMODITIES = ["gold", "silver", "platinum", "palladium"]
INDICES = ["cbr_key_rate"]


def sync_catalog(conn: psycopg.Connection) -> None:
    for s in SOURCES:
        conn.execute(
            """insert into sources (id, name, source_class, base_confidence, license, url, max_age)
               values (%s,%s,%s,%s,%s,%s,%s::interval)
               on conflict (id) do update set name=excluded.name, source_class=excluded.source_class,
                 base_confidence=excluded.base_confidence, license=excluded.license,
                 url=excluded.url, max_age=excluded.max_age""",
            (s.id, s.name, s.source_class, s.base_confidence, s.license, s.url, s.max_age))
    for m in METRICS:
        conn.execute(
            """insert into metrics (id, value_type, unit, aggregation, description) values (%s,%s,%s,%s,%s)
               on conflict (id) do update set value_type=excluded.value_type, unit=excluded.unit,
                 aggregation=excluded.aggregation, description=excluded.description""",
            (m.id, m.value_type, m.unit, m.aggregation, m.description))
    ents = ([(f"point:{p.slug}", "point") for p in POINTS]
            + [(f"country:{c}", "country") for c in COUNTRIES]
            + [(f"commodity:{c}", "commodity") for c in COMMODITIES]
            + [(f"index:{i}", "index") for i in INDICES]
            + [("currency:RUB", "currency")])
    for eid, etype in ents:
        conn.execute("insert into entities (id, type) values (%s,%s) on conflict do nothing", (eid, etype))


def point(slug: str) -> Point:
    return next(p for p in POINTS if p.slug == slug)

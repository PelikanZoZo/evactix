"""Погодный ансамбль: четыре модели и консенсус."""
from datetime import datetime, timedelta, timezone

from evactix.collectors.consensus import QUERY, WeatherConsensusCollector, circular_mean, confidence, votes_from_rows
from evactix.collectors.dwd import DwdMosmixCollector
from evactix.collectors.ecmwf import EcmwfIfsCollector
from evactix.collectors.gfs import GfsCollector
from evactix.collectors.weather_common import forecast_event, wind_from_uv
from evactix.store import Store

from test_core import mock_http, run_collector

RUN = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def write(db, source, events):
    c = Store(db, source, "test", source).write_events(events)
    assert c.rejected == 0, c.reasons
    return c


def test_wind_from_uv():
    assert wind_from_uv(0.0, -5.0) == (5.0, 0.0)       # дует с севера на юг
    assert wind_from_uv(-5.0, 0.0)[1] == 90.0           # с востока
    assert circular_mean([350.0, 10.0])[0] in (0.0, 360.0 % 360.0)
    assert circular_mean([350.0, 10.0])[1] == 20.0


def test_confidence_bounds():
    assert confidence(0.0, 4, 8.0) == 0.95
    assert confidence(20.0, 4, 8.0) == 0.2
    assert confidence(2.0, 1, 8.0) < confidence(2.0, 3, 8.0)


def test_dwd_mosmix(db, raw, fixture_bytes):
    c = run_collector(db, raw, DwdMosmixCollector, {"MOSMIX_L_LATEST": fixture_bytes("mosmix_27612.kmz")})
    assert c.rejected == 0, c.reasons
    assert c.written > 1000
    metrics = {r[0] for r in db.execute("select distinct metric from events where source_id='dwd.mosmix'")}
    assert {"weather.air_temperature", "weather.wind_gust", "weather.precipitation"} <= metrics
    # осадки за час — время события в начале часа
    r = db.execute("""select occurred_at, valid_from, valid_to from events
                      where source_id='dwd.mosmix' and metric='weather.precipitation' limit 1""").fetchone()
    assert r[0] == r[1] and r[2] - r[1] == timedelta(hours=1)


def test_gfs(db, raw, fixture_bytes, monkeypatch):
    monkeypatch.setattr(GfsCollector, "pause", 0)
    monkeypatch.setattr(GfsCollector, "latest_run", lambda self, now: RUN)
    c = run_collector(db, raw, GfsCollector, {"filter_gfs_0p25": fixture_bytes("gfs_small.grib2")})
    assert c.rejected == 0, c.reasons
    rows = dict(db.execute("select metric, (value->>'v')::float from events where source_id='noaa.gfs'").fetchall())
    assert 8 < rows["weather.air_temperature"] < 9
    assert rows["weather.precipitation"] == 0.0
    # тот же прогон второй раз не качаем
    assert GfsCollector(mock_http({}), raw).collect(db).events == []


def test_ecmwf_precip_from_accumulation():
    pts = lambda v: {"moscow": v}
    fields = []
    for step, tp in ((0, 0.0), (3, 0.0012), (6, 0.0015)):
        at = RUN + timedelta(hours=step)
        fields += [{"short": "2t", "valid": at, "values": pts(273.15)},
                   {"short": "10u", "valid": at, "values": pts(0.0)},
                   {"short": "10v", "valid": at, "values": pts(-3.0)},
                   {"short": "tp", "valid": at, "values": pts(tp)}]
    evs = EcmwfIfsCollector(None, None).to_events(RUN, fields, None, None)
    pr = [(e["occurred_at"], e["value"]["v"], e["period"]) for e in evs if e["metric"] == "weather.precipitation"]
    assert pr == [(RUN, 1.2, timedelta(hours=3)), (RUN + timedelta(hours=3), 0.3, timedelta(hours=3))]


def test_consensus(db):
    at = RUN + timedelta(hours=6)
    t = {"metno": 1.0, "dwd": 2.0, "gfs": 3.0, "ecmwf": -6.0}           # одна модель «сломалась»
    d = {"metno": 350.0, "dwd": 10.0, "gfs": 0.0, "ecmwf": 5.0}
    for src, tag in (("metno.forecast", "metno"), ("dwd.mosmix", "dwd"), ("noaa.gfs", "gfs"), ("ecmwf.ifs", "ecmwf")):
        ev = [forecast_event(tag, "moscow", "weather.air_temperature", at, RUN,
                             {"t": "number", "v": t[tag], "unit": "Cel"}, tag, None, None),
              forecast_event(tag, "moscow", "weather.wind_from_direction", at, RUN,
                             {"t": "cyclic", "v": d[tag], "period": 360.0}, tag, None, None)]
        hours = 3 if tag == "ecmwf" else 1
        ev.append(forecast_event(tag, "moscow", "weather.precipitation", at, RUN,
                                 {"t": "number", "v": 0.6 if tag == "ecmwf" else (0.5 if tag != "gfs" else 0.0), "unit": "mm"},
                                 tag, None, None, aggregation="sum", period=timedelta(hours=hours),
                                 valid_from=at, valid_to=at + timedelta(hours=hours)))
        write(db, src, ev)
    rows = db.execute(QUERY, (["metno.forecast", "dwd.mosmix", "noaa.gfs", "ecmwf.ifs"], RUN, RUN + timedelta(days=3))).fetchall()
    evs = WeatherConsensusCollector(None, None).build(votes_from_rows(rows, RUN + timedelta(hours=1)), RUN + timedelta(hours=1))
    by = {e["metric"]: e for e in evs}
    temp = by["weather.air_temperature"]
    assert temp["value"]["v"] == 1.5 and temp["payload"]["n"] == 4 and temp["payload"]["v_ecmwf"] == -6.0
    assert temp["confidence"] == 0.2                     # разброс 9 °C — доверия мало
    assert by["weather.wind_from_direction"]["value"]["v"] < 5 or by["weather.wind_from_direction"]["value"]["v"] > 355
    assert by["weather.precip_agreement"]["value"]["v"] == 0.75   # ECMWF 0.6 мм за 3 ч = 0.2 мм/ч — «за»
    assert by["weather.frost_agreement"]["value"]["v"] == 0.25
    write(db, "weather.consensus", evs)
    assert write(db, "weather.consensus", evs).written == 0      # повтор без изменений не пишется

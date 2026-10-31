from datetime import datetime, timedelta, timezone

import httpx
import pytest

from evactix.collectors.astro import AstroCollector, moon_phase_fraction
from evactix.collectors.calendar import CalendarCollector
from evactix.collectors.cbr import CbrFxCollector, CbrKeyRateCollector, CbrMetalsCollector
from evactix.collectors.hazards import GdacsCollector, UsgsQuakesCollector
from evactix.collectors.metno import MetnoCollector
from evactix.model import EventIn
from evactix.store import Store, uuid7

NOW = datetime(2026, 10, 31, 19, 0, tzinfo=timezone.utc)


def mock_http(routes: dict[str, bytes]) -> httpx.Client:
    def handler(req: httpx.Request):
        for frag, body in routes.items():
            if frag in str(req.url):
                return httpx.Response(200, content=body, headers={"last-modified": "Sat, 31 Oct 2026 19:29:19 GMT"})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def base_event(**kw):
    e = dict(type="test.event", kind="fact", occurred_at=NOW, observed_at=NOW, asserted_by="test",
             metric="weather.air_temperature", value={"t": "number", "v": 5.0, "unit": "Cel"},
             dedup_key="test:1")
    e.update(kw)
    return e


# ---------- модель и валидатор

def test_uuid7_sorted():
    a, b = uuid7(), uuid7()
    assert a.version == 7 and str(a) < str(b) or a.int >> 80 <= b.int >> 80


def test_model_rejects_free_text():
    with pytest.raises(Exception):
        EventIn.model_validate(base_event(payload={"note": "свободный текст с пробелами"}))


def test_model_rejects_naive_time():
    with pytest.raises(Exception):
        EventIn.model_validate(base_event(occurred_at=datetime(2026, 1, 1)))


def test_cyclic_range():
    with pytest.raises(Exception):
        EventIn.model_validate(base_event(metric="weather.wind_from_direction",
                                          value={"t": "cyclic", "v": 360.0, "period": 360.0}))


def test_validator_rules(db):
    st = Store(db, "test", "0", "metno.forecast")
    c = st.write_events([
        base_event(dedup_key="ok"),
        base_event(dedup_key="person", subject=[{"id": "person:ivanov"}]),
        base_event(dedup_key="inn", payload={"inn": "7707083893"}),
        base_event(dedup_key="taxid", payload={"code": "770708389301"}),
        base_event(dedup_key="email", payload={"x": "a@b.ru"}),
        base_event(dedup_key="wrongtype", value={"t": "ratio", "v": 1.0, "as": "percent"}),
        base_event(dedup_key="nometric", metric="no.such", value={"t": "number", "v": 1, "unit": "x"}),
    ])
    assert (c.written, c.rejected) == (1, 6)
    assert db.execute("select count(*) from rejects").fetchone()[0] == 6
    # в журнале отказов только причина и отпечаток
    cols = [r[0] for r in db.execute("select column_name from information_schema.columns where table_name='rejects'")]
    assert set(cols) == {"id", "recorded_at", "source_id", "collector", "reason", "content_hash"}


def test_dedup_and_supersedes(db):
    st = Store(db, "test", "0", "metno.forecast")
    assert st.write_events([base_event()]).written == 1
    later = NOW + timedelta(hours=1)
    assert st.write_events([base_event(observed_at=later)]).skipped == 1     # то же содержание — не пишем
    assert st.write_events([base_event(value={"t": "number", "v": 6.0, "unit": "Cel"})]).written == 1
    rows = db.execute("select id, supersedes, num from events where dedup_key='test:1' order by recorded_at").fetchall()
    assert len(rows) == 2 and rows[1][1] == rows[0][0] and rows[1][2] == 6.0


def test_signature_chain(db):
    st = Store(db, "test", "0", "metno.forecast")
    st.write_events([base_event(dedup_key=f"k{i}") for i in range(3)])
    sigs = [r[0] for r in db.execute("select signature from events order by recorded_at, id")]
    assert len(set(sigs)) == 3
    head = db.execute("select signature from chain_heads where stream='events:metno.forecast'").fetchone()[0]
    assert head in sigs


def test_append_only_for_collector_role(db):
    dbname = db.info.dbname
    import psycopg
    with psycopg.connect(f"host=127.0.0.1 port=5432 user=collector dbname={dbname}", autocommit=True) as c:
        st = Store(c, "test", "0", "metno.forecast")
        assert st.write_events([base_event()]).written == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("update events set num = 0")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("delete from events")
    with psycopg.connect(f"host=127.0.0.1 port=5432 user=ana dbname={dbname}", autocommit=True) as a:
        assert a.execute("select count(*) from events").fetchone()[0] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            a.execute("insert into rejects (reason, content_hash) values ('x','y')")


# ---------- сборщики на сохранённых ответах

def run_collector(db, raw, cls, routes):
    col = cls(mock_http(routes), raw)
    batch = col.collect(db)
    st = Store(db, col.name, col.version, col.source_id)
    c = st.write_events(batch.events)
    c.add(st.write_plans(batch.plans))
    return c


def test_cbr_fx(db, raw, fixture_bytes):
    c = run_collector(db, raw, CbrFxCollector, {"XML_daily.asp": fixture_bytes("cbr_daily.xml")})
    assert c.rejected == 0 and c.written >= 40          # первый запуск + 30 дней истории одним и тем же ответом
    usd = db.execute("""select num, value->>'per_unit', valid_from from events
                        where metric='fx.rate_rub' and subject @> '[{"id":"currency:USD"}]'""").fetchone()
    assert usd[0] > 10 and usd[1] == "unit:USD"
    c2 = run_collector(db, raw, CbrFxCollector, {"XML_daily.asp": fixture_bytes("cbr_daily.xml")})
    assert c2.written == 0 and c2.skipped > 0


def test_cbr_metals_keyrate(db, raw, fixture_bytes):
    c = run_collector(db, raw, CbrMetalsCollector, {"xml_metall": fixture_bytes("cbr_metal.xml")})
    assert c.rejected == 0 and c.written >= 4
    c = run_collector(db, raw, CbrKeyRateCollector, {"DailyInfo": fixture_bytes("cbr_key.xml")})
    assert c.rejected == 0 and c.written >= 5
    assert db.execute("select num from events where metric='rate.key' order by occurred_at desc limit 1").fetchone()[0] > 0


def test_calendar_plans(db, raw, fixture_bytes):
    c = run_collector(db, raw, CalendarCollector, {"/ru/": fixture_bytes("cal_ru_2026.json")})
    assert c.rejected == 0 and c.written > 100
    jan9 = db.execute("select value->>'code' from plans_current where dedup_key='cal:RU:2026-01-09'").fetchone()[0]
    assert jan9 == "nonworking_transfer"
    # календарь изменился: 9 января стал рабочим → поправка плана, история сохраняется
    import json
    d = json.loads(fixture_bytes("cal_ru_2026.json"))
    d["months"][0]["days"] = d["months"][0]["days"].replace("9+,", "")
    run_collector(db, raw, CalendarCollector, {"/ru/": json.dumps(d).encode()})
    rows = db.execute("select value->>'code', revision from plans where dedup_key='cal:RU:2026-01-09' order by revision").fetchall()
    assert rows == [("nonworking_transfer", 1), ("working", 2)]


def test_astro(db, raw):
    assert abs(moon_phase_fraction(datetime(2026, 1, 3, 10, 0, tzinfo=timezone.utc)) - 0.5) < 0.03   # полнолуние 3.01.2026
    c = run_collector(db, raw, AstroCollector, {})
    assert c.rejected == 0 and c.written > 1000
    assert run_collector(db, raw, AstroCollector, {}).written == 0


def test_metno(db, raw, fixture_bytes):
    c = run_collector(db, raw, MetnoCollector, {"api.met.no": fixture_bytes("metno.json")})
    assert c.rejected == 0 and c.written > 1000
    n = db.execute("select count(distinct subject) from events where source_id='metno.forecast'").fetchone()[0]
    assert n == 10


def test_hazards(db, raw, fixture_bytes):
    c = run_collector(db, raw, UsgsQuakesCollector, {"usgs.gov": fixture_bytes("usgs.json")})
    assert c.rejected == 0 and c.written > 0
    c = run_collector(db, raw, GdacsCollector, {"gdacs.org": fixture_bytes("gdacs.xml")})
    assert c.rejected == 0, c.reasons
    assert c.written > 10
    assert db.execute("select count(*) from events where h3 is not null").fetchone()[0] > 0


def test_raw_saved(db, raw, fixture_bytes):
    run_collector(db, raw, UsgsQuakesCollector, {"usgs.gov": fixture_bytes("usgs.json")})
    ref = db.execute("select raw_ref from events limit 1").fetchone()[0]
    import gzip, os
    assert gzip.open(os.path.join(raw.root, ref)).read() == fixture_bytes("usgs.json")


def test_runner_as_collector_role(db, raw, fixture_bytes):
    import psycopg
    from evactix.runner import run_once
    with psycopg.connect(f"host=127.0.0.1 port=5432 user=collector dbname={db.info.dbname}", autocommit=True) as c:
        col = UsgsQuakesCollector(mock_http({"usgs.gov": fixture_bytes("usgs.json")}), raw)
        r = run_once(c, col)
        assert r.written > 0
    assert db.execute("select status, written from collector_runs").fetchone() == ("ok", r.written)


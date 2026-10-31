"""ЦБ РФ: официальные курсы, учётные цены драгметаллов, ключевая ставка. Только открытые публикации, без договоров."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .base import Batch, Collector

MSK = ZoneInfo("Europe/Moscow")
BASE = "https://www.cbr.ru"


def _num(s: str) -> float:
    return float(s.replace(",", ".").replace("\xa0", "").strip())


def _msk_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=MSK)


def _has_history(conn, source_id: str) -> bool:
    return conn.execute("select 1 from events where source_id=%s limit 1", (source_id,)).fetchone() is not None


class CbrFxCollector(Collector):
    name = "cbr.fx"
    source_id = "cbr.fx"
    interval = timedelta(hours=3)
    backfill_days = 30

    def parse(self, data: bytes, ref: str, h: str, now: datetime) -> list[dict]:
        root = ET.fromstring(data)
        d = datetime.strptime(root.attrib["Date"], "%d.%m.%Y").date()
        out = []
        for v in root.findall("Valute"):
            code = v.findtext("CharCode").strip().upper()
            rate = _num(v.findtext("VunitRate") or "0") or _num(v.findtext("Value")) / _num(v.findtext("Nominal"))
            out.append({
                "type": "fx.rate.published", "kind": "fact",
                "occurred_at": _msk_midnight(d), "valid_from": _msk_midnight(d),
                "observed_at": now,
                "subject": [{"id": f"currency:{code}", "role": "base"}, {"id": "currency:RUB", "role": "quote"}],
                "asserted_by": "cbr",
                "metric": "fx.rate_rub",
                "value": {"t": "money", "amount": rate, "currency": "RUB", "per": 1, "per_unit": f"unit:{code}",
                          "price_kind": "official"},
                "quality": "measured",
                "payload": {"nominal": int(_num(v.findtext("Nominal")))},
                "dedup_key": f"cbr.fx:{code}:{d.isoformat()}",
                "raw_ref": ref, "raw_hash": h,
            })
        return out

    def collect(self, conn) -> Batch:
        now = datetime.now(timezone.utc)
        b = Batch()
        urls = [f"{BASE}/scripts/XML_daily.asp"]
        if not _has_history(conn, self.source_id):
            today = now.astimezone(MSK).date()
            urls += [f"{BASE}/scripts/XML_daily.asp?date_req={(today - timedelta(days=i)):%d/%m/%Y}"
                     for i in range(1, self.backfill_days + 1)]
        for u in urls:
            data, ref, h = self.fetch_raw(u, "xml")
            b.events += self.parse(data, ref, h, now)
        return b


METALS = {"1": "gold", "2": "silver", "3": "platinum", "4": "palladium"}


class CbrMetalsCollector(Collector):
    name = "cbr.metals"
    source_id = "cbr.metals"
    interval = timedelta(hours=6)

    def parse(self, data: bytes, ref: str, h: str, now: datetime) -> list[dict]:
        root = ET.fromstring(data)
        out = []
        for r in root.findall("Record"):
            metal = METALS.get(r.attrib.get("Code"))
            if not metal:
                continue
            d = datetime.strptime(r.attrib["Date"], "%d.%m.%Y").date()
            out.append({
                "type": "metal.price.published", "kind": "fact",
                "occurred_at": _msk_midnight(d), "valid_from": _msk_midnight(d), "observed_at": now,
                "subject": [{"id": f"commodity:{metal}"}, {"id": "currency:RUB", "role": "quote"}],
                "asserted_by": "cbr",
                "metric": "metal.price_rub_g",
                "value": {"t": "money", "amount": _num(r.findtext("Buy")), "currency": "RUB", "per": 1,
                          "per_unit": "g", "price_kind": "official"},
                "quality": "measured",
                "dedup_key": f"cbr.metals:{metal}:{d.isoformat()}",
                "raw_ref": ref, "raw_hash": h,
            })
        return out

    def collect(self, conn) -> Batch:
        now = datetime.now(timezone.utc)
        today = now.astimezone(MSK).date()
        days = 7 if _has_history(conn, self.source_id) else 365
        u = (f"{BASE}/scripts/xml_metall.asp?date_req1={(today - timedelta(days=days)):%d/%m/%Y}"
             f"&date_req2={(today + timedelta(days=1)):%d/%m/%Y}")
        data, ref, h = self.fetch_raw(u, "xml")
        return Batch(events=self.parse(data, ref, h, now))


SOAP = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
<KeyRateXML xmlns="http://web.cbr.ru/"><fromDate>{d1}</fromDate><ToDate>{d2}</ToDate></KeyRateXML>
</soap:Body></soap:Envelope>"""


class CbrKeyRateCollector(Collector):
    name = "cbr.keyrate"
    source_id = "cbr.keyrate"
    interval = timedelta(hours=6)

    def parse(self, data: bytes, ref: str, h: str, now: datetime) -> list[dict]:
        root = ET.fromstring(data)
        out = []
        for kr in root.iter("KR"):
            dt = datetime.fromisoformat(kr.findtext("DT"))
            out.append({
                "type": "rate.key.published", "kind": "fact",
                "occurred_at": dt, "valid_from": dt, "observed_at": now,
                "subject": [{"id": "index:cbr_key_rate"}, {"id": "currency:RUB"}],
                "asserted_by": "cbr",
                "metric": "rate.key",
                "value": {"t": "ratio", "v": _num(kr.findtext("Rate")), "as": "percent"},
                "quality": "measured",
                "dedup_key": f"cbr.keyrate:{dt.date().isoformat()}",
                "raw_ref": ref, "raw_hash": h,
            })
        return out

    def collect(self, conn) -> Batch:
        now = datetime.now(timezone.utc)
        today = now.astimezone(MSK).date()
        days = 14 if _has_history(conn, self.source_id) else 730
        body = SOAP.format(d1=(today - timedelta(days=days)).isoformat(), d2=(today + timedelta(days=1)).isoformat())
        data, ref, h = self.fetch_raw(f"{BASE}/DailyInfoWebServ/DailyInfo.asmx", "xml", method="POST",
                                      content=body.encode(),
                                      headers={"Content-Type": "text/xml; charset=utf-8",
                                               "SOAPAction": '"http://web.cbr.ru/KeyRateXML"'})
        return Batch(events=self.parse(data, ref, h, now))

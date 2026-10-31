"""Стихийные бедствия: землетрясения USGS (M4.5+) и оповещения GDACS с уровнем угрозы.

Текстовые поля (названия мест, описания) остаются в сырых данных, в события идут только коды и числа.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .base import Batch, Collector

USGS_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
GDACS_URL = "https://www.gdacs.org/xml/rss.xml"


class UsgsQuakesCollector(Collector):
    name = "usgs.quakes"
    source_id = "usgs.quakes"
    interval = timedelta(minutes=15)

    def parse(self, data: bytes, ref: str, h: str) -> list[dict]:
        d = json.loads(data)
        out = []
        for f in d["features"]:
            pr = f["properties"]
            if pr.get("mag") is None or pr.get("type") != "earthquake":
                continue
            lon, lat, depth = f["geometry"]["coordinates"]
            at = datetime.fromtimestamp(pr["time"] / 1000, tz=timezone.utc)
            upd = datetime.fromtimestamp(pr["updated"] / 1000, tz=timezone.utc)
            out.append({
                "type": "quake.detected", "kind": "fact",
                "occurred_at": at, "observed_at": upd,
                "place": {"lat": lat, "lon": lon},
                "asserted_by": "usgs",
                "metric": "quake.magnitude", "value": {"t": "number", "v": pr["mag"], "unit": "Mw"},
                "quality": "measured",
                "payload": {"depth_km": float(depth) if depth is not None else None,
                            "mag_type": pr.get("magType") or None,
                            "status": pr.get("status") or None,
                            "tsunami": bool(pr.get("tsunami")),
                            "sig": int(pr.get("sig") or 0),
                            "usgs_id": f["id"]},
                "dedup_key": f"usgs:{f['id']}",
                "raw_ref": ref, "raw_hash": h,
            })
        return out

    def collect(self, conn) -> Batch:
        data, ref, h = self.fetch_raw(USGS_URL, "json")
        return Batch(events=self.parse(data, ref, h))


NS = {"gdacs": "http://www.gdacs.org", "georss": "http://www.georss.org/georss"}
LEVEL = {"green": 1, "orange": 2, "red": 3}


class GdacsCollector(Collector):
    name = "gdacs.alerts"
    source_id = "gdacs.alerts"
    interval = timedelta(minutes=30)

    def parse(self, data: bytes, ref: str, h: str) -> list[dict]:
        root = ET.fromstring(data)
        out = []
        for it in root.find("channel").findall("item"):
            g = lambda tag: (it.findtext(f"gdacs:{tag}", namespaces=NS) or "").strip()
            level = LEVEL.get(g("alertlevel").lower())
            etype, eid, episode = g("eventtype"), g("eventid"), g("episodeid") or "0"
            if level is None or not etype or not eid:
                continue
            pt = (it.findtext("georss:point", namespaces=NS) or "").split()
            lat, lon = (float(pt[0]), float(pt[1])) if len(pt) == 2 else (None, None)
            start = parsedate_to_datetime(g("fromdate")) if g("fromdate") else None
            end = parsedate_to_datetime(g("todate")) if g("todate") else None
            modified = parsedate_to_datetime(g("datemodified")) if g("datemodified") else datetime.now(timezone.utc)
            sev = it.find("gdacs:severity", NS)
            iso3 = [c for c in g("iso3").replace(" ", "").split(",") if c][:3]
            payload = {"event_type": etype, "gdacs_id": f"{etype}{eid}", "episode": int(episode),
                       "current": g("iscurrent").lower() == "true"}
            if sev is not None and sev.attrib.get("value"):
                try:
                    payload["severity_value"] = float(sev.attrib["value"])
                    unit = sev.attrib.get("unit") or ""
                    payload["severity_unit"] = unit if re.fullmatch(r"[A-Za-z0-9_./%\-]{1,16}", unit) else None
                except ValueError:
                    pass
            for i, c in enumerate(iso3):
                payload[f"iso3_{i + 1}"] = c
            out.append({
                "type": "hazard.alert", "kind": "fact",
                "occurred_at": start or modified, "valid_from": start,
                "valid_to": end if end and start and end >= start else None,
                "observed_at": modified,
                "place": {"lat": lat, "lon": lon} if lat is not None else None,
                "asserted_by": "gdacs",
                "metric": "hazard.alert_level", "value": {"t": "ordinal", "level": level, "scale_max": 3},
                "quality": "estimated",
                "payload": payload,
                "dedup_key": f"gdacs:{etype}{eid}",
                "raw_ref": ref, "raw_hash": h,
            })
        return out

    def collect(self, conn) -> Batch:
        data, ref, h = self.fetch_raw(GDACS_URL, "xml")
        return Batch(events=self.parse(data, ref, h))

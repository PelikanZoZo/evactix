"""Расчётный слой: фаза Луны, восход, закат и долгота дня по опорным точкам. Внешних API нет, доверие 1."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun

from ..catalog import POINTS
from .base import Batch, Collector

SYNODIC = 29.530588853
NEW_MOON_JD = 2451550.1   # опорное новолуние 6 января 2000


def moon_phase_fraction(dt: datetime) -> float:
    jd = dt.timestamp() / 86400.0 + 2440587.5
    return ((jd - NEW_MOON_JD) / SYNODIC) % 1.0


class AstroCollector(Collector):
    name = "astro"
    source_id = "astro"
    version = "0.1"
    interval = timedelta(hours=12)
    moon_days = 400
    sun_days = 30

    def collect(self, conn) -> Batch:
        now = datetime.now(timezone.utc)
        today = now.date()
        b = Batch()
        for i in range(self.moon_days):
            d = today + timedelta(days=i)
            at = datetime.combine(d, time(12), tzinfo=timezone.utc)
            f = round(moon_phase_fraction(at), 4) % 1.0
            b.events.append({
                "type": "astro.moon_phase", "kind": "forecast",
                "occurred_at": at, "observed_at": datetime.combine(today, time(0), tzinfo=timezone.utc),
                "asserted_by": "evactix-astro", "confidence": 1.0,
                "metric": "astro.moon_phase", "value": {"t": "cyclic", "v": f, "period": 1.0},
                "quality": "computed",
                "dedup_key": f"astro:moon:{d.isoformat()}",
            })
        for p in POINTS:
            loc = LocationInfo(p.slug, p.region, p.tz, p.lat, p.lon)
            place = {"lat": p.lat, "lon": p.lon, "region": p.region, "tz": p.tz}
            for i in range(self.sun_days):
                d = today + timedelta(days=i)
                try:
                    s = sun(loc.observer, date=d, tzinfo=ZoneInfo(p.tz))
                except ValueError:   # полярный день/ночь
                    continue
                for ev in ("sunrise", "sunset"):
                    b.events.append({
                        "type": f"astro.{ev}", "kind": "forecast",
                        "occurred_at": s[ev].astimezone(timezone.utc),
                        "observed_at": datetime.combine(today, time(0), tzinfo=timezone.utc),
                        "place": place, "subject": [{"id": f"point:{p.slug}"}],
                        "asserted_by": "evactix-astro", "confidence": 1.0,
                        "metric": "astro.sun_event", "value": {"t": "category", "code": ev, "dictionary": "astro"},
                        "quality": "computed",
                        "dedup_key": f"astro:{ev}:{p.slug}:{d.isoformat()}",
                    })
                b.events.append({
                    "type": "astro.day_length", "kind": "forecast",
                    "occurred_at": s["noon"].astimezone(timezone.utc),
                    "valid_from": s["sunrise"].astimezone(timezone.utc), "valid_to": s["sunset"].astimezone(timezone.utc),
                    "observed_at": datetime.combine(today, time(0), tzinfo=timezone.utc),
                    "place": place, "subject": [{"id": f"point:{p.slug}"}],
                    "asserted_by": "evactix-astro", "confidence": 1.0,
                    "metric": "astro.day_length",
                    "value": {"t": "duration", "seconds": round((s["sunset"] - s["sunrise"]).total_seconds())},
                    "quality": "computed",
                    "dedup_key": f"astro:daylen:{p.slug}:{d.isoformat()}",
                })
        return b

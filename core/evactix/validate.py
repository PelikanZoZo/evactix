"""Валидатор на входе. Нарушение запретов — запись не попадает в ядро, а уходит в журнал отказов.

Запреты (раздел «Правила и запреты» ТЗ):
- нет персональных данных, физлиц и ИП; ядро работает только с ID;
- нет свободного текста в содержимом (обеспечивает шаблон Code в модели);
- метрика и тип значения — только из справочника.
"""
from __future__ import annotations

import re

from .model import EventIn, PlanIn

ALLOWED_ENTITY_TYPES = {"currency", "country", "point", "commodity", "index", "org",
                        "route", "warehouse", "cargo", "region"}

# Ключи, которые сами по себе говорят о реквизитах или личных данных
FORBIDDEN_KEYS = {"inn", "ogrn", "ogrnip", "kpp", "snils", "passport", "phone", "email", "mail",
                  "name", "fio", "first_name", "last_name", "surname", "address", "addr",
                  "birth", "birthday", "person", "driver", "card", "account"}

EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", re.I)
PHONE_RE = re.compile(r"^\+?[78]?[\d\-() ]{10,}$")
TAXID_RE = re.compile(r"^\d{10}$|^\d{12}$|^\d{13}$|^\d{15}$")   # ИНН/ОГРН/ОГРНИП


class Rejected(Exception):
    pass


def _scan_scalar(where: str, v) -> None:
    if isinstance(v, bool) or v is None:
        return
    s = str(v)
    if isinstance(v, str) and EMAIL_RE.search(s):
        raise Rejected(f"{where}: похоже на email")
    if isinstance(v, str) and PHONE_RE.match(s):
        raise Rejected(f"{where}: похоже на телефон")
    if isinstance(v, (int, str)) and TAXID_RE.match(s):
        raise Rejected(f"{where}: похоже на ИНН/ОГРН — реквизиты хранятся вне ядра")


def _check_payload(where: str, d: dict | None) -> None:
    for k, v in (d or {}).items():
        if k.lower() in FORBIDDEN_KEYS or any(k.lower().startswith(f + "_") for f in FORBIDDEN_KEYS):
            raise Rejected(f"{where}.{k}: запрещённый ключ")
        _scan_scalar(f"{where}.{k}", v)


def _check_subject(subject) -> None:
    for s in subject:
        etype = s.id.split(":", 1)[0] if ":" in s.id else ""
        if etype not in ALLOWED_ENTITY_TYPES:
            raise Rejected(f"subject {s.id}: тип сущности не разрешён (физлица и ИП не храним)")
        _scan_scalar(f"subject {s.id}", s.id.split(":", 1)[1])


def check_event(ev: EventIn, metrics: dict[str, str]) -> None:
    """metrics: id метрики → тип значения из справочника."""
    _check_subject(ev.subject)
    _check_payload("payload", ev.payload)
    if ev.metric is not None:
        vt = metrics.get(ev.metric)
        if vt is None:
            raise Rejected(f"метрика {ev.metric} не в справочнике")
        if ev.value.t != vt:
            raise Rejected(f"метрика {ev.metric}: ожидается тип {vt}, пришёл {ev.value.t}")


def check_plan(p: PlanIn, metrics: dict[str, str]) -> None:
    _check_subject(p.subject)
    _check_payload("tolerance", p.tolerance)
    if p.metric is not None:
        vt = metrics.get(p.metric)
        if vt is None:
            raise Rejected(f"метрика {p.metric} не в справочнике")
        if p.value is not None and p.value.t != vt:
            raise Rejected(f"метрика {p.metric}: ожидается тип {vt}, пришёл {p.value.t}")

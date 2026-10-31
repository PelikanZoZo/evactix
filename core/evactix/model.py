"""Модель события и плана. Поля и смысл — по «ТЗ ядра Evactix: модель данных»."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Code = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:\-+/]+$")]


class _V(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def main_number(self) -> float | None:
        return None


class Number(_V):
    t: Literal["number"] = "number"
    v: float
    unit: Code

    def main_number(self):
        return self.v


class Money(_V):
    t: Literal["money"] = "money"
    amount: float
    currency: Code
    per: float = 1.0          # за сколько единиц
    per_unit: Code            # 'unit:USD', 'g', 't'
    price_kind: Literal["exchange", "wholesale", "retail", "announced", "deal", "official"] = "official"
    vat: Literal["incl", "excl", "na"] = "na"

    def main_number(self):
        return self.amount / self.per


class Ratio(_V):
    t: Literal["ratio"] = "ratio"
    v: float
    as_: Literal["percent", "fraction"] = Field("percent", alias="as")
    base: Code | None = None  # от чего считается (для скидки)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    def main_number(self):
        return self.v


class Category(_V):
    t: Literal["category"] = "category"
    code: Code
    dictionary: Code

    def main_number(self):
        return None


class Ordinal(_V):
    t: Literal["ordinal"] = "ordinal"
    level: int
    scale_max: int

    @model_validator(mode="after")
    def _range(self):
        if not 0 <= self.level <= self.scale_max:
            raise ValueError("уровень вне шкалы")
        return self

    def main_number(self):
        return float(self.level)


class Bool3(_V):
    t: Literal["bool3"] = "bool3"
    v: Literal["yes", "no", "unknown"]   # «не известно» ≠ «нет»

    def main_number(self):
        return {"yes": 1.0, "no": 0.0}.get(self.v)


class Cyclic(_V):
    t: Literal["cyclic"] = "cyclic"
    v: float
    period: float               # 360 для градусов, 1.0 для доли цикла

    @model_validator(mode="after")
    def _range(self):
        if not 0 <= self.v < self.period:
            raise ValueError("циклическое значение вне [0, period)")
        return self

    def main_number(self):
        return self.v


class Duration(_V):
    t: Literal["duration"] = "duration"
    seconds: float

    def main_number(self):
        return self.seconds


class Range(_V):
    t: Literal["range"] = "range"
    low: float
    high: float
    unit: Code

    @model_validator(mode="after")
    def _order(self):
        if self.low > self.high:
            raise ValueError("low > high")
        return self

    def main_number(self):
        return (self.low + self.high) / 2


class OHLC(_V):
    t: Literal["ohlc"] = "ohlc"
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    unit: Code

    def main_number(self):
        return self.close


Value = Annotated[Union[Number, Money, Ratio, Category, Ordinal, Bool3, Cyclic, Duration, Range, OHLC],
                  Field(discriminator="t")]


class SubjectRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: Code
    role: Code | None = None


class Place(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    lat: float | None = None
    lon: float | None = None
    region: Code | None = None
    tz: Code | None = None

    @field_validator("lat")
    @classmethod
    def _lat(cls, v):
        if v is not None and not -90 <= v <= 90:
            raise ValueError("широта вне диапазона")
        return v

    @field_validator("lon")
    @classmethod
    def _lon(cls, v):
        if v is not None and not -180 <= v <= 180:
            raise ValueError("долгота вне диапазона")
        return v


PayloadScalar = Union[bool, int, float, Code, None]


class EventIn(BaseModel):
    """То, что сборщик отдаёт ядру. Служебные поля (id, подпись, хэши) ядро заполняет само."""
    model_config = ConfigDict(extra="forbid")

    type: Code
    kind: Literal["fact", "forecast"]
    occurred_at: datetime
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    observed_at: datetime
    place: Place | None = None
    subject: list[SubjectRef] = []
    asserted_by: Code
    confidence: float | None = None       # если не задано — базовое доверие источника
    metric: Code | None = None
    value: Value | None = None
    aggregation: Literal["instant", "sum", "mean", "min", "max", "last"] | None = None
    period: timedelta | None = None
    precision: float | None = None
    quality: Literal["measured", "computed", "estimated", "imputed"] | None = None
    payload: dict[Code, PayloadScalar] = {}
    chain_id: str | None = None
    plan_id: str | None = None
    caused_by: str | None = None
    dedup_key: Annotated[str, Field(min_length=1, max_length=300)]   # естественный ключ «то же самое событие»
    raw_ref: str | None = None
    raw_hash: str | None = None
    tenant: Code = "public"

    @model_validator(mode="after")
    def _checks(self):
        for name in ("occurred_at", "observed_at", "valid_from", "valid_to"):
            v = getattr(self, name)
            if v is not None and v.tzinfo is None:
                raise ValueError(f"{name}: время без часового пояса")
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("valid_from > valid_to")
        if (self.metric is None) != (self.value is None):
            raise ValueError("metric и value задаются вместе")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence вне [0,1]")
        if self.value is not None:
            n = self.value.main_number()
            if n is not None and (math.isnan(n) or math.isinf(n)):
                raise ValueError("значение NaN/inf")
        return self


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parameter: Literal["moment", "duration", "price", "ratio", "size", "category"]
    metric: Code | None = None
    value: Value | None = None
    window_from: datetime | None = None
    window_to: datetime | None = None
    tolerance: dict[Code, PayloadScalar] | None = None
    subject: list[SubjectRef] = []
    target_at: datetime
    basis: Literal["schedule", "contract", "tariff", "announcement", "norm"]
    asserted_by: Code
    announced_at: datetime | None = None
    dedup_key: Annotated[str, Field(min_length=1, max_length=300)]
    raw_ref: str | None = None
    raw_hash: str | None = None
    tenant: Code = "public"

    @model_validator(mode="after")
    def _checks(self):
        if self.target_at.tzinfo is None:
            raise ValueError("target_at без часового пояса")
        if self.value is None and self.window_from is None:
            raise ValueError("у плана нет ни значения, ни окна")
        return self

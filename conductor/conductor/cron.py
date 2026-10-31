"""Cron из 5 полей (минута час день месяц день_недели) по московскому времени, и описание словами.

Поддерживает: *, */n, a-b, a-b/n, списки через запятую. День недели 0–7 (0 и 7 — воскресенье).
Если ограничены и день месяца, и день недели — срабатывает при совпадении любого (как в классическом cron).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
BOUNDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


class CronError(ValueError):
    pass


def _field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise CronError(f"шаг «{s}»")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            if not (x.isdigit() and y.isdigit()):
                raise CronError(f"диапазон «{part}»")
            a, b = int(x), int(y)
        elif part.isdigit():
            a = b = int(part)
            if step > 1:
                b = hi
        else:
            raise CronError(f"значение «{part}»")
        if a < lo or b > hi or a > b:
            raise CronError(f"«{part}» вне {lo}–{hi}")
        out.update(range(a, b + 1, step))
    return out


class Cron:
    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) != 5:
            raise CronError("нужно 5 полей: минута час день месяц день_недели")
        self.expr = expr
        self.min, self.hour, self.dom, self.mon, dow = (_field(p, *b) for p, b in zip(parts, BOUNDS))
        self.dow = {d % 7 for d in dow}
        self.dom_any = parts[2] == "*"
        self.dow_any = parts[4] == "*"

    def _day_ok(self, d: datetime) -> bool:
        if d.month not in self.mon:
            return False
        dom_ok = d.day in self.dom
        dow_ok = (d.isoweekday() % 7) in self.dow
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok

    def next_after(self, after: datetime) -> datetime:
        """Следующий момент строго после after (UTC на входе и выходе), расчёт по Москве."""
        t = after.astimezone(MSK).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(366 * 2):
            if self._day_ok(t):
                for h in sorted(self.hour):
                    if h < t.hour:
                        continue
                    for m in sorted(self.min):
                        if h == t.hour and m < t.minute:
                            continue
                        return t.replace(hour=h, minute=m).astimezone(timezone.utc)
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
        raise CronError("расписание не срабатывает в ближайшие 2 года")


DOW_RU = {1: "понедельникам", 2: "вторникам", 3: "средам", 4: "четвергам", 5: "пятницам", 6: "субботам", 0: "воскресеньям"}


def describe(expr: str) -> str:
    """«по понедельникам в 11:00», «по будням в 09:00», «каждый день в 08:30», «1-го числа в 10:00»."""
    try:
        c = Cron(expr)
    except CronError:
        return expr
    parts = expr.split()
    times = ""
    if len(c.hour) == 1 and len(c.min) == 1:
        times = f" в {min(c.hour):02d}:{min(c.min):02d}"
    elif parts[0].startswith("*/") and parts[1] == "*":
        return f"каждые {parts[0][2:]} мин"
    elif len(c.min) == 1 and parts[1].startswith("*/"):
        return f"каждые {parts[1][2:]} ч"
    else:
        return f"по расписанию «{expr}»"
    if c.dom_any and c.dow_any:
        return "каждый день" + times
    if c.dom_any:
        if c.dow == {1, 2, 3, 4, 5}:
            return "по будням" + times
        if c.dow == {0, 6}:
            return "по выходным" + times
        return "по " + ", ".join(DOW_RU[d] for d in sorted(c.dow, key=lambda x: (x == 0, x))) + times
    if c.dow_any and len(c.dom) == 1:
        return f"{min(c.dom)}-го числа" + times
    return f"по расписанию «{expr}»"

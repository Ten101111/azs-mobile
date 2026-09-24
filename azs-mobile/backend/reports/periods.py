"""Периоды справки: неделя пн–вс, прошлая неделя и та же неделя прошлого года.

Прошлый год — сдвиг на 364 дня (52 недели), а не «то же число»: так совпадают
дни недели, и выходные сравниваются с выходными (требование БТ-Р4).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")
MONTHS_NOM = ("январь", "февраль", "март", "апрель", "май", "июнь",
              "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")
YEAR_SHIFT_DAYS = 364

# Нерабочие праздничные дни РФ по Трудовому кодексу (ст. 112). Переносы
# выходных меняются каждый год постановлением правительства — их здесь нет:
# справка честно помечает праздник, а не угадывает перенос.
HOLIDAYS = {
    (1, 1): "Новогодние каникулы", (1, 2): "Новогодние каникулы", (1, 3): "Новогодние каникулы",
    (1, 4): "Новогодние каникулы", (1, 5): "Новогодние каникулы", (1, 6): "Новогодние каникулы",
    (1, 7): "Рождество Христово", (1, 8): "Новогодние каникулы",
    (2, 23): "День защитника Отечества", (3, 8): "Международный женский день",
    (5, 1): "Праздник Весны и Труда", (5, 9): "День Победы", (6, 12): "День России",
    (11, 4): "День народного единства",
}


@dataclass(frozen=True)
class Week:
    start: date   # понедельник
    end: date     # воскресенье

    @property
    def iso(self) -> str:
        year, week, _ = self.start.isocalendar()
        return f"{year}-W{week:02d}"

    @property
    def label(self) -> str:
        """«7–13 сентября 2026», «29 сентября – 5 октября 2026», «29 декабря 2025 – 4 января 2026»."""
        s, e = self.start, self.end
        if s.year != e.year:
            return f"{s.day} {MONTHS_GEN[s.month - 1]} {s.year} – {e.day} {MONTHS_GEN[e.month - 1]} {e.year}"
        if s.month != e.month:
            return f"{s.day} {MONTHS_GEN[s.month - 1]} – {e.day} {MONTHS_GEN[e.month - 1]} {e.year}"
        return f"{s.day}–{e.day} {MONTHS_GEN[e.month - 1]} {e.year}"

    @property
    def short(self) -> str:
        """«07.09–13.09» — подпись точки на графике."""
        return f"{self.start:%d.%m}–{self.end:%d.%m}"

    def shift(self, days: int) -> "Week":
        return Week(self.start + timedelta(days=days), self.end + timedelta(days=days))

    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(7)]

    def holidays(self) -> list[dict]:
        return [{"date": d.isoformat(), "name": HOLIDAYS[(d.month, d.day)]}
                for d in self.days() if (d.month, d.day) in HOLIDAYS]

    def as_dict(self) -> dict:
        return {"iso": self.iso, "from": self.start.isoformat(), "to": self.end.isoformat(),
                "label": self.label, "short": self.short}


def week_of(day: date) -> Week:
    start = day - timedelta(days=day.weekday())
    return Week(start, start + timedelta(days=6))


def last_complete_week(today: date) -> Week:
    """Последняя завершённая неделя пн–вс: в понедельник это вчерашняя неделя."""
    return week_of(today).shift(-7)


def previous(week: Week) -> Week:
    return week.shift(-7)


def last_year(week: Week) -> Week:
    return week.shift(-YEAR_SHIFT_DAYS)


def trailing(week: Week, count: int = 8) -> list[Week]:
    """Последние `count` недель, заканчивая отчётной, от старой к новой."""
    return [week.shift(-7 * i) for i in range(count - 1, -1, -1)]


def parse_iso(value: str) -> Week:
    """«2026-W37» → неделя."""
    year, week = value.split("-W")
    start = date.fromisocalendar(int(year), int(week), 1)
    return Week(start, start + timedelta(days=6))


def month_bounds(day: date) -> tuple[date, date]:
    """Первый и последний день месяца, в который попадает `day`."""
    start = day.replace(day=1)
    following = (start + timedelta(days=32)).replace(day=1)
    return start, following - timedelta(days=1)


def months_of(week: "Week") -> list[tuple[date, date]]:
    """Месяцы, которых касается неделя: один или два (переходная неделя)."""
    first = month_bounds(week.start)
    last = month_bounds(week.end)
    return [first] if first == last else [first, last]


def month_label(day: date, case: str = "nom") -> str:
    names = MONTHS_GEN if case == "gen" else MONTHS_NOM
    return f"{names[day.month - 1]} {day.year}"

"""Дата съёмки из имени файла, день съёмки, названия и описания роликов."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

# VID_20261001_180500_00_001.mp4 — дата и местное время камеры в начале имени.
FILENAME_RE = re.compile(r"^VID_(\d{8})_(\d{6})", re.IGNORECASE)

DEFAULT_DAY_START = time(4, 0)

KINDS = {"training": "Тренировка", "lesson": "Занятие", "show": "Шоу", "masterclass": "Мастер-класс"}


def parse_start(filename: str) -> datetime | None:
    """Время начала клипа из имени файла или None, если имя не по шаблону."""
    m = FILENAME_RE.match(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def shooting_day(start: datetime, day_start: time = DEFAULT_DAY_START) -> date:
    """День съёмки: съёмка до day_start (по умолчанию 04:00) относится к предыдущему дню."""
    shift = timedelta(hours=day_start.hour, minutes=day_start.minute)
    return (start - shift).date()


def title(kind: str, day: date, part: int = 1) -> str:
    """«Тренировка 01.10.2026», для второго ролика за день — «… (часть 2)»."""
    if kind not in KINDS:
        raise ValueError(f"Неизвестный тип ролика: {kind!r}")
    text = f"{KINDS[kind]} {day:%d.%m.%Y}"
    if part > 1:
        text += f" (часть {part})"
    return text


def description(start: datetime, end: datetime) -> str:
    """«Снято 01.10.2026, 18:05–20:40»; если съёмка перешла через полночь — с обеими датами."""
    if start.date() == end.date():
        return f"Снято {start:%d.%m.%Y}, {start:%H:%M}–{end:%H:%M}"
    return f"Снято {start:%d.%m.%Y %H:%M} – {end:%d.%m.%Y %H:%M}"

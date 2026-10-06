"""Склейка кусков одной записи и разбиение записей по дням съёмки."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .naming import DEFAULT_DAY_START, shooting_day
from .probe import Media
from .scan import Clip

# Ace Pro 2 режет длинную запись на файлы с ОДНИМ И ТЕМ ЖЕ временем начала в имени
# (VID_20260801_134501_00_585.mp4, …_586.mp4, …_587.mp4) — это главный признак.
# Запасной признак: следующий файл начинается не позже чем через 2 с после конца записи.
MAX_GAP_SECONDS = 2.0


@dataclass
class Part:
    clip: Clip
    media: Media


@dataclass
class Recording:
    parts: list[Part]

    @property
    def start(self) -> datetime:
        return self.parts[0].clip.start

    @property
    def duration(self) -> float:
        return sum(p.media.duration for p in self.parts)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration)


def group_recordings(parts: list[Part], max_gap: float = MAX_GAP_SECONDS) -> list[Recording]:
    """Куски с тем же временем начала или вплотную после конца записи — одна запись."""
    recordings: list[Recording] = []
    for part in sorted(parts, key=lambda p: (p.clip.start, p.clip.name)):
        if recordings:
            rec = recordings[-1]
            gap = (part.clip.start - rec.end).total_seconds()
            if part.clip.start == rec.start or gap <= max_gap:
                recordings[-1].parts.append(part)
                continue
        recordings.append(Recording(parts=[part]))
    return recordings


def group_days(
    recordings: list[Recording], day_start: time = DEFAULT_DAY_START
) -> dict[date, list[Recording]]:
    """Запись целиком относится к дню своего начала."""
    days: dict[date, list[Recording]] = defaultdict(list)
    for rec in recordings:
        days[shooting_day(rec.start, day_start)].append(rec)
    return dict(sorted(days.items()))

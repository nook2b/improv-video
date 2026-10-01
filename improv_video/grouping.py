"""Склейка кусков одной записи и разбиение записей по дням съёмки."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .naming import DEFAULT_DAY_START, shooting_day
from .probe import Media
from .scan import Clip

# Камера режет длинную запись на файлы; следующий кусок начинается сразу после предыдущего.
# Время в имени — с точностью до секунды, поэтому допускаем небольшой «нахлёст».
MAX_GAP_SECONDS = 2.0


@dataclass
class Part:
    clip: Clip
    media: Media

    @property
    def end(self) -> datetime:
        return self.clip.start + timedelta(seconds=self.media.duration)


@dataclass
class Recording:
    parts: list[Part]

    @property
    def start(self) -> datetime:
        return self.parts[0].clip.start

    @property
    def end(self) -> datetime:
        return self.parts[-1].end

    @property
    def duration(self) -> float:
        return sum(p.media.duration for p in self.parts)


def group_recordings(parts: list[Part], max_gap: float = MAX_GAP_SECONDS) -> list[Recording]:
    """Соседние клипы с разрывом не больше max_gap секунд — одна запись."""
    recordings: list[Recording] = []
    for part in sorted(parts, key=lambda p: (p.clip.start, p.clip.name)):
        if recordings:
            last = recordings[-1].parts[-1]
            gap = (part.clip.start - last.end).total_seconds()
            if gap <= max_gap:
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

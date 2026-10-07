"""Прогресс сборки дня: этапы, общий процент, оставшееся время, «нет прогресса»."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

# Этап → (название в меню, доля времени сборки). Доли — грубая оценка по замерам на Ace Pro 2.
STAGES: dict[str, tuple[str, float]] = {
    "measure": ("Замер и автоцвет", 0.15),
    "encode": ("Кодирование", 0.70),
    "audio": ("Звук", 0.10),
    "mux": ("Склейка", 0.05),
}
STALL_SECONDS = 120  # столько без продвижения — «Нет прогресса — проверьте флешку»
MIN_ETA_SECONDS = 20  # раньше оценка времени слишком шумная


@dataclass
class StageView:
    key: str
    title: str
    state: str  # done | active | pending
    fraction: float
    seconds: float  # сколько длился (для done) или идёт (для active)


@dataclass
class DayProgress:
    """Прогресс сборки одного дня. Обновляется из рабочих потоков, читается меню."""

    label: str  # «Тренировка 06.10.2026» или «06.10.2026», пока тип не выбран
    index: int = 1  # день index из total
    total: int = 1
    reading_card: bool = False  # клипы читаются с флешки — её нельзя вынимать
    day: date | None = None
    clock: Callable[[], float] = time.monotonic
    _fractions: dict[str, float] = field(default_factory=dict)
    _started: dict[str, float] = field(default_factory=dict)
    _finished: dict[str, float] = field(default_factory=dict)
    _stage: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        now = self.clock()
        self._t0 = now
        self._last_change = now

    def start(self, stage: str) -> None:
        with self._lock:
            now = self.clock()
            if self._stage and self._stage not in self._finished:
                self._fractions[self._stage] = 1.0
                self._finished[self._stage] = now
            self._stage = stage
            self._started.setdefault(stage, now)
            self._fractions.setdefault(stage, 0.0)
            self._last_change = now

    def set(self, stage: str, fraction: float) -> None:
        fraction = min(1.0, max(0.0, fraction))
        with self._lock:
            if fraction > self._fractions.get(stage, 0.0) + 1e-4:
                self._fractions[stage] = fraction
                self._last_change = self.clock()

    def finish(self) -> None:
        with self._lock:
            now = self.clock()
            for key in STAGES:
                self._fractions[key] = 1.0
                self._started.setdefault(key, now)
                self._finished.setdefault(key, now)
            self._stage = None
            self._last_change = now

    # ---------- для меню ----------

    @property
    def stage(self) -> str | None:
        return self._stage

    def overall(self) -> float:
        with self._lock:
            return sum(w * self._fractions.get(k, 0.0) for k, (_, w) in STAGES.items())

    def elapsed(self) -> float:
        return self.clock() - self._t0

    def eta_seconds(self) -> float | None:
        done, spent = self.overall(), self.elapsed()
        if done < 0.02 or spent < MIN_ETA_SECONDS or done >= 1:
            return None
        return spent / done * (1 - done)

    def stalled_for(self) -> float:
        """Сколько секунд прогресс не двигался (0, если двигался недавно)."""
        idle = self.clock() - self._last_change
        return idle if idle >= STALL_SECONDS else 0.0

    def stages(self) -> list[StageView]:
        now = self.clock()
        out = []
        with self._lock:
            for key, (title, _) in STAGES.items():
                if key in self._finished:
                    out.append(StageView(key, title, "done", 1.0, self._finished[key] - self._started[key]))
                elif key == self._stage:
                    out.append(StageView(key, title, "active", self._fractions.get(key, 0.0),
                                         now - self._started[key]))
                else:
                    out.append(StageView(key, title, "pending", 0.0, 0.0))
        return out

    def summary(self) -> str:
        """Одна строка для меню: «06.10.2026 · Кодирование 61% · всего 42% · ~18 мин»."""
        parts = [self.label + (f" (день {self.index} из {self.total})" if self.total > 1 else "")]
        active = next((s for s in self.stages() if s.state == "active"), None)
        if active:
            parts.append(f"{active.title} {active.fraction:.0%}")
        parts.append(f"всего {self.overall():.0%}")
        if self.stalled_for():
            parts.append(f"без изменений {minutes(self.stalled_for())}")
        elif (eta := self.eta_seconds()) is not None:
            parts.append(f"осталось ~{minutes(eta)}")
        return " · ".join(parts)


class Meter:
    """Сумма обработанных секунд по нескольким параллельным процессам ffmpeg → доля этапа."""

    def __init__(self, progress: DayProgress | None, stage: str, total_seconds: float):
        self.progress, self.stage = progress, stage
        self.total = max(total_seconds, 1e-6)
        self._done: dict[object, float] = {}
        self._lock = threading.Lock()

    def track(self, key: object, offset: float = 0.0) -> Callable[[float], None] | None:
        """Колбэк для tools.run(progress=…): key — один процесс, offset — уже учтённые секунды."""
        if self.progress is None:
            return None

        def update(seconds: float) -> None:
            with self._lock:
                self._done[key] = offset + seconds
                total = sum(self._done.values())
            self.progress.set(self.stage, total / self.total)

        return update

    def add(self, key: object, seconds: float) -> None:
        """Отметить кусок работы целиком выполненным (без колбэка ffmpeg)."""
        if self.progress is None:
            return
        with self._lock:
            self._done[key] = seconds
            total = sum(self._done.values())
        self.progress.set(self.stage, total / self.total)


def minutes(seconds: float) -> str:
    m = round(seconds / 60)
    if m < 1:
        return "меньше минуты"
    if m < 60:
        return f"{m} мин"
    return f"{m // 60} ч {m % 60:02d} мин"

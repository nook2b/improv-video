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
        """Оценка по скорости текущего этапа: сколько он ещё займёт плюс остальные этапы по их долям.

        Не по общему проценту: замер теперь идёт минуту, а весит 15% — по общему проценту
        оценка в начале кодирования выходила вдвое меньше и потом всё росла.
        """
        done, spent = self.overall(), self.elapsed()
        if done < 0.02 or spent < MIN_ETA_SECONDS or done >= 1:
            return None
        now = self.clock()
        with self._lock:
            stage = self._stage
            f = self._fractions.get(stage, 0.0) if stage else 0.0
            ran = now - self._started[stage] if stage else 0.0
            pending = sum(w for k, (_, w) in STAGES.items() if k != stage and k not in self._finished)
        if stage is None or f < 0.02 or ran < MIN_ETA_SECONDS:
            return spent / done * (1 - done)
        stage_total = ran / f
        return stage_total * (1 - f) + stage_total * pending / STAGES[stage][1]

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

    def __init__(self, progress: DayProgress | None, stage: str, total_seconds: float,
                 clock: Callable[[], float] = time.monotonic):
        self.progress, self.stage = progress, stage
        self.total = max(total_seconds, 1e-6)
        self.clock = clock
        self._done: dict[object, float] = {}
        self._lock = threading.Lock()
        self._sum = 0.0
        self.last_change = clock()
        self._running: dict[object, dict] = {}  # процесс → подпись, начало, последний блок ffmpeg -progress

    def _set(self, key: object, seconds: float) -> None:
        with self._lock:
            self._done[key] = seconds
            total = sum(self._done.values())
            if total > self._sum + 0.05:
                self._sum, self.last_change = total, self.clock()
        if self.progress is not None:
            self.progress.set(self.stage, total / self.total)

    def watch(self, key: object, label: str) -> Callable[[dict], None]:
        """Колбэк для tools.run(stats=…): что сейчас делает процесс — для журнала, когда прогресса нет."""
        now = self.clock()
        info = {"label": label, "started": now, "seen": None, "stats": {}}
        with self._lock:
            self._running[key] = info

        def update(block: dict) -> None:
            info["stats"], info["seen"] = block, self.clock()

        return update

    def forget(self, key: object) -> None:
        with self._lock:
            self._running.pop(key, None)

    def describe(self) -> list[str]:
        """По строке на идущий процесс: «запись 1, кусок 3: кадр 1200, 24 к/с, ×0.8, ffmpeg молчит 95 с»."""
        now = self.clock()
        out = []
        with self._lock:
            running = list(self._running.values())
        for info in running:
            st, seen = info["stats"], info["seen"]
            if seen is None:
                out.append(f"{info['label']}: ffmpeg ещё ничего не сообщил за {now - info['started']:.0f} с")
                continue
            line = (f"{info['label']}: кадр {st.get('frame', '?')}, {st.get('fps', '?')} к/с, "
                    f"скорость {st.get('speed', '?').strip()}")
            if now - seen > 5:
                line += f", ffmpeg молчит {now - seen:.0f} с"
            out.append(line)
        return out

    def track(self, key: object, offset: float = 0.0) -> Callable[[float], None]:
        """Колбэк для tools.run(progress=…): key — один процесс, offset — уже учтённые секунды."""
        def update(seconds: float) -> None:
            self._set(key, offset + seconds)

        return update

    def add(self, key: object, seconds: float) -> None:
        """Отметить кусок работы целиком выполненным (без колбэка ffmpeg)."""
        self._set(key, seconds)


class StallWatch:
    """Пока этап идёт: если STALL_SECONDS нет прогресса — одна строка в журнал о том, что делает каждый
    процесс и как отвечает источник (check), и ещё одна, когда прогресс пошёл снова."""

    def __init__(self, meter: Meter, notify: Callable[[str], None], check: Callable[[], str] | None = None,
                 every: float = 10.0, stall: float = STALL_SECONDS):
        self.meter, self.notify, self.check, self.every, self.stall = meter, notify, check, every, stall
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=self.every + 1)

    def poll(self, stalled_since: float | None) -> float | None:
        """Один шаг проверки; возвращает, с какого момента стоим (None — идёт)."""
        idle = self.meter.clock() - self.meter.last_change
        if stalled_since is None and idle >= self.stall:
            parts = self.meter.describe()
            if self.check:
                parts.append(self.check())
            self.notify(f"Нет прогресса {idle:.0f} с: " + "; ".join(parts))
            return self.meter.last_change
        if stalled_since is not None and idle < self.stall:
            pause = self.meter.last_change - stalled_since
            self.notify(f"Прогресс снова идёт, пауза была {minutes(pause) if pause >= 60 else f'{pause:.0f} с'}")
            return None
        return stalled_since

    def _loop(self) -> None:
        since = None
        while not self._stop.wait(self.every):
            try:
                since = self.poll(since)
            except Exception:  # noqa: BLE001 — диагностика не должна ронять сборку
                pass


def minutes(seconds: float) -> str:
    m = round(seconds / 60)
    if m < 1:
        return "меньше минуты"
    if m < 60:
        return f"{m} мин"
    return f"{m // 60} ч {m % 60:02d} мин"

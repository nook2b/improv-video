"""Автоцвет в духе «Авто» Lumetri: экспозиция, баланс белого, чёрные/белые и контраст.

Всё замеряется по редким кадрам дня и «запекается» в один LUT, поэтому на скорость
обработки 4K не влияет. Цели взяты с кадра Ace Pro 2, к которому Иван применил «Авто»
в Premiere (calibration/ref_lumetri_auto.png): средняя яркость ≈ 0.25, чёрные (1%) ≈ 0.045,
белые (99.5%) ≈ 0.80, тёплый тон сцены почти не трогается.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import brightness
from . import lut as lutlib
from .tools import ffmpeg

TARGET_MEAN = 0.25
TARGET_BLACK = 0.045  # 1-й процентиль яркости
TARGET_WHITE = 0.80  # 99.5-й процентиль яркости
MAX_STOPS = 1.0
MAX_WB = 0.04  # баланс белого: не больше ±4% на канал
WB_STRENGTH = 0.5  # исправляем только половину найденного оттенка
STATS_SIZE = (96, 54)


@dataclass
class Grade:
    stops: float = 0.0
    gains: tuple[float, float, float] = (1.0, 1.0, 1.0)
    black_in: float = 0.0
    white_in: float = 1.0
    gamma: float = 1.0
    log: list[str] = field(default_factory=list)

    def curve(self, v: float, c: int) -> float:
        v *= self.gains[c]
        span = max(self.white_in - self.black_in, 1e-3)
        v = TARGET_BLACK + (v - self.black_in) * (TARGET_WHITE - TARGET_BLACK) / span
        v = min(max(v, 0.0), 1.0)
        return v ** self.gamma

    def describe(self) -> str:
        r, g, b = self.gains
        return (f"экспозиция {self.stops:+.2f} ст., баланс R×{r:.3f} B×{b:.3f}, "
                f"чёрные {self.black_in:.3f}→{TARGET_BLACK}, белые {self.white_in:.3f}→{TARGET_WHITE}, "
                f"гамма {self.gamma:.2f}")


def _pixels(samples: Path, lut_file: Path) -> list[tuple[float, float, float]]:
    """Кадры выборки через LUT → маленькие RGB 0..1 (дисплейные значения BT.709)."""
    work = samples.parent
    name = "stats.cube"
    shutil.copyfile(lut_file, work / name)
    raw = work / "stats.rgb"
    w, h = STATS_SIZE
    ffmpeg(["-i", str(samples.resolve()), "-vf",
            f"scale=in_color_matrix=bt709:in_range=tv,format=gbrp10le,lut3d=file={name}:interp=tetrahedral,"
            f"scale={w}:{h}:flags=area,format=rgb24", "-f", "rawvideo", str(raw)], cwd=work)
    data = raw.read_bytes()
    raw.unlink(missing_ok=True)
    return [(data[i] / 255, data[i + 1] / 255, data[i + 2] / 255) for i in range(0, len(data) - 2, 3)]


def _luma(p):
    return 0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2]


def _pct(sorted_vals, q):
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def _levels(grade: Grade, px) -> None:
    """Баланс белого, точки чёрного/белого и мягкая гамма по уже экспонированным пикселям."""
    grays = [p for p in px if 0.15 < _luma(p) < 0.85 and max(p) - min(p) < 0.12]
    grade.gains = (1.0, 1.0, 1.0)
    if len(grays) > len(px) * 0.02:
        mr = sum(p[0] for p in grays) / len(grays)
        mg = sum(p[1] for p in grays) / len(grays)
        mb = sum(p[2] for p in grays) / len(grays)
        clamp = lambda x: min(max(x, 1 - MAX_WB), 1 + MAX_WB)
        grade.gains = (clamp(1 + (mg / mr - 1) * WB_STRENGTH), 1.0, clamp(1 + (mg / mb - 1) * WB_STRENGTH))

    lum = sorted(_luma((p[0] * grade.gains[0], p[1], p[2] * grade.gains[2])) for p in px)
    black, white = _pct(lum, 0.01), _pct(lum, 0.995)
    # Чёрные ставятся точно; белые растягиваются не больше чем в 1.5 раза и сжимаются не сильнее 0.8
    k = min(max((TARGET_WHITE - TARGET_BLACK) / max(white - black, 1e-3), 0.8), 1.5)
    grade.black_in = black
    grade.white_in = black + (TARGET_WHITE - TARGET_BLACK) / k

    def mean_after(gamma):
        grade.gamma = gamma
        sub = px[::5]
        return sum(_luma(tuple(grade.curve(p[c], c) for c in range(3))) for p in sub) / len(sub)

    lo, hi = 0.8, 1.25  # мягкая гамма: остальное по яркости добирает экспозиция
    for _ in range(16):
        g = (lo + hi) / 2
        if mean_after(g) > TARGET_MEAN:
            lo = g
        else:
            hi = g
    grade.gamma = round((lo + hi) / 2, 3)


def _mean(grade: Grade, px) -> float:
    sub = px[::5]
    return sum(_luma(tuple(grade.curve(p[c], c) for c in range(3))) for p in sub) / len(sub)


def solve(samples: Path, lut: Path, work: Path) -> tuple[Path, Grade]:
    """Подбирает поправки и возвращает (запечённый LUT, параметры).

    Экспозиция (сдвиг лог-сигнала до LUT) и уровни уточняются вместе за 3 итерации:
    уровни растягивают картинку, экспозиция возвращает среднюю яркость к цели.
    """
    import math

    grade = Grade()
    grade.stops = brightness.solve(samples, lut, "ilog", TARGET_MEAN, MAX_STOPS)
    exposed = lut
    for _ in range(3):
        offset = grade.stops * brightness.ILOG_CODES_PER_STOP / 876
        exposed = lutlib.bake_offset(lut, work / "auto_exposure.cube", offset) if abs(grade.stops) > 1e-3 else lut
        px = _pixels(samples, exposed)
        _levels(grade, px)
        m = _mean(grade, px)
        if abs(m - TARGET_MEAN) < 0.01:
            break
        step = math.log2(TARGET_MEAN / max(m, 0.01))
        grade.stops = round(min(max(grade.stops + step, -MAX_STOPS), MAX_STOPS), 3)

    size, rows = lutlib.read_cube(exposed)
    final = [tuple(grade.curve(v, c) for c, v in enumerate(row)) for row in rows]
    return lutlib.write_cube(work / "auto_final.cube", size, final), grade

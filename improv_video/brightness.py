"""Автояркость: замер на редких кадрах и поправка экспозиции в стопах."""

from __future__ import annotations

import math
import re
import shutil
from pathlib import Path

from .tools import concat_list, ffmpeg

# Сколько 10-битных кодов яркости (диапазон 64–940) приходится на один стоп I-Log.
# Оценка по серой оси официального LUT Insta360 «I-Log → Rec.709» около 18% серого:
# ~0.11 входного диапазона на стоп × 876 кодов ≈ 96. Уточняется на реальных клипах.
ILOG_CODES_PER_STOP = 96
MAX_STOPS = 0.5
TARGET_LUMA = 0.475  # 45–50% диапазона 16–235


def adjust_filter(profile: str, stops: float) -> str | None:
    """Фильтр поправки на 10-битном сигнале до LUT.

    I-Log: сдвиг лог-сигнала (одинаковый сдвиг RGB = сдвиг только яркости Y).
    Обычный профиль: усиление яркости над уровнем чёрного, ~2^(stops/2.2) в гамме.
    """
    if abs(stops) < 1e-3:
        return None
    if profile == "ilog":
        offset = round(stops * ILOG_CODES_PER_STOP)
        return f"lutyuv=y='clip(val+{offset},minval,maxval)'"
    gain = 2 ** (stops / 2.2)
    return f"lutyuv=y='clip(64+(val-64)*{gain:.4f},minval,maxval)'"


def sample_frames(clips: list[Path], out: Path, every: float = 2.0) -> Path:
    """Кадр раз в every секунд, 480p, 10 бит без потерь — быстрый материал для замеров."""
    lst = concat_list(clips, out.with_suffix(".txt"))
    ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(lst), "-an",
        "-vf", f"fps=1/{every},scale=-2:480:flags=bilinear,format=yuv420p10le",
        "-c:v", "ffv1", str(out),
    ])
    return out


def measure(samples: Path, lut: Path | None, profile: str, stops: float) -> float:
    """Средняя яркость кадров после поправки и LUT, 0..1 в диапазоне 16–235."""
    steps = ["format=yuv420p10le"]
    adj = adjust_filter(profile, stops)
    if adj:
        steps.append(adj)
    cwd = samples.parent
    if lut:
        name = "lut" + Path(lut).suffix.lower()
        if Path(lut).resolve() != (cwd / name).resolve():
            shutil.copyfile(lut, cwd / name)
        steps += ["scale=in_color_matrix=bt709:in_range=tv", "format=gbrp10le", f"lut3d=file={name}:interp=tetrahedral"]
    steps += ["scale=out_color_matrix=bt709:out_range=tv", "format=yuv420p", "signalstats",
              "metadata=print:key=lavfi.signalstats.YAVG"]
    proc = ffmpeg(["-i", str(samples), "-vf", ",".join(steps), "-f", "null", "-"], cwd=cwd)
    values = [float(v) for v in re.findall(r"lavfi\.signalstats\.YAVG=([\d.]+)", proc.stderr)]
    if not values:
        raise RuntimeError("Не удалось замерить яркость")
    mean = sum(values) / len(values)
    return min(max((mean - 16) / 219, 0.0), 1.0)


def solve(samples: Path, lut: Path | None, profile: str, target: float = TARGET_LUMA,
          max_stops: float = MAX_STOPS) -> float:
    """Поправка в стопах за 2–3 замера (метод секущих), ограниченная ±max_stops."""
    clamp = lambda s: max(-max_stops, min(max_stops, s))
    s0, m0 = 0.0, measure(samples, lut, profile, 0.0)
    if abs(m0 - target) < 0.01:
        return 0.0
    s1 = clamp(math.log2(target / max(m0, 0.02)))
    m1 = measure(samples, lut, profile, s1)
    best = min(((s0, m0), (s1, m1)), key=lambda sm: abs(sm[1] - target))
    if abs(m1 - m0) > 1e-4 and abs(m1 - target) >= 0.01:
        s2 = clamp(s1 + (target - m1) * (s1 - s0) / (m1 - m0))
        if abs(s2 - s1) > 1e-3:
            m2 = measure(samples, lut, profile, s2)
            best = min((best, (s2, m2)), key=lambda sm: abs(sm[1] - target))
    return round(best[0], 3)

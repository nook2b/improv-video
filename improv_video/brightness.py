"""Автояркость: замер на редких кадрах и поправка экспозиции в стопах."""

from __future__ import annotations

import math
import re
import shutil
from pathlib import Path

from .tools import concat_list, ffmpeg
from .video import decode_args, normalize_filter

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


def sample_frames(clips: list[Path], out: Path, every: float = 2.0, src_range: str = "tv") -> Path:
    """Кадр раз в every секунд, 480p, 10 бит без потерь — быстрый материал для замеров.

    Декодируются только опорные кадры (-skip_frame nokey): полный декод 4K HEVC медленнее реального времени.
    """
    lst = concat_list(clips, out.with_suffix(".txt"))
    vf = f"fps=1/{every},scale=-2:480:flags=bilinear,{normalize_filter(src_range)}"
    for fast in (True, False):
        skip = ["-skip_frame", "nokey"] if fast else []
        ffmpeg([*decode_args(), *skip, "-f", "concat", "-safe", "0", "-i", str(lst), "-an",
                "-vf", vf, "-c:v", "ffv1", str(out)])
        if _frame_count(out) > 0:
            break
    return out


def _frame_count(path: Path) -> int:
    from .tools import ToolError, run

    try:
        out = run("ffprobe", ["-v", "error", "-count_packets", "-select_streams", "v:0",
                              "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)]).stdout
        return int(out.strip() or 0)
    except (ToolError, ValueError):
        return 0


def measure(samples: Path, lut: Path | None, profile: str, stops: float) -> float:
    """Средняя яркость кадров после поправки и LUT, 0..1 в диапазоне 16–235."""
    samples = Path(samples).resolve()
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

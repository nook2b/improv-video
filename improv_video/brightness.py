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


MAX_SAMPLES = 150  # кадров на замер дня: для автоцвета хватает, больше — только дольше
SAMPLE_WORKERS = 4


def sample_frames(clips: list[Path], out: Path, every: float = 2.0, src_range: str = "tv",
                  progress=None) -> Path:
    """Кадры для замеров: не чаще раза в every секунд и не больше MAX_SAMPLES, 480p, 10 бит без потерь.

    К каждому моменту ffmpeg прыгает сразу (-ss до -i), поэтому с карты читаются только
    несколько мегабайт вокруг кадра, а не весь файл: раньше замер дня читал десятки ГБ
    и шёл 20–40 минут. progress получает «секунды видео», как и остальные этапы.
    """
    from concurrent.futures import ThreadPoolExecutor

    from .probe import probe
    from .tools import Cancelled, ToolError

    clips = [Path(c) for c in clips]
    durations = [max(probe(c).duration, 0.0) for c in clips]
    total = sum(durations)
    n = max(1, min(MAX_SAMPLES, math.ceil(total / max(every, 1e-3))))
    jobs = []
    for i in range(n):
        t, start = (i + 0.5) * total / n, 0.0
        for clip, d in zip(clips, durations):
            if t < start + d or clip == clips[-1]:
                jobs.append((i, clip, min(max(t - start, 0.0), max(d - 0.5, 0.0))))
                break
            start += d
    work = out.parent / f"{out.stem}_frames"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    vf = f"scale=-2:480:flags=bilinear,{normalize_filter(src_range)}"
    done = 0

    def grab(job):
        nonlocal done
        i, clip, at = job
        frame = work / f"{i:04d}.mkv"
        try:
            ffmpeg([*decode_args(), "-ss", f"{at:.3f}", "-i", str(clip.resolve()), "-an", "-frames:v", "1",
                    "-vf", vf, "-c:v", "ffv1", str(frame)])
        except Cancelled:
            raise
        except ToolError:
            frame = None  # битое место в файле — без этого кадра
        done += 1
        if progress:
            progress(done / n * total)
        return frame

    with ThreadPoolExecutor(max_workers=SAMPLE_WORKERS) as pool:
        frames = [f for f in pool.map(grab, jobs) if f is not None and f.exists() and f.stat().st_size > 0]
    if not frames:
        raise ValueError("Не удалось взять ни одного кадра для замера")
    lst = concat_list(frames, work / "frames.txt")
    # Склейка заново (кадры крошечные): у каждого своё время 0, setpts раскладывает их подряд
    ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst), "-an", "-vf", "setpts=N/(25*TB)", "-r", "25",
            "-c:v", "ffv1", str(out)])
    shutil.rmtree(work, ignore_errors=True)
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

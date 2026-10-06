"""Кодирование записей в HEVC 10 бит и склейка без перекодирования."""

from __future__ import annotations

import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from .probe import Media
from .tools import ToolError, concat_list, ffmpeg

COLOR_TAGS = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]

ENCODERS = {
    "darwin": ["hevc_videotoolbox", "libx265"],
    "win32": ["hevc_nvenc", "hevc_amf", "hevc_qsv", "libx265"],
}
DEFAULT_ENCODERS = ["hevc_nvenc", "libx265"]


def decode_args() -> list[str]:
    """Аппаратное декодирование 4K HEVC на Mac; в остальных системах — процессор."""
    return ["-hwaccel", "videotoolbox"] if sys.platform == "darwin" else []


def normalize_filter(src_range: str = "tv") -> str:
    """Любой вход → 10 бит, ограниченный диапазон, BT.709. Ace Pro 2 пишет 8 бит полного диапазона."""
    rng = "pc" if src_range == "pc" else "tv"
    return (f"scale=in_range={rng}:out_range=tv:in_color_matrix=bt709:out_color_matrix=bt709,"
            "format=yuv420p10le")


@dataclass(frozen=True)
class Target:
    width: int
    height: int
    fps: Fraction


def _pix_fmt(encoder: str) -> str:
    return "yuv420p10le" if encoder == "libx265" else "p010le"


def encoder_args(encoder: str, bitrate: int, x265_preset: str = "medium") -> list[str]:
    rate = ["-b:v", str(bitrate), "-maxrate", str(int(bitrate * 1.5)), "-bufsize", str(bitrate * 2)]
    if encoder == "libx265":
        extra = ["-preset", x265_preset, "-x265-params", "log-level=error"]
    elif encoder == "hevc_nvenc":
        extra = ["-preset", "p5", "-rc", "vbr", "-profile:v", "main10"]
    else:
        extra = ["-profile:v", "main10"]
    return ["-c:v", encoder, *extra, *rate, "-pix_fmt", _pix_fmt(encoder), *COLOR_TAGS, "-tag:v", "hvc1"]


@lru_cache(maxsize=None)
def pick_encoder(candidates: tuple[str, ...] | None = None) -> str:
    """Первый кодировщик из списка, который реально кодирует 10 бит на этом компьютере."""
    for enc in candidates or tuple(ENCODERS.get(sys.platform, DEFAULT_ENCODERS)):
        try:
            ffmpeg([
                "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=0.2",
                *encoder_args(enc, 2_000_000, "ultrafast"), "-f", "null", "-",
            ])
            return enc
        except ToolError:
            continue
    raise RuntimeError("Не найден кодировщик HEVC 10 бит")


def bitrate_for(target: Target) -> int:
    """Битрейт по рекомендациям YouTube для SDR: выше FPS — выше битрейт."""
    high_fps = target.fps > 31
    short_side = min(target.width, target.height)
    if short_side >= 2160:
        mbps = 60 if high_fps else 40
    elif short_side >= 1440:
        mbps = 24 if high_fps else 16
    elif short_side >= 1080:
        mbps = 12 if high_fps else 8
    else:
        mbps = 7 if high_fps else 5
    return mbps * 1_000_000


def choose_target(medias: list[Media], max_height: int = 2160) -> Target:
    """Разрешение и FPS большинства клипов дня (по длительности), не выше потолка."""
    landscape = [m for m in medias if m.width >= m.height] or medias
    weight: dict[tuple, float] = defaultdict(float)
    for m in landscape:
        weight[(m.width, m.height, m.fps.limit_denominator(1001))] += m.duration
    w, h, fps = max(weight, key=weight.get)
    short = min(w, h)
    if short > max_height:
        k = max_height / short
        w, h = (round(w * k / 2) * 2, round(h * k / 2) * 2)
    return Target(w, h, fps)


def video_filter(target: Target, encoder: str, lut_name: str | None, adjust: str | None,
                 src_range: str = "tv", src_size: tuple[int, int] | None = None) -> str:
    """Цепочка на кадр.

    С LUT — два пересчёта цвета: вход (8 бит, полный диапазон) → RGB 10 бит → LUT (поправка
    яркости уже запечена в нём) → YUV 10 бит BT.709 с масштабом, если размер отличается.
    Без LUT — приведение к 10 битам, поправка яркости кривой, масштаб.
    """
    w, h = target.width, target.height
    same = src_size == (w, h)
    rng = "pc" if src_range == "pc" else "tv"
    if lut_name:
        steps = [f"scale=in_range={rng}:in_color_matrix=bt709", "format=gbrp10le",
                 f"lut3d=file={lut_name}:interp=tetrahedral"]
        if adjust:
            steps.insert(0, adjust)
        size = "" if same else f"{w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos:"
        steps.append(f"scale={size}out_color_matrix=bt709:out_range=tv")
    else:
        steps = [normalize_filter(src_range)]
        if adjust:
            steps.append(adjust)
        if not same:
            steps.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos"
                         ":in_color_matrix=bt709:out_color_matrix=bt709:out_range=tv")
    if not same:
        steps.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
    steps += ["setsar=1", f"fps={target.fps.numerator}/{target.fps.denominator}", f"format={_pix_fmt(encoder)}"]
    return ",".join(steps)


def plan_chunks(duration: float, fps: Fraction, chunk_seconds: float = 300) -> list[tuple[int, int]]:
    """Делит запись на куски по ~chunk_seconds: (первый кадр, число кадров). Сумма кадров точная."""
    total = max(1, round(duration * fps))
    per = max(1, round(chunk_seconds * fps))
    if total <= per * 1.5:
        return [(0, total)]
    chunks, start = [], 0
    while start < total:
        count = per if total - start - per >= per // 2 else total - start
        chunks.append((start, count))
        start += count
    return chunks


def encode_chunk(
    clips: list[Path],
    out: Path,
    target: Target,
    encoder: str,
    lut: Path | None = None,
    adjust: str | None = None,
    x265_preset: str = "medium",
    src_range: str = "tv",
    src_size: tuple[int, int] | None = None,
    start_frame: int = 0,
    frames: int | None = None,
) -> Path:
    """Кодирует кусок записи без звука: кадры [start_frame, start_frame + frames) на сетке target.fps."""
    out = Path(out).resolve()
    work = out.parent
    lut_name = None
    if lut:
        lut_name = Path(lut).resolve().name
        if Path(lut).resolve().parent != work:
            shutil.copyfile(lut, work / lut_name)
    lst = concat_list(clips, out.with_suffix(".txt"))
    seek = ["-ss", f"{float(Fraction(start_frame) / target.fps):.6f}"] if start_frame else []
    count = ["-frames:v", str(frames)] if frames else []
    ffmpeg(
        [
            *decode_args(), *seek, "-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0:v:0", "-an",
            "-vf", video_filter(target, encoder, lut_name, adjust, src_range, src_size),
            *count, *encoder_args(encoder, bitrate_for(target), x265_preset),
            str(out),
        ],
        cwd=work,
    )
    return out


def encode_recording(clips: list[Path], out: Path, target: Target, encoder: str, lut: Path | None = None,
                     adjust: str | None = None, x265_preset: str = "medium", src_range: str = "tv",
                     src_size: tuple[int, int] | None = None) -> Path:
    """Вся запись одним куском (без деления)."""
    return encode_chunk(clips, out, target, encoder, lut, adjust, x265_preset, src_range, src_size)


def concat_video(parts: list[Path], out: Path) -> Path:
    if len(parts) == 1:
        Path(parts[0]).replace(out)
        return out
    lst = concat_list(parts, out.with_name(out.stem + "_parts.txt"))
    ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-tag:v", "hvc1", str(out)])
    for p in parts:
        Path(p).unlink(missing_ok=True)
    return out


def mux(segments: list[Path], audio_wav: Path, out: Path) -> Path:
    """Склейка закодированных записей без перекодирования + готовый звук в AAC."""
    lst = concat_list(segments, out.with_name(out.stem + "_segments.txt"))
    ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(lst), "-i", str(audio_wav),
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-tag:v", "hvc1",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out),
    ])
    return out

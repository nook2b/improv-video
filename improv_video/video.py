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
                 src_range: str = "tv") -> str:
    """Порядок: 10 бит, ограниченный диапазон → поправка яркости → LUT → масштаб с полями → FPS."""
    steps = [normalize_filter(src_range)]
    if adjust:
        steps.append(adjust)
    if lut_name:
        steps += [
            "scale=in_color_matrix=bt709:in_range=tv",
            "format=gbrp10le",
            f"lut3d=file={lut_name}:interp=tetrahedral",
        ]
    w, h = target.width, target.height
    steps += [
        f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos"
        ":in_color_matrix=bt709:out_color_matrix=bt709:out_range=tv",
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
        f"fps={target.fps.numerator}/{target.fps.denominator}",
        f"format={_pix_fmt(encoder)}",
    ]
    return ",".join(steps)


def encode_recording(
    clips: list[Path],
    out: Path,
    target: Target,
    encoder: str,
    lut: Path | None = None,
    adjust: str | None = None,
    x265_preset: str = "medium",
    src_range: str = "tv",
) -> Path:
    """Кодирует одну запись (все её куски) без звука."""
    out = Path(out).resolve()
    work = out.parent
    lut_name = None
    if lut:
        lut_name = "lut" + Path(lut).suffix.lower()
        if Path(lut).resolve() != (work / lut_name).resolve():
            shutil.copyfile(lut, work / lut_name)
    lst = concat_list(clips, out.with_suffix(".txt"))
    ffmpeg(
        [
            *decode_args(), "-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0:v:0", "-an",
            "-vf", video_filter(target, encoder, lut_name, adjust, src_range),
            *encoder_args(encoder, bitrate_for(target), x265_preset),
            str(out),
        ],
        cwd=work,
    )
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

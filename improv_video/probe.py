"""Параметры видеофайла через ffprobe."""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .tools import run


@dataclass(frozen=True)
class Media:
    duration: float  # секунды, по видеодорожке
    width: int  # с учётом поворота (вертикальное видео — width < height)
    height: int
    fps: Fraction
    has_audio: bool
    pix_fmt: str = ""
    color_transfer: str = ""


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            return int(round(float(side["rotation"])))
    rotate = stream.get("tags", {}).get("rotate")
    return int(rotate) if rotate else 0


def _fps(stream: dict) -> Fraction:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key, "0/0")
        num, _, den = value.partition("/")
        if den and int(den) and int(num):
            return Fraction(int(num), int(den))
    return Fraction(30)


def probe(path: Path) -> Media:
    out = run(
        "ffprobe",
        ["-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
    ).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError(f"В файле нет видео: {path}")
    duration = float(video.get("duration") or data.get("format", {}).get("duration") or 0)
    w, h = int(video["width"]), int(video["height"])
    if abs(_rotation(video)) % 180 == 90:
        w, h = h, w
    return Media(
        duration=duration,
        width=w,
        height=h,
        fps=_fps(video),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        pix_fmt=video.get("pix_fmt", ""),
        color_transfer=video.get("color_transfer", ""),
    )

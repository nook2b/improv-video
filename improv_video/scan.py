"""Поиск карты камеры и клипов на ней."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .naming import parse_start


@dataclass(frozen=True)
class Clip:
    path: Path
    name: str
    size: int
    start: datetime


def find_camera_dirs(volume: Path) -> list[Path]:
    """Папки DCIM/Camera* с клипами VID_*.mp4 на томе."""
    dcim = Path(volume) / "DCIM"
    if not dcim.is_dir():
        return []
    dirs = [d for d in sorted(dcim.iterdir()) if d.is_dir() and d.name.lower().startswith("camera")]
    return [d for d in dirs if list_clips(d)]


def list_clips(folder: Path) -> list[Clip]:
    """Только VID_*.mp4 с датой в имени; .lrv, .bin, фото и прочее пропускаются.

    Копии Finder («VID_… 2.mp4») с тем же временем и размером, что у оригинала, отбрасываются.
    """
    clips = []
    for p in Path(folder).iterdir():
        if not p.is_file() or p.suffix.lower() != ".mp4" or p.name.startswith("."):
            continue
        start = parse_start(p.name)
        if start is None:
            continue
        clips.append(Clip(path=p, name=p.name, size=p.stat().st_size, start=start))
    # Из одинаковых по времени и размеру оставляем файл с самым коротким именем (оригинал).
    best: dict[tuple, Clip] = {}
    for c in clips:
        key = (c.start, c.size)
        if key not in best or (len(c.name), c.name) < (len(best[key].name), best[key].name):
            best[key] = c
    return sorted(best.values(), key=lambda c: (c.start, c.name))

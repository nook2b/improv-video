import subprocess
from pathlib import Path

import pytest

from improv_video.pipeline import Settings


def make_clip(path: Path, seconds: float, size="640x360", audio_seconds: float | None = None, vf: str | None = None):
    """Синтетический клип: тестовая картинка + тон; audio_seconds=0 — без звука."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=s={size}:r=30:d={seconds}"]
    a = seconds if audio_seconds is None else audio_seconds
    if a:
        cmd += ["-f", "lavfi", "-i", f"sine=f=440:d={a}:sample_rate=48000"]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if a:
        cmd += ["-c:a", "aac", "-ac", "2"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True)
    return path


@pytest.fixture(scope="session")
def card(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("card")
    cam = root / "DCIM" / "Camera01"
    make_clip(cam / "VID_20261001_180500_00_001.mp4", 3, audio_seconds=2.5)  # звук короче видео
    make_clip(cam / "VID_20261001_180503_00_002.mp4", 3)  # продолжение той же записи
    make_clip(cam / "VID_20261001_183000_00_003.mp4", 2, audio_seconds=0)  # без звука
    make_clip(cam / "VID_20261002_013000_00_004.mp4", 2)  # после полуночи → день 01.10
    make_clip(cam / "VID_20261003_120000_00_005.mp4", 2, size="360x640")  # вертикальное
    (cam / "LRV_20261001_180500_01_001.lrv").write_bytes(b"proxy")
    (cam / "IMG_20261001_180000_00_006.jpg").write_bytes(b"photo")
    return root


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(archive=tmp_path / "Footage", profile="normal", auto_brightness=False,
                    x265_preset="ultrafast", encoders=("libx265",))


@pytest.fixture
def identity_lut(tmp_path) -> Path:
    lines = ["LUT_3D_SIZE 2"]
    for b in (0, 1):
        for g in (0, 1):
            for r in (0, 1):
                lines.append(f"{r}.0 {g}.0 {b}.0")
    p = tmp_path / "identity.cube"
    p.write_text("\n".join(lines) + "\n")
    return p

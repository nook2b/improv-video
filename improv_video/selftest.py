"""Самопроверка собранного приложения: ffmpeg внутри, кодировщик, шумодав, сборка ролика.

improv-video.app/Contents/MacOS/improv-video --selftest
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

from . import audio, video
from .pipeline import Settings, build_day
from .probe import probe
from .tools import ToolError, ffmpeg, find_tool, resources_dir


def _clip(path: Path, seconds: int) -> None:
    src = ["-f", "lavfi", "-i", f"testsrc2=s=1280x720:r=30000/1001:d={seconds}",
           "-f", "lavfi", "-i", f"sine=f=440:d={seconds}:sample_rate=48000"]
    for codec in (["-c:v", "libx264", "-pix_fmt", "yuv420p"], ["-c:v", "mpeg4", "-q:v", "5"]):
        try:
            ffmpeg([*src, *codec, "-c:a", "aac", "-ac", "2", str(path)])
            return
        except ToolError:
            continue
    raise RuntimeError("Не удалось создать тестовый клип")


def main() -> int:
    print("ffmpeg:", find_tool("ffmpeg"))
    print("ffprobe:", find_tool("ffprobe"))
    encoders = ffmpeg(["-encoders"]).stdout
    for name in ("hevc_videotoolbox", "libx265", "aac"):
        print(f"  {name}: {'есть' if name in encoders else 'НЕТ'}")
    print("кодировщик:", video.pick_encoder())
    model = resources_dir() / "rnnoise" / "bd.rnnn"
    print("RNNoise:", model, model.exists())
    try:
        print("deep-filter:", find_tool("deep-filter"))
    except FileNotFoundError:
        print("deep-filter: нет (сильное шумоподавление недоступно)")

    # Автообновление: сертификаты внутри приложения (без них — CERTIFICATE_VERIFY_FAILED)
    import ssl
    import urllib.error

    from .app import updater

    tls_ok = True
    try:
        with updater._open("https://github.com/nook2b/improv-video/releases", 30) as r:
            print("HTTPS до GitHub:", r.status)
    except urllib.error.HTTPError as e:  # сертификат принят, ответ сервера не важен
        print("HTTPS до GitHub: сертификат принят, ответ", e.code)
    except urllib.error.URLError as e:
        tls_ok = not isinstance(e.reason, ssl.SSLError)
        print("HTTPS до GitHub:", e.reason)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        a = work / "VID_20261001_180000_00_001.mp4"
        b = work / "VID_20261001_180000_00_002.mp4"  # второй кусок той же записи
        _clip(a, 3)
        _clip(b, 2)
        settings = Settings(archive=work, profile="normal", denoise="medium", rnnoise_model=model,
                            x265_preset="ultrafast")
        r = build_day(date(2026, 10, 1), [a, b], "training", 1, settings, work / "out.mp4", notify=print)
        m = probe(r.file)
        print(f"ролик: {r.title} · {m.width}x{m.height} · {float(m.fps):.3f} к/с · {m.duration:.2f} с · {r.encoder}")
        ok = abs(m.duration - 5) < 0.2 and m.has_audio and float(m.fps) < 30 and tls_ok
    print("ИТОГ:", "OK" if ok else "ОШИБКА")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

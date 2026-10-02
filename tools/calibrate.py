"""Калибровка на настоящем клипе: метаданные, яркость с LUT, громкость, кадры и превью.

python tools/calibrate.py clip.mp4 lut.cube out/
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from improv_video import brightness  # noqa: E402
from improv_video.pipeline import Settings, build_day  # noqa: E402
from improv_video.probe import probe  # noqa: E402
from improv_video.tools import ffmpeg, resources_dir, run  # noqa: E402


def section(title):
    print(f"\n=== {title} ===", flush=True)


def stats(clip: Path, vf: str) -> dict:
    out = ffmpeg(["-i", str(clip), "-vf", vf + ",signalstats,metadata=print", "-an", "-f", "null", "-"]).stderr
    keys = ("YAVG", "YLOW", "YHIGH", "SATAVG")
    res = {}
    for k in keys:
        vals = [float(v) for v in re.findall(rf"lavfi\.signalstats\.{k}=([\d.]+)", out)]
        res[k] = round(sum(vals) / len(vals), 1) if vals else None
    return res


def still(clip: Path, t: float, vf: str, out: Path) -> None:
    ffmpeg(["-ss", f"{t:.2f}", "-i", str(clip), "-vf", vf + ",scale=1280:-2", "-frames:v", "1", str(out)])


def main(clip: Path, lut: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    section("Метаданные")
    info = json.loads(run("ffprobe", ["-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(clip)]).stdout)
    for s in info["streams"]:
        keep = {k: s.get(k) for k in ("codec_type", "codec_name", "profile", "pix_fmt", "width", "height",
                                      "avg_frame_rate", "color_range", "color_space", "color_transfer",
                                      "color_primaries", "bit_rate", "sample_rate", "channels", "duration")}
        print(json.dumps({k: v for k, v in keep.items() if v is not None}, ensure_ascii=False))
        if s.get("tags"):
            print("  теги потока:", json.dumps(s["tags"], ensure_ascii=False))
    print("теги файла:", json.dumps(info["format"].get("tags", {}), ensure_ascii=False))
    m = probe(clip)
    print(f"probe: {m.width}x{m.height} {float(m.fps):.3f} к/с {m.duration:.1f} с звук={m.has_audio}")

    section("Яркость (кадр раз в 2 с, 480p)")
    work = out / "work"
    work.mkdir(exist_ok=True)
    samples = brightness.sample_frames([clip], work / "samples.mkv", 2.0)
    raw = stats(samples, "format=yuv420p")
    print("без LUT (лог-картинка):", raw)
    lut_name = "lut.cube"
    shutil.copyfile(lut, samples.parent / lut_name)
    curve = {}
    for st in (-0.5, -0.25, 0.0, 0.25, 0.5):
        curve[st] = round(brightness.measure(samples, lut, "ilog", st), 3)
    print("яркость после LUT при поправке (стопы → доля 16–235):", curve)
    stops = brightness.solve(samples, lut, "ilog")
    after = brightness.measure(samples, lut, "ilog", stops)
    print(f"цель {brightness.TARGET_LUMA}: поправка {stops:+.2f} стопа → {after:.3f}")
    # Без ограничения: какая поправка нужна на самом деле
    free = brightness.solve(samples, lut, "ilog", max_stops=3.0)
    print(f"без ограничения ±0.5 понадобилось бы {free:+.2f} стопа")

    section("Громкость")
    ebu = ffmpeg(["-i", str(clip), "-vn", "-af", "ebur128", "-f", "null", "-"]).stderr
    summary = ebu[ebu.rfind("Summary:"):]
    print(summary.strip())

    section("Кадры")
    mid = m.duration / 2
    base = f"format=yuv420p10le,scale=in_color_matrix=bt709:in_range=tv,format=gbrp10le,lut3d=file={lut_name}:interp=tetrahedral"
    adj = brightness.adjust_filter("ilog", stops)
    cwd_lut = out / lut_name
    shutil.copyfile(lut, cwd_lut)
    for name, vf in (("1_raw", "format=yuv420p"), ("2_lut", base),
                     ("3_lut_auto", ("format=yuv420p10le," + adj + "," + base[len('format=yuv420p10le,'):]) if adj else base)):
        run("ffmpeg", ["-y", "-ss", f"{mid:.2f}", "-i", str(clip), "-vf", vf + ",scale=1280:-2:out_color_matrix=bt709",
                       "-frames:v", "1", f"{name}.png"], cwd=out)
    print("кадры: 1_raw.png, 2_lut.png, 3_lut_auto.png")

    section("Превью полного конвейера (первые 60 с)")
    vid = work / "VID_20261001_180000_00_001.mp4"
    ffmpeg(["-i", str(clip), "-t", "60", "-c", "copy", str(vid)])
    settings = Settings(archive=work, lut=lut, profile="ilog", denoise="medium",
                        rnnoise_model=resources_dir() / "rnnoise" / "bd.rnnn", x265_preset="veryfast")
    r = build_day(date(2026, 10, 1), [vid], "training", 1, settings, out / "preview.mp4", notify=print)
    pm = probe(r.file)
    print(f"превью: {pm.width}x{pm.height} {float(pm.fps):.3f} к/с {pm.duration:.1f} с, поправка {r.brightness_stops}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))

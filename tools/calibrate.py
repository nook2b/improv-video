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
from improv_video.video import normalize_filter  # noqa: E402
import time  # noqa: E402


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
    clip, lut, out = clip.resolve(), lut.resolve(), out.resolve()
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
    t0 = time.monotonic()
    samples = brightness.sample_frames([clip], work / "samples.mkv", 2.0, m.color_range)
    print(f"выборка кадров: {time.monotonic() - t0:.1f} с на {m.duration:.0f} с видео (диапазон {m.color_range})")
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
    norm = normalize_filter(m.color_range)
    lut_part = f"scale=in_color_matrix=bt709:in_range=tv,format=gbrp10le,lut3d=file={lut_name}:interp=tetrahedral"
    adj = brightness.adjust_filter("ilog", stops)
    cwd_lut = out / lut_name
    shutil.copyfile(lut, cwd_lut)
    variants = [("1_raw", "format=yuv420p"), ("2_lut_0", f"{norm},{lut_part}")]
    for st in (0.5, 1.0, 1.5, 2.0):
        variants.append((f"3_lut_+{st:g}", f"{norm},{brightness.adjust_filter('ilog', st)},{lut_part}"))
    for name, vf in variants:
        run("ffmpeg", ["-y", "-ss", f"{mid:.2f}", "-i", str(clip), "-vf", vf + ",scale=1280:-2:out_color_matrix=bt709",
                       "-frames:v", "1", f"{name}.png"], cwd=out)
    print("кадры:", ", ".join(n + ".png" for n, _ in variants))

    section("Автоцвет против «Авто» Lumetri (кадр 00:00:00)")
    from improv_video import autocolor
    auto_lut, grade = autocolor.solve(samples, lut, work)
    print("автоцвет:", grade.describe())
    shutil.copyfile(auto_lut, out / "auto.cube")
    ref = Path(__file__).resolve().parent.parent / "calibration" / "ref_lumetri_auto.png"
    shutil.copyfile(ref, out / "0_lumetri_auto_t0.png")
    rng = "pc" if m.color_range == "pc" else "tv"
    for name, cube in (("4_lut_only_t0", lut_name), ("5_auto_t0", "auto.cube")):
        run("ffmpeg", ["-y", "-i", str(clip), "-frames:v", "1", "-vf",
                       f"scale=in_range={rng}:in_color_matrix=bt709,format=gbrp10le,lut3d=file={cube}:interp=tetrahedral,"
                       "scale=1280:-2:out_color_matrix=bt709,format=rgb24", f"{name}.png"], cwd=out)

    import subprocess
    from improv_video.tools import find_tool
    for name in ("0_lumetri_auto_t0", "4_lut_only_t0", "5_auto_t0"):
        data = subprocess.run([find_tool("ffmpeg"), "-v", "error", "-i", str(out / f"{name}.png"), "-vf", "scale=195:108",
                               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
        px = [(data[i] / 255, data[i + 1] / 255, data[i + 2] / 255) for i in range(0, len(data) - 2, 3)]
        lum = sorted(autocolor._luma(p) for p in px)
        mean = sum(lum) / len(lum)
        rgb = [sum(p[c] for p in px) / len(px) for c in range(3)]
        print(f"{name}: средняя {mean:.3f}, медиана {autocolor._pct(lum, .5):.3f}, чёрные(1%) {autocolor._pct(lum, .01):.3f}, "
              f"белые(99.5%) {autocolor._pct(lum, .995):.3f}, RGB {rgb[0]:.3f}/{rgb[1]:.3f}/{rgb[2]:.3f}")

    section("Скорость цветовой обработки 4K (без кодирования, 150 кадров)")
    from improv_video.video import Target, video_filter
    t4k = Target(3840, 2160, m.fps)
    vf = video_filter(t4k, "libx265", lut_name, adj, m.color_range)
    for label, extra in (("как в приложении", []), ("с -filter_threads 8", ["-filter_threads", "8"])):
        t0 = time.monotonic()
        run("ffmpeg", ["-y", *extra, "-i", str(clip), "-frames:v", "150", "-vf", vf, "-an", "-f", "null", "-"], cwd=out)
        dt = time.monotonic() - t0
        print(f"{label}: {150 / dt:.1f} к/с (с декодированием)")
    t0 = time.monotonic()
    run("ffmpeg", ["-y", "-i", str(clip), "-frames:v", "150", "-an", "-f", "null", "-"])
    print(f"только декодирование: {150 / (time.monotonic() - t0):.1f} к/с")

    section("Превью полного конвейера (первые 20 с, 1080p)")
    vid = work / "VID_20261001_180000_00_001.mp4"
    ffmpeg(["-i", str(clip), "-t", "20", "-c", "copy", str(vid)])
    settings = Settings(archive=work, lut=lut, profile="ilog", denoise="medium",
                        rnnoise_model=resources_dir() / "rnnoise" / "bd.rnnn", x265_preset="ultrafast",
                        max_height=1080)
    t0 = time.monotonic()
    r = build_day(date(2026, 10, 1), [vid], "training", 1, settings, out / "preview.mp4", notify=print)
    pm = probe(r.file)
    print(f"превью: {pm.width}x{pm.height} {float(pm.fps):.3f} к/с {pm.duration:.1f} с, поправка {r.brightness_stops}, "
          f"сборка {time.monotonic() - t0:.0f} с ({r.encoder})")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))

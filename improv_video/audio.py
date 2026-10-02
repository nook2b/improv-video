"""Звук дня: дорожки клипов точно по длине видео, шумодав, выравнивание громкости."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .tools import concat_list, ffmpeg, run

RATE = 48000
DENOISE_LEVELS = ("off", "weak", "medium", "strong")


def samples(seconds: float) -> int:
    return round(seconds * RATE)


def clip_audio(src: Path, duration: float, has_audio: bool, out: Path) -> Path:
    """WAV 48 кГц стерео ровно на duration секунд: лишнее отрезается, недостающее — тишина."""
    n = samples(duration)
    shape = f"aresample={RATE},aformat=sample_fmts=s16:channel_layouts=stereo,apad=whole_len={n},atrim=end_sample={n}"
    if has_audio:
        args = ["-i", str(src), "-map", "0:a:0", "-af", shape]
    else:
        args = ["-f", "lavfi", "-i", f"anullsrc=r={RATE}:cl=stereo", "-af", shape]
    ffmpeg([*args, "-c:a", "pcm_s16le", str(out)])
    return out


def fit(src: Path, duration: float, out: Path) -> Path:
    """Подгоняет WAV под длину duration (под длину закодированного видео)."""
    return clip_audio(src, duration, True, out)


def concat(wavs: list[Path], out: Path) -> Path:
    lst = concat_list(wavs, out.with_suffix(".txt"))
    ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
    return out


def denoise(src: Path, out: Path, level: str, rnnoise_model: Path | None = None) -> Path:
    if level not in DENOISE_LEVELS:
        raise ValueError(f"Неизвестный уровень шумоподавления: {level!r}")
    if level == "off":
        shutil.copyfile(src, out)
        return out
    if level == "weak":
        ffmpeg(["-i", str(src), "-af", "afftdn=nf=-25", "-c:a", "pcm_s16le", str(out)])
        return out
    if level == "medium":
        if not rnnoise_model or not Path(rnnoise_model).exists():
            raise FileNotFoundError("Для среднего шумоподавления нужен файл модели RNNoise (.rnnn)")
        # Модель копируется в рабочую папку, чтобы путь не требовал экранирования в фильтре.
        src, out = Path(src).resolve(), Path(out).resolve()
        local = src.parent / "rnnoise.rnnn"
        shutil.copyfile(rnnoise_model, local)
        ffmpeg(["-i", str(src), "-af", "arnndn=m=rnnoise.rnnn", "-c:a", "pcm_s16le", str(out)], cwd=src.parent)
        return out
    # strong: DeepFilterNet, автономная утилита deep-filter; пишет файл с тем же именем в папку -o.
    outdir = out.parent / "deepfilter"
    outdir.mkdir(exist_ok=True)
    run("deep-filter", [str(src), "-o", str(outdir)])
    produced = outdir / src.name
    # deep-filter может отдать float WAV; приводим к общему формату.
    ffmpeg(["-i", str(produced), "-af", f"aresample={RATE}", "-ac", "2", "-c:a", "pcm_s16le", str(out)])
    return out


def loudnorm(src: Path, out: Path, target_lufs: float = -14.0, true_peak: float = -1.5, lra: float = 11.0) -> Path:
    """Двухпроходная нормализация громкости EBU R128."""
    base = f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={lra}"
    proc = ffmpeg(["-i", str(src), "-af", base + ":print_format=json", "-f", "null", "-"])
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.S)
    measured = json.loads(match.group(0)) if match else {}
    try:
        input_i = float(measured.get("input_i", "-inf"))
    except ValueError:
        input_i = float("-inf")
    if input_i == float("-inf") or input_i < -70:
        shutil.copyfile(src, out)  # тишина: нормализовать нечего
        return out
    second = (
        f"{base}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}:linear=true,aresample={RATE}"
    )
    ffmpeg(["-i", str(src), "-af", second, "-c:a", "pcm_s16le", str(out)])
    return out

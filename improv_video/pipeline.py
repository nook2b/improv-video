"""Конвейер: импорт с карты → сборка ролика дня."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable

from . import audio, brightness, video
from .grouping import Part, Recording, group_days, group_recordings
from .naming import DEFAULT_DAY_START, description, shooting_day, title
from .probe import probe
from .scan import Clip, find_camera_dirs, list_clips
from .state import State

GB = 1024**3
RESERVE_BYTES = 10 * GB

Notify = Callable[[str], None]


@dataclass
class Settings:
    archive: Path
    lut: Path | None = None
    profile: str = "ilog"  # ilog | normal
    denoise: str = "weak"  # off | weak | medium | strong
    rnnoise_model: Path | None = None
    auto_brightness: bool = True
    brightness_scope: str = "day"  # day — одна поправка на весь день (ровный свет) | recording
    target_luma: float = brightness.TARGET_LUMA
    max_height: int = 2160
    day_start: time = DEFAULT_DAY_START
    x265_preset: str = "medium"
    encoders: tuple[str, ...] | None = None


@dataclass
class BuildResult:
    file: Path
    title: str
    description: str
    recorded_at: datetime
    recorded_end: datetime
    duration: float
    encoder: str
    brightness_stops: list[float] = field(default_factory=list)


class NotEnoughSpace(RuntimeError):
    pass


def estimate_needed_bytes(clips: list[Clip], durations: list[float], max_height: int = 2160) -> int:
    """Клипы + 2 × итоговое видео (записи и склейка живут одновременно) + WAV дня + запас."""
    seconds = sum(durations)
    final = seconds * (60_000_000 if max_height >= 2160 else 16_000_000) / 8
    wav = seconds * audio.RATE * 2 * 2
    return int(sum(c.size for c in clips) + 2 * final + wav + RESERVE_BYTES)


def source_folders(path: Path) -> list[Path]:
    """Папки с клипами: DCIM/Camera* на карте или сама папка, если клипы лежат прямо в ней."""
    return find_camera_dirs(path) or ([path] if list_clips(path) else [])


def new_clips_on_card(volume: Path, state: State) -> list[Clip]:
    clips = []
    for folder in source_folders(volume):
        clips += [c for c in list_clips(folder) if not state.is_known(c.name, c.size)]
    return clips


def mark_existing_as_done(volume: Path, state: State, settings: Settings) -> int:
    """Первый запуск: всё, что уже лежит на карте, помечается как обработанное."""
    clips = new_clips_on_card(volume, state)
    for c in clips:
        state.add_clip(c.name, c.size, shooting_day(c.start, settings.day_start), status="skipped")
    return len(clips)


def _copy_verified(src: Path, dst: Path) -> None:
    tmp = dst.with_name(dst.name + ".part")
    shutil.copyfile(src, tmp)
    if tmp.stat().st_size != src.stat().st_size:
        tmp.unlink(missing_ok=True)
        raise OSError(f"Размер копии не совпал: {src.name}")
    os.replace(tmp, dst)


def import_card(volume: Path, state: State, settings: Settings, notify: Notify = print) -> dict[date, list[str]]:
    """Копирует новые клипы в архив/ГГГГ-ММ-ДД/. Возвращает {день: [имена клипов]}."""
    clips = new_clips_on_card(volume, state)
    if not clips:
        return {}
    parts = [Part(c, probe(c.path)) for c in clips]
    settings.archive.mkdir(parents=True, exist_ok=True)
    need = estimate_needed_bytes(clips, [p.media.duration for p in parts], settings.max_height)
    free = shutil.disk_usage(settings.archive).free
    if free < need:
        raise NotEnoughSpace(f"Нет места на диске: нужно {need / GB:.0f} ГБ, свободно {free / GB:.0f} ГБ")

    days = group_days(group_recordings(parts), settings.day_start)
    notify(f"Найдено {len(clips)} новых клипов за {len(days)} дн., копирую…")
    result: dict[date, list[str]] = {}
    for day, recordings in days.items():
        folder = settings.archive / day.isoformat()
        folder.mkdir(exist_ok=True)
        for rec in recordings:
            for part in rec.parts:
                target = folder / part.clip.name
                if part.clip.path.resolve() != target.resolve():
                    _copy_verified(part.clip.path, target)
                state.add_clip(part.clip.name, part.clip.size, day)
                result.setdefault(day, []).append(part.clip.name)
    notify("Можно извлечь флешку")
    return result


def build_day(
    day: date,
    clip_paths: list[Path],
    kind: str,
    part: int,
    settings: Settings,
    out: Path,
    notify: Notify = print,
) -> BuildResult:
    """Собирает ролик из клипов одного дня съёмки."""
    clips = [Clip(p, p.name, p.stat().st_size, s) for p in clip_paths if (s := _start(p))]
    if not clips:
        raise ValueError("Нет клипов для сборки")
    recordings = group_recordings([Part(c, probe(c.path)) for c in clips])
    encoder = video.pick_encoder(settings.encoders)
    target = video.choose_target([p.media for r in recordings for p in r.parts], settings.max_height)
    out.parent.mkdir(parents=True, exist_ok=True)

    out = Path(out).resolve()
    with tempfile.TemporaryDirectory(prefix="improv-", dir=out.parent) as tmp:
        work = Path(tmp)
        day_stops = None
        if settings.auto_brightness and settings.brightness_scope == "day":
            notify("Замер яркости")
            day_stops = _solve_brightness([p.clip.path for r in recordings for p in r.parts],
                                          sum(r.duration for r in recordings), settings, work / "samples.mkv",
                                          recordings[0].parts[0].media.color_range)
        segments, rec_wavs, stops_used = [], [], []
        for i, rec in enumerate(recordings, 1):
            notify(f"Запись {i} из {len(recordings)}")
            paths = [p.clip.path for p in rec.parts]
            if day_stops is not None:
                stops = day_stops
            elif settings.auto_brightness:
                stops = _solve_brightness(paths, rec.duration, settings, work / f"samples{i}.mkv",
                                          rec.parts[0].media.color_range)
            else:
                stops = 0.0
            stops_used.append(stops)
            seg = video.encode_recording(
                paths, work / f"rec{i}.mp4", target, encoder, settings.lut,
                brightness.adjust_filter(settings.profile, stops), settings.x265_preset,
                rec.parts[0].media.color_range,
            )
            segments.append(seg)
            # Звук записи: каждый кусок по длине своего видео, затем вся запись — по длине сегмента.
            piece_wavs = [
                audio.clip_audio(p.clip.path, p.media.duration, p.media.has_audio, work / f"rec{i}_{j}.wav")
                for j, p in enumerate(rec.parts)
            ]
            joined = audio.concat(piece_wavs, work / f"rec{i}_joined.wav")
            rec_wavs.append(audio.fit(joined, probe(seg).duration, work / f"rec{i}.wav"))

        notify("Звук: шумоподавление и громкость")
        day_wav = audio.concat(rec_wavs, work / "day.wav")
        clean = audio.denoise(day_wav, work / "day_clean.wav", settings.denoise, settings.rnnoise_model)
        loud = audio.loudnorm(clean, work / "day_loud.wav")
        notify("Склейка")
        video.mux(segments, loud, out)

    return BuildResult(
        file=out,
        title=title(kind, day, part),
        description=description(recordings[0].start, recordings[-1].end),
        recorded_at=recordings[0].start,
        recorded_end=recordings[-1].end,
        duration=probe(out).duration,
        encoder=encoder,
        brightness_stops=stops_used,
    )


def _solve_brightness(paths: list[Path], seconds: float, settings: Settings, samples_file: Path,
                      src_range: str = "tv") -> float:
    # Не больше ~600 кадров на замер: раз в 2 с, для длинного дня — реже.
    every = max(2.0, seconds / 600)
    samples = brightness.sample_frames(paths, samples_file, every, src_range)
    try:
        return brightness.solve(samples, settings.lut, settings.profile, settings.target_luma)
    finally:
        samples.unlink(missing_ok=True)


def _start(p: Path) -> datetime | None:
    from .naming import parse_start

    return parse_start(p.name)


def build_pending(day: date, state: State, settings: Settings, kind: str | None = None,
                  notify: Notify = print) -> int | None:
    """Собирает ролик из ещё не использованных клипов дня (досъёмка → «часть 2»).

    Тип (тренировка / занятие) можно выбрать позже: он нужен только для названия при загрузке.
    Возвращает id ролика в журнале.
    """
    names = state.unassigned_clips(day)
    if not names:
        return None
    video_id, part = state.create_video(day, names, kind)
    folder = settings.archive / day.isoformat()
    out = folder / ("video.mp4" if part == 1 else f"video_part{part}.mp4")
    try:
        result = build_day(day, [folder / n for n in names], kind or "training", part, settings, out, notify)
    except Exception:
        state.update_video(video_id, status="failed")
        raise
    state.update_video(video_id, status="built", file=str(out),
                       rec_start=result.recorded_at.isoformat(), rec_end=result.recorded_end.isoformat())
    return video_id


def video_metadata(row) -> tuple[str, str, datetime]:
    """Название, описание и время начала съёмки для ролика из журнала."""
    start, end = datetime.fromisoformat(row["rec_start"]), datetime.fromisoformat(row["rec_end"])
    return title(row["kind"], date.fromisoformat(row["day"]), row["part"]), description(start, end), start


def upload_ready(state: State, settings: Settings, uploader: Callable[..., "object"], notify: Notify = print) -> list[int]:
    """Загружает собранные ролики с выбранным типом. uploader — youtube.upload с готовым входом."""
    done = []
    for row in state.videos("built"):
        if not row["kind"]:
            continue  # ждёт выбора «Тренировка / Занятие»
        name, desc, start = video_metadata(row)
        notify(f"Загружаю «{name}»")
        result = uploader(Path(row["file"]), name, desc, start)
        state.update_video(row["id"], status="uploaded", youtube_id=result.video_id, privacy=result.privacy)
        if result.privacy != "unlisted":
            notify(f"«{name}» загружено как {result.privacy}: YouTube ограничил доступ до аудита API — {result.url}")
        else:
            notify(f"«{name}» загружено: {result.url}")
        done.append(row["id"])
    return done

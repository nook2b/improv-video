"""Конвейер: импорт с карты → сборка ролика дня."""

from __future__ import annotations

import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from time import monotonic
from typing import Callable

from . import audio, autocolor, brightness, lut as lutlib, video
from .progress import DayProgress, Meter, StallWatch
from .grouping import Part, Recording, group_days, group_recordings
from .naming import DEFAULT_DAY_START, description, shooting_day, title
from .probe import probe
from .scan import Clip, find_camera_dirs, list_clips
from .state import State
from .tools import Cancelled, ToolError

GB = 1024**3
RESERVE_BYTES = 5 * GB  # macOS нужно немного свободного места для работы

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
    copy_clips: bool = True  # False — клипы читаются прямо с флешки, на Mac пишется только готовое видео
    workers: int = 0  # параллельных процессов кодирования; 0 — по числу ядер (до 4)
    chunk_seconds: float = 300  # длинные записи делятся на куски для параллельного кодирования


def auto_workers() -> int:
    """Куски, кодируемые одновременно. По замеру на Mac 3–4 куска быстрее двух на 10–30%;
    8 ядер и больше — 4 куска (M2 Pro: 10–12 ядер)."""
    return min(4, max(1, (os.cpu_count() or 1) // 2))


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
    color: str = ""  # что сделал автоцвет


class NotEnoughSpace(RuntimeError):
    pass


def estimate_build_bytes(durations: list[float], max_height: int = 2160) -> int:
    """Место на сборку одного дня: готовое видео + сегменты записей (живут до склейки) + WAV + запас."""
    seconds = sum(durations)
    if max_height >= 2160:
        bps = 60_000_000
    elif max_height >= 1440:
        bps = 24_000_000
    else:
        bps = 12_000_000
    final = seconds * bps / 8
    wav = seconds * audio.RATE * 2 * 2
    # Куски записи удаляются после склейки записи; одновременно живут сегменты и итоговый файл.
    # WAV: куски, склейка, подгонка, день, шумодав, громкость.
    return int(2 * final + 6 * wav + RESERVE_BYTES)


def free_bytes(folder: Path) -> int:
    """Сколько места можно занять. На Mac — как в «Хранилище»: вместе с тем, что macOS освобождает
    сама по требованию (локальные снимки Time Machine, кэши iCloud и т. п.). shutil.disk_usage видит
    только «чистое» свободное место: на заполненном Mac это 3 ГБ вместо 90."""
    try:
        from Foundation import NSURL, NSURLVolumeAvailableCapacityForImportantUsageKey

        ok, value, _ = NSURL.fileURLWithPath_(str(folder)).getResourceValue_forKey_error_(
            None, NSURLVolumeAvailableCapacityForImportantUsageKey, None)
        if ok and value is not None and int(value) > 0:
            return int(value)
    except Exception:  # noqa: BLE001 — не Mac или нет PyObjC
        pass
    return shutil.disk_usage(folder).free


def _check_space(folder: Path, need: int, what: str) -> None:
    free = free_bytes(folder)
    if free < need:
        raise NotEnoughSpace(f"{what}: нужно {need / GB:.0f} ГБ, свободно {free / GB:.0f} ГБ")


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


COPY_CHUNK = 16 * 1024 * 1024


def _copy_verified(src: Path, dst: Path, copied: Callable[[int], None] | None = None) -> None:
    """Копия через .part с проверкой размера; copied(байт) — для процента, остановка между кусками."""
    from .tools import check_cancel

    tmp = dst.with_name(dst.name + ".part")
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while chunk := fin.read(COPY_CHUNK):
                check_cancel()
                fout.write(chunk)
                if copied:
                    copied(len(chunk))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    shutil.copystat(src, tmp)
    if tmp.stat().st_size != src.stat().st_size:
        tmp.unlink(missing_ok=True)
        raise OSError(f"Размер копии не совпал: {src.name}")
    os.replace(tmp, dst)


def import_card(volume: Path, state: State, settings: Settings, notify: Notify = print,
                on_copy: Callable[[float], None] | None = None,
                summary: dict | None = None) -> dict[date, list[str]]:
    """Регистрирует новые клипы; при copy_clips копирует их в архив/ГГГГ-ММ-ДД/.

    Возвращает {день: [имена клипов]}. Без копирования клипы читаются с флешки при сборке.
    """
    clips = new_clips_on_card(volume, state)
    if not clips:
        return {}
    parts, broken = [], []
    for c in clips:
        try:
            parts.append(Part(c, probe(c.path)))
        except ToolError:  # запись оборвалась (села батарея): у файла нет оглавления, его не открыть
            broken.append(c)
            state.add_clip(c.name, c.size, shooting_day(c.start, settings.day_start), status="broken")
    if broken:
        notify(f"Повреждён и пропущен: {', '.join(c.name for c in broken)} — запись, видимо, оборвалась; "
               "остальные клипы обрабатываются")
    clips = [p.clip for p in parts]
    if not clips:
        return {}
    settings.archive.mkdir(parents=True, exist_ok=True)
    clips_bytes = sum(c.size for c in clips)
    hours = sum(p.media.duration for p in parts) / 3600
    if settings.copy_clips:
        # Место на сборку проверяется отдельно перед каждым днём
        _check_space(settings.archive, clips_bytes + RESERVE_BYTES,
                     f"Нет места для копии {len(clips)} клипов ({clips_bytes / GB:.0f} ГБ)")

    days = group_days(group_recordings(parts), settings.day_start)
    notify(f"Найдено {len(clips)} новых клипов ({clips_bytes / GB:.0f} ГБ, {hours:.1f} ч) за {len(days)} дн."
           + (", копирую…" if settings.copy_clips else ". Не вынимайте флешку до конца обработки"))
    result: dict[date, list[str]] = {}
    done = 0

    def copied(n: int) -> None:
        nonlocal done
        done += n
        if on_copy:
            on_copy(done / max(clips_bytes, 1))

    for day, recordings in days.items():
        if summary is not None:  # для окна «Что снимали?»: начало, конец, длительность, клипов
            summary[day] = (recordings[0].start, recordings[-1].end, sum(r.duration for r in recordings),
                            sum(len(r.parts) for r in recordings))
        folder = settings.archive / day.isoformat()
        folder.mkdir(exist_ok=True)
        for rec in recordings:
            for part in rec.parts:
                target = folder / part.clip.name
                if settings.copy_clips and part.clip.path.resolve() != target.resolve():
                    _copy_verified(part.clip.path, target, copied)
                state.add_clip(part.clip.name, part.clip.size, day)
                result.setdefault(day, []).append(part.clip.name)
    if settings.copy_clips:
        notify("Можно извлечь флешку")
    return result


def clip_locations(source: Path | None) -> dict[str, Path]:
    """Имя клипа → путь на флешке (или в папке), если она сейчас подключена."""
    if source is None:
        return {}
    return {c.name: c.path for folder in source_folders(source) for c in list_clips(folder)}


def build_day(
    day: date,
    clip_paths: list[Path],
    kind: str,
    part: int,
    settings: Settings,
    out: Path,
    notify: Notify = print,
    progress: DayProgress | None = None,
) -> BuildResult:
    """Собирает ролик из клипов одного дня съёмки; progress — для меню (этапы и проценты)."""
    clips = [Clip(p, p.name, p.stat().st_size, s) for p in clip_paths if (s := _start(p))]
    if not clips:
        raise ValueError("Нет клипов для сборки")
    recordings = group_recordings([Part(c, probe(c.path)) for c in clips])
    out.parent.mkdir(parents=True, exist_ok=True)
    total = sum(r.duration for r in recordings)
    _check_space(out.parent, estimate_build_bytes([total], settings.max_height),
                 f"Нет места для ролика {day:%d.%m.%Y} ({total / 3600:.1f} ч видео)")
    encoder = video.pick_encoder(settings.encoders)
    target = video.choose_target([p.media for r in recordings for p in r.parts], settings.max_height)

    out = Path(out).resolve()
    with tempfile.TemporaryDirectory(prefix="improv-", dir=out.parent) as tmp:
        work = Path(tmp)
        all_paths = [p.clip.path for r in recordings for p in r.parts]
        src_range = recordings[0].parts[0].media.color_range
        auto_lut, color_note = None, ""
        marks = [("замер", monotonic())]  # начала этапов — итог по времени в журнал
        if progress:
            progress.start("measure")
        measure = Meter(progress, "measure", total)
        if settings.auto_brightness and settings.lut and settings.profile == "ilog":
            # Автоцвет как «Авто» в Lumetri: одна оценка на день, всё запекается в LUT
            notify("Автоцвет: замер")
            samples = brightness.sample_frames(all_paths, work / "samples.mkv", max(2.0, total / 600), src_range,
                                              progress=measure.track("day"))
            auto_lut, grade = autocolor.solve(samples, settings.lut, work)
            color_note = grade.describe()
            notify("Автоцвет: " + color_note)
            stops_used = [grade.stops] * len(recordings)
        else:
            day_stops = None
            if settings.auto_brightness and settings.brightness_scope == "day":
                notify("Замер яркости")
                day_stops = _solve_brightness(all_paths, total, settings, work / "samples.mkv", src_range,
                                              measure.track("day"))
            stops_used = []
            for i, rec in enumerate(recordings, 1):
                if day_stops is not None:
                    stops_used.append(day_stops)
                elif settings.auto_brightness:
                    stops_used.append(_solve_brightness([p.clip.path for p in rec.parts], rec.duration, settings,
                                                        work / f"samples{i}.mkv", rec.parts[0].media.color_range,
                                                        measure.track(i)))
                    measure.add(i, rec.duration)
                else:
                    stops_used.append(0.0)

        # Для I-Log с LUT поправка запекается в LUT: на 4K-кадр меньше проходов
        baked: dict[float, Path] = {}

        def color_for(stops: float) -> tuple[Path | None, str | None]:
            if auto_lut:
                return auto_lut, None
            if settings.lut and settings.profile == "ilog":
                if abs(stops) < 1e-3:
                    return settings.lut, None
                if stops not in baked:
                    offset = stops * brightness.ILOG_CODES_PER_STOP / 876
                    baked[stops] = lutlib.bake_offset(settings.lut, work / f"lut_{len(baked)}.cube", offset)
                return baked[stops], None
            return settings.lut, brightness.adjust_filter(settings.profile, stops)

        # 3. Куски всех записей кодируются параллельно
        jobs = []
        for i, rec in enumerate(recordings, 1):
            lut_file, adjust = color_for(stops_used[i - 1])
            m = rec.parts[0].media
            for j, (start, frames) in enumerate(video.plan_chunks(rec.duration, target.fps, settings.chunk_seconds)):
                jobs.append((i, j, frames / float(target.fps), dict(clips=[p.clip.path for p in rec.parts], out=work / f"rec{i}_{j:03d}.mp4",
                                        target=target, encoder=encoder, lut=lut_file, adjust=adjust,
                                        x265_preset=settings.x265_preset, src_range=m.color_range,
                                        src_size=(m.width, m.height), start_frame=start, frames=frames)))
        done = 0
        marks.append(("кодирование", monotonic()))
        if progress:
            progress.start("encode")
        encode = Meter(progress, "encode", sum(job[2] for job in jobs))

        def run(job):
            nonlocal done
            t0 = monotonic()
            key = (job[0], job[1])
            start = job[3]["start_frame"] / float(target.fps)
            stats = encode.watch(key, f"запись {job[0]} с {_mmss(start)}")
            try:
                path = video.encode_chunk(**job[3], progress=encode.track(key), stats=stats)
            finally:
                encode.forget(key)
            encode.add(key, job[2])
            done += 1
            dt = monotonic() - t0
            notify(f"Кодирование: {done} из {len(jobs)} (кусок {_mmss(job[2])} за {_mmss(dt)}, "
                   f"×{job[2] / max(dt, 1e-3):.1f})")
            return path

        notify(f"Кодирование: 0 из {len(jobs)}")
        with StallWatch(encode, notify, lambda: read_check(all_paths)), \
                ThreadPoolExecutor(max_workers=settings.workers or auto_workers()) as pool:
            outputs = list(pool.map(run, jobs))

        marks.append(("звук", monotonic()))
        if progress:
            progress.start("audio")
        # Звук: по шагу на каждый кусок и запись, плюс склейка дня, шумодав и громкость.
        sound = Meter(progress, "audio", sum(len(r.parts) + 2 for r in recordings) + 3)
        step = 0

        def stepped(result):
            nonlocal step
            step += 1
            sound.add(step, 1)
            return result

        segments, rec_wavs = [], []
        for i, rec in enumerate(recordings, 1):
            parts = [o for (ri, _, _, _), o in zip(jobs, outputs) if ri == i]
            seg = video.concat_video(parts, work / f"rec{i}.mp4")
            segments.append(seg)
            # Звук записи: каждый кусок по длине своего видео, затем вся запись — по длине сегмента.
            piece_wavs = [
                stepped(audio.clip_audio(p.clip.path, p.media.duration, p.media.has_audio, work / f"rec{i}_{j}.wav"))
                for j, p in enumerate(rec.parts)
            ]
            joined = stepped(audio.concat(piece_wavs, work / f"rec{i}_joined.wav"))
            rec_wavs.append(stepped(audio.fit(joined, probe(seg).duration, work / f"rec{i}.wav")))

        notify("Звук: шумоподавление и громкость")
        day_wav = stepped(audio.concat(rec_wavs, work / "day.wav"))
        clean = stepped(audio.denoise(day_wav, work / "day_clean.wav", settings.denoise, settings.rnnoise_model))
        loud = stepped(audio.loudnorm(clean, work / "day_loud.wav"))
        notify("Склейка")
        marks.append(("склейка", monotonic()))
        if progress:
            progress.start("mux")
        video.mux(segments, loud, out, progress=Meter(progress, "mux", total).track("mux"))
        if progress:
            progress.finish()
        marks.append(("", monotonic()))
        spent = marks[-1][1] - marks[0][1]
        notify(f"Время: {_mmss(total)} видео за {_mmss(spent)} (×{total / max(spent, 1e-3):.1f}); "
               + ", ".join(f"{name} {_mmss(b - a)}" for (name, a), (_, b) in zip(marks, marks[1:])))

    return BuildResult(
        file=out,
        title=title(kind, day, part),
        description=description(recordings[0].start, recordings[-1].end),
        recorded_at=recordings[0].start,
        recorded_end=recordings[-1].end,
        duration=probe(out).duration,
        encoder=encoder,
        brightness_stops=stops_used,
        color=color_note,
    )


def _solve_brightness(paths: list[Path], seconds: float, settings: Settings, samples_file: Path,
                      src_range: str = "tv", progress=None) -> float:
    # Не больше ~600 кадров на замер: раз в 2 с, для длинного дня — реже.
    every = max(2.0, seconds / 600)
    samples = brightness.sample_frames(paths, samples_file, every, src_range, progress=progress)
    try:
        return brightness.solve(samples, settings.lut, settings.profile, settings.target_luma)
    finally:
        samples.unlink(missing_ok=True)


def read_check(paths: list[Path], size: int = 8 << 20, timeout: float = 20.0) -> str:
    """Насколько быстро сейчас читается источник: 8 МБ из случайного места самого длинного клипа
    (каждый раз другого — иначе ответит кэш Mac, а не флешка)."""
    import random
    import threading

    path = max(paths, key=lambda p: p.stat().st_size if p.exists() else -1, default=None)
    if path is None or not path.exists():
        return "клипы недоступны — флешка отключена?"
    result: list[float] = []

    def read():
        t0 = monotonic()
        with open(path, "rb", buffering=0) as f:
            f.seek(random.randrange(max(1, path.stat().st_size - size)))
            got = 0
            while got < size and (chunk := f.read(1 << 20)):
                got += len(chunk)
        result.append(monotonic() - t0)

    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(timeout)
    if not result:
        return f"чтение {size >> 20} МБ с источника не закончилось за {timeout:.0f} с"
    return f"источник: {size >> 20} МБ за {result[0]:.1f} с ({size / (1 << 20) / max(result[0], 1e-3):.0f} МБ/с)"


def _mmss(seconds: float) -> str:
    s = round(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _start(p: Path) -> datetime | None:
    from .naming import parse_start

    return parse_start(p.name)


def failure_reason(e: BaseException) -> str:
    """Коротко для меню: почему день не собрался."""
    if isinstance(e, Cancelled):
        return "обработку остановили"
    if isinstance(e, NotEnoughSpace):
        return "нет места на диске"
    if isinstance(e, ToolError):
        return "ошибка обработки видео"
    if isinstance(e, OSError):
        return "флешку вынули"
    return "ошибка, подробности в журнале"


def build_pending(day: date, state: State, settings: Settings, kind: str | None = None,
                  notify: Notify = print, source: Path | None = None,
                  progress: DayProgress | None = None) -> int | None:
    """Собирает ролик из ещё не использованных клипов дня (досъёмка → «часть 2»).

    Тип (тренировка / занятие) можно выбрать позже: он нужен только для названия при загрузке.
    Возвращает id ролика в журнале.
    """
    names = state.unassigned_clips(day)
    if not names:
        return None
    folder = settings.archive / day.isoformat()
    on_card = clip_locations(source)
    paths = [on_card.get(n, folder / n) for n in names]
    if not all(p.exists() for p in paths):
        return None  # клипы на флешке, а её нет — соберём при следующей вставке
    video_id, part = state.create_video(day, names, kind)
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / ("video.mp4" if part == 1 else f"video_part{part}.mp4")
    try:
        result = build_day(day, paths, kind or "training", part, settings, out, notify, progress)
    except Exception as e:
        state.release_video(video_id)
        state.set_failure(day, failure_reason(e))
        raise
    state.clear_failure(day)
    state.update_video(video_id, status="built", file=str(out),
                       rec_start=result.recorded_at.isoformat(), rec_end=result.recorded_end.isoformat())
    return video_id


def video_metadata(row) -> tuple[str, str, datetime]:
    """Название, описание и время начала съёмки для ролика из журнала."""
    start, end = datetime.fromisoformat(row["rec_start"]), datetime.fromisoformat(row["rec_end"])
    return title(row["kind"], date.fromisoformat(row["day"]), row["part"]), description(start, end), start


def upload_ready(state: State, settings: Settings, uploader: Callable[..., "object"], notify: Notify = print,
                 on_progress: Callable[[int, float], None] | None = None) -> list[int]:
    """Загружает собранные ролики с выбранным типом. uploader — youtube.upload с готовым входом.

    on_progress(id ролика, доля) — для процента в списке «Ролики».
    """
    done = []
    for row in state.videos("built"):
        if not row["kind"]:
            continue  # ждёт ответа на «Что снимали?»
        name, desc, start = video_metadata(row)
        notify(f"Загружаю «{name}»")
        extra = {"progress": lambda f, vid=row["id"]: on_progress(vid, f)} if on_progress else {}
        result = uploader(Path(row["file"]), name, desc, start, **extra)
        state.update_video(row["id"], status="uploaded", youtube_id=result.video_id, privacy=result.privacy,
                           done_at=datetime.now().isoformat(timespec="seconds"))
        if result.privacy != "unlisted":
            notify(f"«{name}» загружено как {result.privacy}: YouTube ограничил доступ до аудита API — {result.url}")
        else:
            notify(f"«{name}» загружено: {result.url}")
        done.append(row["id"])
    return done


def trash_done_videos(state: State, days: int, trash: Callable[[Path], None],
                      now: datetime | None = None) -> list[str]:
    """Файлы роликов, переданных в Studio или загруженных days дней назад и раньше, — в Корзину.
    Возвращает названия; days <= 0 — автоудаление выключено."""
    if days <= 0:
        return []
    from datetime import timedelta

    moved = []
    for row in state.done_before((now or datetime.now()) - timedelta(days=days)):
        path = Path(row["file"])
        if path.exists():
            trash(path)
        state.update_video(row["id"], file=None)
        moved.append(title(row["kind"], date.fromisoformat(row["day"]), row["part"]) if row["kind"]
                     else path.name)
    return moved


@dataclass
class VideoItem:
    """Строка списка «Ролики» в меню."""

    key: str  # «v<id>» для ролика или «d<день>» для несобранного дня
    title: str  # «Тренировка 06.10.2026» или «07.10.2026», пока тип не выбран
    status: str  # building | kind_needed | manual | handed | queued | uploading | uploaded | failed
    detail: str  # вторая строка: «Ждёт выбора типа · 18:05–20:40», «На YouTube · по ссылке»…
    day: date
    video_id: int | None = None
    fraction: float | None = None  # сборка или загрузка, 0..1
    url: str | None = None


def _span(row) -> str:
    if not row["rec_start"] or not row["rec_end"]:
        return ""
    start, end = datetime.fromisoformat(row["rec_start"]), datetime.fromisoformat(row["rec_end"])
    return f"{start:%H:%M}–{end:%H:%M}"


def recent_videos(state: State, *, upload_mode: str = "manual", uploading: dict[int, float] | None = None,
                  building: tuple[date, float] | None = None, limit: int = 10) -> list[VideoItem]:
    """Последние ролики и несобранные дни, новые сверху."""
    uploading = uploading or {}
    items: list[VideoItem] = []
    for row in state.videos():
        day = date.fromisoformat(row["day"])
        vid, status = row["id"], row["status"]
        if status == "pending":
            continue  # собирается — строку даёт building ниже
        if row["kind"]:
            name = title(row["kind"], day, row["part"])
        else:
            name = f"{day:%d.%m.%Y}" + (f" (часть {row['part']})" if row["part"] > 1 else "")
        span = _span(row)
        gone = " · файл в Корзине" if status in ("handed", "uploaded") and not row["file"] else ""
        if status == "uploaded":
            where = "по ссылке" if row["privacy"] == "unlisted" else "приватно"
            items.append(VideoItem(f"v{vid}", name, "uploaded", f"На YouTube · {where}{gone}", day, vid,
                                   url=f"https://youtu.be/{row['youtube_id']}"))
        elif not row["kind"]:
            items.append(VideoItem(f"v{vid}", name, "kind_needed",
                                   "Ждёт выбора типа" + (f" · {span}" if span else ""), day, vid))
        elif vid in uploading:
            items.append(VideoItem(f"v{vid}", name, "uploading", f"Загружается · {uploading[vid]:.0%}", day, vid,
                                   fraction=uploading[vid]))
        elif status == "handed":
            items.append(VideoItem(f"v{vid}", name, "handed", "Передан в YouTube Studio" + gone, day, vid))
        elif status == "manual" or (status == "built" and upload_mode == "manual"):
            items.append(VideoItem(f"v{vid}", name, "manual", "Загрузить вручную", day, vid))
        else:
            items.append(VideoItem(f"v{vid}", name, "queued", "Ждёт загрузки на YouTube", day, vid))
    for day, reason in state.failures().items():
        if building and building[0] == day:
            continue
        items.append(VideoItem(f"d{day.isoformat()}", f"{day:%d.%m.%Y}", "failed", f"Не собрался: {reason}", day))
    if building:
        day, fraction = building
        items.append(VideoItem(f"d{day.isoformat()}", f"{day:%d.%m.%Y}", "building",
                               f"Собирается · {fraction:.0%}", day, fraction=fraction))
    order = {"building": 2}  # при сортировке по убыванию собираемый день — первым
    items.sort(key=lambda it: (it.day, order.get(it.status, 1), it.key), reverse=True)
    return items[:limit]

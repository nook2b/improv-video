import json
import subprocess
from datetime import date

import pytest

from improv_video import audio, brightness
from improv_video.pipeline import (NotEnoughSpace, Settings, build_pending, import_card, mark_existing_as_done,
                                   new_clips_on_card, upload_ready, video_metadata)
from improv_video.youtube import UploadResult
from improv_video.probe import probe
from improv_video.state import State
from improv_video.video import Target, choose_target, bitrate_for
from fractions import Fraction

from conftest import make_clip


def streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"]


def test_clip_audio_matches_video_length(card, tmp_path):
    clip = card / "DCIM" / "Camera01" / "VID_20261001_180500_00_578.mp4"
    m = probe(clip)
    wav = audio.clip_audio(clip, m.duration, m.has_audio, tmp_path / "a.wav")
    assert int(streams(wav)[0]["duration_ts"]) == audio.samples(m.duration)
    silent = audio.clip_audio(clip, 1.5, False, tmp_path / "s.wav")
    assert int(streams(silent)[0]["duration_ts"]) == 72000


def test_target_and_bitrate():
    from improv_video.probe import Media
    ms = [Media(100, 3840, 2160, Fraction(30), True), Media(10, 1920, 1080, Fraction(60), True),
          Media(500, 1080, 1920, Fraction(30), True)]
    t = choose_target(ms)
    assert (t.width, t.height, t.fps) == (3840, 2160, Fraction(30))
    eight_k = choose_target([Media(10, 7680, 4320, Fraction(30), True)])
    assert (eight_k.width, eight_k.height) == (3840, 2160)
    assert bitrate_for(Target(3840, 2160, Fraction(60))) == 60_000_000
    assert bitrate_for(Target(3840, 2160, Fraction(30))) == 40_000_000


def test_dark_clip_gets_brighter(tmp_path):
    clip = make_clip(tmp_path / "VID_20261001_100000_00_001.mp4", 4, vf="eq=brightness=-0.25")
    samples = brightness.sample_frames([clip], tmp_path / "samples.mkv", every=1.0)
    stops = brightness.solve(samples, None, "normal")
    assert 0 < stops <= brightness.MAX_STOPS
    assert brightness.measure(samples, None, "normal", stops) > brightness.measure(samples, None, "normal", 0)


def test_full_day(card, settings, identity_lut):
    settings.lut = identity_lut
    settings.auto_brightness = True
    state = State(settings.archive / "state.sqlite")
    days = import_card(card, state, settings, notify=lambda _: None)
    assert list(days) == [date(2026, 10, 1), date(2026, 10, 3)]
    assert new_clips_on_card(card, state) == []  # повторная вставка карты — ничего нового

    vid = build_pending(date(2026, 10, 1), state, settings, notify=lambda _: None)  # тип ещё не выбран
    row = state.video(vid)
    assert row["status"] == "built" and row["kind"] is None
    state.update_video(vid, kind="lesson")
    name, desc, start = video_metadata(state.video(vid))
    assert name == "Занятие 01.10.2026"
    assert desc == ""  # без описания: время съёмки YouTube превращал в таймкоды
    assert start.hour == 18

    v, a = sorted(streams(row["file"]), key=lambda s: s["codec_type"], reverse=True)
    assert v["codec_name"] == "hevc" and v["pix_fmt"] == "yuv420p10le"
    assert (v["width"], v["height"]) == (640, 360)
    assert v["color_primaries"] == "bt709" and v["color_transfer"] == "bt709" and v["color_space"] == "bt709"
    assert a["codec_name"] == "aac"
    # 3 + 3 + 2 + 2 секунды, звук не разъехался с видео
    assert abs(float(v["duration"]) - 10) < 0.1
    assert abs(float(a["duration"]) - float(v["duration"])) < 0.05

    assert build_pending(date(2026, 10, 1), state, settings) is None  # всё уже собрано
    vertical = build_pending(date(2026, 10, 3), state, settings, "training", notify=lambda _: None)

    uploaded = []

    def fake_upload(file, title, description, recorded_at):
        assert description == ""  # без описания: время съёмки YouTube превращал в таймкоды
        uploaded.append(title)
        return UploadResult("abc123", "private" if title.startswith("Тренировка") else "unlisted")

    assert upload_ready(state, settings, fake_upload, notify=lambda _: None) == [vid, vertical]
    assert uploaded == ["Занятие 01.10.2026", "Тренировка 03.10.2026"]
    assert state.video(vertical)["privacy"] == "private"


def test_first_run_skip(card, settings):
    state = State(settings.archive / "state.sqlite")
    assert mark_existing_as_done(card, state, settings) == 5
    assert import_card(card, state, settings, notify=lambda _: None) == {}


def test_space_checked_per_day_not_for_whole_card(card, settings, monkeypatch):
    """Без копирования импорт не требует места; нехватка на сборку дня не теряет клипы."""
    import shutil
    from collections import namedtuple

    settings.copy_clips = False
    state = State(settings.archive / "state.sqlite")
    days = import_card(card, state, settings, notify=lambda _: None)
    assert days
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _: Usage(0, 0, 1))
    day = sorted(days)[0]
    with pytest.raises(NotEnoughSpace, match="Нет места для ролика"):
        build_pending(day, state, settings, notify=lambda _: None, source=card)
    assert state.unassigned_clips(day)  # день соберётся при следующей вставке


def test_ilog_offset_before_lut(tmp_path, identity_lut):
    clip = make_clip(tmp_path / "VID_20261001_100000_00_001.mp4", 2, vf="eq=brightness=-0.2")
    samples = brightness.sample_frames([clip], tmp_path / "samples.mkv", every=1.0)
    base = brightness.measure(samples, identity_lut, "ilog", 0)
    up = brightness.measure(samples, identity_lut, "ilog", 0.5)
    down = brightness.measure(samples, identity_lut, "ilog", -0.5)
    assert down < base < up


def test_full_range_input_matches_limited(tmp_path, identity_lut):
    """Ace Pro 2 пишет 8 бит полного диапазона (yuvj420p): яркость должна совпасть с той же картинкой в 16–235."""
    src = make_clip(tmp_path / "src.mp4", 2, audio_seconds=0)
    full = tmp_path / "VID_20261001_100000_00_001.mp4"
    limited = tmp_path / "VID_20261001_100000_00_002.mp4"
    for path, rng in ((full, "pc"), (limited, "tv")):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                        "-vf", f"scale=out_range={rng}", "-color_range", rng, "-c:v", "libx265",
                        "-preset", "ultrafast", "-pix_fmt", "yuvj420p" if rng == "pc" else "yuv420p",
                        "-x265-params", "log-level=error", str(path)], check=True)
    assert probe(full).color_range == "pc" and probe(limited).color_range == "tv"
    values = []
    for path in (full, limited):
        s = brightness.sample_frames([path], tmp_path / f"{path.stem}.mkv", 1.0, probe(path).color_range)
        values.append(brightness.measure(s, identity_lut, "ilog", 0))
    assert abs(values[0] - values[1]) < 0.01, values


def test_broken_clip_is_skipped_not_whole_card(tmp_path, settings):
    """Оборванная запись (нет moov) не должна останавливать всю карту."""
    card = tmp_path / "card"
    cam = card / "DCIM" / "Camera01"
    make_clip(cam / "VID_20261001_180500_00_001.mp4", 2)
    broken = cam / "VID_20260719_194558_00_578.mp4"
    broken.write_bytes((cam / "VID_20261001_180500_00_001.mp4").read_bytes()[:4000])  # обрыв: только начало
    state = State(settings.archive / "state.sqlite")
    notes = []
    days = import_card(card, state, settings, notify=notes.append)
    assert [names for names in days.values()] == [["VID_20261001_180500_00_001.mp4"]]
    assert any(n.startswith("Повреждённый клип пропущен: VID_20260719_194558_00_578.mp4") for n in notes)
    assert import_card(card, state, settings, notify=notes.append) == {}  # второй раз не спотыкается


def test_archive_on_camera_card_is_detected(tmp_path):
    from improv_video.pipeline import is_camera_card

    volumes = tmp_path / "Volumes"
    (volumes / "HotBaby" / "DCIM" / "Camera01").mkdir(parents=True)
    (volumes / "Backup" / "improv").mkdir(parents=True)
    assert is_camera_card(volumes / "HotBaby" / "DCIM" / "Camera01", volumes)
    assert is_camera_card(volumes / "HotBaby", volumes)
    assert not is_camera_card(volumes / "Backup" / "improv", volumes)  # внешний диск без DCIM — можно
    assert not is_camera_card(tmp_path / "Movies", volumes)

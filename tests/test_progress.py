import threading
import time
from datetime import date

import pytest

from improv_video import tools
from improv_video.pipeline import build_pending, import_card, recent_videos
from improv_video.progress import STALL_SECONDS, DayProgress, Meter
from improv_video.state import State


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_stages_overall_eta_and_stall():
    clock = Clock()
    p = DayProgress("Тренировка 06.10.2026", 1, 2, clock=clock)
    p.start("measure")
    clock.t = 60
    p.set("measure", 1.0)
    p.start("encode")
    m = Meter(p, "encode", 100)
    a, b = m.track("a"), m.track("b")
    clock.t = 120
    a(20)
    b(30)  # два куска параллельно: 50 из 100 секунд
    assert p.overall() == pytest.approx(0.15 + 0.70 * 0.5)
    assert p.eta_seconds() == pytest.approx(120 / 0.5 * 0.5)
    stages = {s.key: s for s in p.stages()}
    assert stages["measure"].state == "done" and stages["measure"].seconds == 60
    assert stages["encode"].state == "active" and stages["encode"].fraction == pytest.approx(0.5)
    assert stages["audio"].state == "pending"
    assert "день 1 из 2" in p.summary() and "Кодирование 50%" in p.summary()

    assert p.stalled_for() == 0
    clock.t = 120 + STALL_SECONDS + 1
    assert p.stalled_for() > STALL_SECONDS
    assert "без изменений" in p.summary()
    a(25)  # снова двигается
    assert p.stalled_for() == 0

    p.finish()
    assert p.overall() == pytest.approx(1.0)


def test_ffmpeg_reports_progress_seconds(tmp_path):
    seen = []
    tools.ffmpeg(["-f", "lavfi", "-i", "testsrc2=s=160x90:r=25:d=4", "-c:v", "libx264", "-preset", "ultrafast",
                  str(tmp_path / "out.mp4")], progress=seen.append)
    assert seen and max(seen) == pytest.approx(4, abs=0.2)


def test_cancel_stops_running_ffmpeg(tmp_path):
    tools.reset_cancel()
    errors = []

    def long_run():
        try:
            tools.ffmpeg(["-re", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=25:d=60", "-f", "null", "-"])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=long_run)
    t.start()
    time.sleep(1.0)
    started = time.monotonic()
    tools.cancel_all()
    t.join(10)
    assert not t.is_alive() and time.monotonic() - started < 5
    assert errors and isinstance(errors[0], tools.Cancelled)
    with pytest.raises(tools.Cancelled):
        tools.ffmpeg(["-version"])  # после остановки новые процессы не стартуют
    tools.reset_cancel()
    tools.ffmpeg(["-version"])


def test_build_reports_all_stages_and_failures_are_listed(card, settings):
    settings.copy_clips = False
    state = State(settings.archive / "state.sqlite")
    days = import_card(card, state, settings, notify=lambda _: None)
    day = date(2026, 10, 1)
    assert day in days

    progress = DayProgress("01.10.2026", day=day)
    seen = set()
    real_set = progress.set

    def spy(stage, fraction):
        seen.add(stage)
        real_set(stage, fraction)

    progress.set = spy
    vid = build_pending(day, state, settings, notify=lambda _: None, source=card, progress=progress)
    assert vid is not None
    assert seen >= {"encode", "audio", "mux"}  # замер выключен в настройках тестов
    assert progress.overall() == pytest.approx(1.0)

    # Остановка посреди дня: клипы свободны, день виден как «не собрался»
    other = date(2026, 10, 3)
    tools.cancel_all()
    try:
        with pytest.raises(tools.Cancelled):
            build_pending(other, state, settings, notify=lambda _: None, source=card)
    finally:
        tools.reset_cancel()
    assert state.unassigned_clips(other)
    items = {it.key: it for it in recent_videos(state)}
    assert items[f"d{other.isoformat()}"].status == "failed"
    assert "остановили" in items[f"d{other.isoformat()}"].detail
    assert items[f"v{vid}"].status == "kind_needed"
    assert items[f"v{vid}"].detail.startswith("Ждёт выбора типа · 18:05–")

    state.set_day_kind(day, "lesson")
    items = {it.key: it for it in recent_videos(state, upload_mode="manual")}
    assert items[f"v{vid}"].title == "Занятие 01.10.2026" and items[f"v{vid}"].status == "manual"
    items = {it.key: it for it in recent_videos(state, upload_mode="api", uploading={vid: 0.4})}
    assert items[f"v{vid}"].status == "uploading" and items[f"v{vid}"].detail == "Загружается · 40%"

    # Собрался со второй попытки — отметка о сбое снимается
    assert build_pending(other, state, settings, notify=lambda _: None, source=card) is not None
    assert f"d{other.isoformat()}" not in {it.key for it in recent_videos(state)}

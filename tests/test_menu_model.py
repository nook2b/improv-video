from datetime import date

from improv_video.app import menu_model as mm
from improv_video.pipeline import VideoItem
from improv_video.progress import STALL_SECONDS, DayProgress, Meter

D = date(2026, 10, 7)


def item(status, title="Тренировка 06.10.2026", detail="", **kw):
    return VideoItem(f"k{status}{title}", title, status, detail, D, **kw)


def test_idle_and_last_video():
    block = mm.status_block(progress=None, status="Жду флешку", busy=False, note=None, videos=[])
    assert (block.title, block.subtitle, block.tone) == (
        "Жду флешку", "Вставьте карту камеры — ролик дня соберётся сам", mm.IDLE)
    block = mm.status_block(progress=None, status="", busy=False, note=None,
                            videos=[item("uploaded", "Тренировка 05.10.2026")])
    assert block.subtitle == "Последний: Тренировка 05.10.2026 · на YouTube"


def test_waiting_videos_and_icon():
    videos = [item("kind_needed", "07.10.2026"), item("manual"), item("uploaded", "Занятие 05.10.2026")]
    block = mm.status_block(progress=None, status="", busy=False, note=None, videos=videos)
    assert block.title == "2 ролика ждут вас" and block.subtitle == "Выбрать тип · загрузить вручную"
    assert block.tone == mm.WARNING
    assert mm.waiting_count(videos) == 2
    assert mm.icon_state(progress=None, busy=False, videos=videos) == ("waiting", "")
    assert mm.icon_state(progress=None, busy=False, videos=videos + [item("failed", "04.10.2026")]) == ("error", "")
    one = mm.status_block(progress=None, status="", busy=False, note=None, videos=videos[:1])
    assert one.title == "1 ролик ждёт вас" and one.subtitle == "Выбрать тип"


def test_note_wins_over_idle():
    block = mm.status_block(progress=None, status="", busy=False,
                            note=("Новых клипов нет", "Всё на карте уже обработано. Можно извлечь флешку"), videos=[])
    assert block.icon == "circle-check" and block.title == "Новых клипов нет"


def test_progress_block_matches_design():
    class Clock:
        t = 0.0

        def __call__(self):
            return self.t

    clock = Clock()
    p = DayProgress("Тренировка 06.10.2026", 1, 2, reading_card=True, clock=clock)
    p.start("measure")
    clock.t = 180
    p.start("encode")
    Meter(p, "encode", 100).track("a")(61)
    clock.t = 240
    block = mm.status_block(progress=p, status="", busy=True, note=None, videos=[])
    b = block.progress
    assert (b.title, b.day_note, b.reading_card) == ("Тренировка 06.10.2026", "· день 1 из 2", True)
    assert [s.state for s in b.stages] == ["done", "active", "pending", "pending"]
    assert b.stages[0].right == "3 мин" and b.stages[1].right == "61%"
    assert b.percent == f"{0.15 + 0.7 * 0.61:.0%}" and b.right.startswith("осталось ~")
    assert mm.icon_state(progress=p, busy=True, videos=[]) == ("work", b.percent)

    clock.t = 240 + STALL_SECONDS + 60
    b = mm.status_block(progress=p, status="", busy=True, note=None, videos=[]).progress
    assert b.stalled and b.right.startswith("без изменений") and b.stages[1].stalled


def test_video_rows():
    rows = {r.item.status: r for r in map(mm.video_row, [
        item("kind_needed", "07.10.2026", "Ждёт выбора типа · 18:05–20:40"),
        item("uploading", detail="Загружается · 40%", fraction=0.4),
        item("manual", detail="Загрузить вручную"),
        item("uploaded", detail="На YouTube · по ссылке", url="https://youtu.be/x"),
        item("failed", "04.10.2026", "Не собрался: флешку вынули"),
    ])}
    assert rows["kind_needed"].accessory == "Выбрать…" and rows["kind_needed"].action == "kind"
    assert rows["uploading"].fraction == 0.4 and rows["uploading"].action is None
    assert rows["manual"].accessory == "В Studio" and rows["manual"].accessory_icon == "arrow-right"
    assert rows["uploaded"].accessory_icon == "external-link" and rows["uploaded"].action == "open_url"
    assert rows["failed"].detail_danger and rows["failed"].accessory == "при вставке"


def test_values_and_paths():
    assert mm.youtube_value(False, "api") == "не вошли"
    assert mm.youtube_value(True, "manual") == "вручную"
    assert mm.quality_value(2160) == "4K" and mm.quality_value(1080) == "1080p"
    assert mm.short_path("/Users/ivan/Movies/improv-video", "/Users/ivan") == "~/Movies/improv-video"
    name = mm.ellipsize_middle("AcePro2_I-Log_To_Rec.709_V1.0.cube")
    assert len(name) == 28 and "…" in name and name.endswith(".cube")

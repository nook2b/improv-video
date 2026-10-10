from datetime import date

from improv_video.state import State


def test_parts_and_assignment(tmp_path):
    s = State(tmp_path / "s.sqlite")
    d = date(2026, 10, 1)
    assert s.is_empty()
    s.add_clip("a.mp4", 10, d)
    s.add_clip("b.mp4", 20, d)
    assert s.is_known("a.mp4", 10) and not s.is_known("a.mp4", 11)
    vid, part = s.create_video(d, ["a.mp4", "b.mp4"], "training")
    assert part == 1 and s.unassigned_clips(d) == []
    s.add_clip("c.mp4", 30, d)  # досняли в тот же день
    vid2, part2 = s.create_video(d, s.unassigned_clips(d), "training")
    assert part2 == 2 and vid2 != vid
    s.update_video(vid, status="uploaded", youtube_id="xyz")
    assert s.video(vid)["youtube_id"] == "xyz"


def test_old_journal_gets_done_at_and_trash_rule(tmp_path):
    import sqlite3
    from datetime import date, datetime, timedelta

    from improv_video.pipeline import trash_done_videos
    from improv_video.state import State

    db = tmp_path / "state.sqlite"
    con = sqlite3.connect(db)  # журнал версии без done_at
    con.executescript("""CREATE TABLE videos (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
        part INTEGER NOT NULL, kind TEXT, status TEXT NOT NULL, file TEXT, rec_start TEXT, rec_end TEXT,
        youtube_id TEXT, privacy TEXT, UNIQUE (day, part));""")
    f = tmp_path / "video.mp4"
    f.write_bytes(b"x")
    con.execute("INSERT INTO videos (day, part, kind, status, file) VALUES ('2026-09-08', 1, 'masterclass', 'handed', ?)",
                (str(f),))
    con.execute("INSERT INTO videos (day, part, kind, status, file) VALUES ('2026-09-09', 1, 'lesson', 'manual', ?)",
                (str(f),))
    con.commit()
    con.close()

    state = State(db)
    assert state.video(1)["done_at"] and state.video(2)["done_at"] is None
    gone = []
    assert trash_done_videos(state, 3, gone.append) == []  # отсчёт пошёл с обновления
    assert trash_done_videos(state, 0, gone.append, now=datetime.now() + timedelta(days=30)) == []  # выключено
    moved = trash_done_videos(state, 3, gone.append, now=datetime.now() + timedelta(days=3, minutes=1))
    assert moved == ["Мастер-класс 08.09.2026"] and gone == [f]
    assert state.video(1)["file"] is None and state.video(2)["file"] == str(f)  # «загрузить вручную» не трогаем
    assert date.fromisoformat(state.video(1)["day"]) == date(2026, 9, 8)


def test_finish_uploaded_playlist_and_thumbnail(tmp_path):
    from datetime import date

    from improv_video.pipeline import finish_uploaded
    from improv_video.state import State
    from improv_video.youtube import ThumbnailNotAllowed

    state = State(tmp_path / "state.sqlite")
    vid, _ = state.create_video(date(2026, 10, 8), [], "show")
    state.update_video(vid, status="uploaded", youtube_id="yt1")
    state.set_day_playlist(date(2026, 10, 8), "PLa", "Команда А")
    thumb = tmp_path / "t.jpg"
    thumb.write_bytes(b"x")
    state.update_video(vid, thumbnail=str(thumb))
    added, thumbs, notes = [], [], []
    finish_uploaded(state, lambda pl, v: added.append((pl, v)), lambda v, img: thumbs.append(v), notes.append)
    finish_uploaded(state, lambda pl, v: added.append((pl, v)), lambda v, img: thumbs.append(v), notes.append)
    assert added == [("PLa", "yt1")] and thumbs == ["yt1"]  # второй раз — ничего не повторяет
    assert state.video(vid)["thumbnail"] is None and not thumb.exists()
    assert "«Шоу 08.10.2026» добавлен в плейлист «Команда А»" in notes

    # Сбой сети при добавлении — повторится в следующий раз; запрет обложек — сообщение, без повторов
    state.set_day_playlist(date(2026, 10, 8), "PLb", "Другая")
    state.update_video(vid, thumbnail=str(thumb))
    thumb.write_bytes(b"x")

    def broken(pl, v):
        raise ConnectionError("нет сети")

    def forbidden(v, img):
        raise ThumbnailNotAllowed("подтвердите телефон")

    finish_uploaded(state, broken, forbidden, notes.append)
    assert state.video(vid)["in_playlist"] == "PLa" and any("Повторю позже" in n for n in notes)
    finish_uploaded(state, lambda pl, v: added.append((pl, v)), forbidden, notes.append)
    assert added[-1] == ("PLb", "yt1") and "подтвердите телефон" in notes
    assert state.video(vid)["thumbnail"] is None


def test_discard_forget_and_hide(tmp_path):
    from datetime import date

    from improv_video.pipeline import discard_video, forget_day, recent_videos
    from improv_video.state import State

    archive = tmp_path / "Footage"
    state = State(archive / "state.sqlite")
    trashed = []

    def trash(p):
        trashed.append(p.name)
        p.unlink()

    # 18.08: собран, ждёт выбора типа — «Не загружать»
    d1 = date(2026, 8, 18)
    (archive / "2026-08-18").mkdir(parents=True)
    for n in ("VID_a.mp4", "video.mp4"):
        (archive / "2026-08-18" / n).write_bytes(b"x")
    state.add_clip("VID_a.mp4", 1, d1)
    vid, _ = state.create_video(d1, ["VID_a.mp4"])
    state.update_video(vid, status="built", file=str(archive / "2026-08-18" / "video.mp4"))
    # 23.01: не собрался — «Забыть день»
    d2 = date(2026, 1, 23)
    state.add_clip("VID_b.mp4", 1, d2)
    state.set_failure(d2, "нет места на диске")
    # 25.08: на YouTube — «Убрать из списка»
    d3 = date(2026, 8, 25)
    up, _ = state.create_video(d3, [], "lesson")
    state.update_video(up, status="uploaded", youtube_id="yt", rec_start="2026-08-25T18:00:00",
                       rec_end="2026-08-25T20:00:00")
    assert {it.status for it in recent_videos(state)} == {"kind_needed", "failed", "uploaded"}

    discard_video(state, vid, archive, trash)
    forget_day(state, d2, archive, trash)
    state.update_video(up, hidden=1)

    assert recent_videos(state) == []
    assert sorted(trashed) == ["VID_a.mp4", "video.mp4"]  # готовый ролик и копия клипа
    assert state.days_with_unassigned() == [] and state.failures() == {}
    assert state.is_known("VID_b.mp4", 1)  # при вставке флешки день не появится снова
    assert state.video(up)["status"] == "uploaded"  # на YouTube ничего не меняется

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

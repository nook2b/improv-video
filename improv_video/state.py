"""Журнал состояния в SQLite: какие клипы уже взяты и что стало с роликами."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    name   TEXT NOT NULL,
    size   INTEGER NOT NULL,
    day    TEXT NOT NULL,
    status TEXT NOT NULL,          -- copied | skipped (помечен как уже обработанный) | broken (не открывается)
                                   -- | discarded (день отменён: «Не загружать» / «Забыть день»)
    video  INTEGER REFERENCES videos(id),
    PRIMARY KEY (name, size)
);
CREATE TABLE IF NOT EXISTS days (
    day  TEXT PRIMARY KEY,
    kind TEXT                      -- training | lesson | show | masterclass: ответ на «Что снимали?» хранится за днём
);
CREATE TABLE IF NOT EXISTS videos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day        TEXT NOT NULL,
    part       INTEGER NOT NULL,
    kind       TEXT,               -- training | lesson | show | masterclass; NULL, пока не выбран
    status     TEXT NOT NULL,      -- pending | built | manual | handed | uploaded | discarded («Не загружать»)
    file       TEXT,
    rec_start  TEXT,               -- начало и конец съёмки (местное время камеры, ISO)
    rec_end    TEXT,
    youtube_id TEXT,
    privacy    TEXT,               -- что ответил YouTube: unlisted | private | public
    done_at    TEXT,               -- когда передан в Studio или загружен: от него считается автоудаление
    UNIQUE (day, part)
);
CREATE TABLE IF NOT EXISTS failures (
    day    TEXT PRIMARY KEY,       -- день не собрался; клипы ждут следующей вставки флешки
    reason TEXT NOT NULL,
    at     TEXT NOT NULL
);
"""


class State:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        day_cols = {r[1] for r in self.db.execute("PRAGMA table_info(days)")}
        with self.db:
            for col in ("playlist_id", "playlist_title"):  # плейлист дня (команда / мастер-классы)
                if col not in day_cols:
                    self.db.execute(f"ALTER TABLE days ADD COLUMN {col} TEXT")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(videos)")}
        with self.db:
            if "in_playlist" not in cols:  # id плейлиста, куда ролик уже добавлен
                self.db.execute("ALTER TABLE videos ADD COLUMN in_playlist TEXT")
            if "thumbnail" not in cols:  # подготовленная обложка, ждёт отправки на YouTube
                self.db.execute("ALTER TABLE videos ADD COLUMN thumbnail TEXT")
            if "hidden" not in cols:  # убран из списка «Ролики»
                self.db.execute("ALTER TABLE videos ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        if "done_at" not in cols:
            # Ролики, переданные до появления автоудаления, считаются переданными сейчас
            with self.db:
                self.db.execute("ALTER TABLE videos ADD COLUMN done_at TEXT")
                self.db.execute("UPDATE videos SET done_at = ? WHERE status IN ('handed', 'uploaded')",
                                (datetime.now().isoformat(timespec="seconds"),))

    def close(self) -> None:
        self.db.close()

    def is_empty(self) -> bool:
        return self.db.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 0

    def is_known(self, name: str, size: int) -> bool:
        row = self.db.execute("SELECT 1 FROM clips WHERE name = ? AND size = ?", (name, size))
        return row.fetchone() is not None

    def add_clip(self, name: str, size: int, day: date, status: str = "copied") -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO clips (name, size, day, status) VALUES (?, ?, ?, ?)",
                (name, size, day.isoformat(), status),
            )

    def forget_skipped(self) -> int:
        """Снимает отметку «уже обработан» с клипов первого запуска; их снова можно импортировать."""
        with self.db:
            return self.db.execute("DELETE FROM clips WHERE status = 'skipped'").rowcount

    def release_video(self, video_id: int) -> None:
        """Несобранный ролик (сбой, флешку вынули): клипы снова свободны, при следующей вставке соберутся."""
        with self.db:
            self.db.execute("UPDATE clips SET video = NULL WHERE video = ?", (video_id,))
            self.db.execute("DELETE FROM videos WHERE id = ?", (video_id,))

    def days_with_unassigned(self) -> list[date]:
        rows = self.db.execute(
            "SELECT DISTINCT day FROM clips WHERE status = 'copied' AND video IS NULL ORDER BY day")
        return [date.fromisoformat(r["day"]) for r in rows]

    def unassigned_clips(self, day: date) -> list[str]:
        rows = self.db.execute(
            "SELECT name FROM clips WHERE day = ? AND status = 'copied' AND video IS NULL ORDER BY name",
            (day.isoformat(),),
        )
        return [r["name"] for r in rows]

    def set_day_kind(self, day: date, kind: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO days (day, kind) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET kind = ?",
                            (day.isoformat(), kind, kind))
            self.db.execute("UPDATE videos SET kind = ? WHERE day = ? AND status IN ('pending', 'built')",
                            (kind, day.isoformat()))

    def set_day_playlist(self, day: date, playlist_id: str | None, playlist_title: str | None) -> None:
        with self.db:
            self.db.execute("INSERT INTO days (day, playlist_id, playlist_title) VALUES (?, ?, ?) "
                            "ON CONFLICT(day) DO UPDATE SET playlist_id = excluded.playlist_id, "
                            "playlist_title = excluded.playlist_title",
                            (day.isoformat(), playlist_id, playlist_title))

    def day_playlist(self, day: date) -> tuple[str, str] | None:
        row = self.db.execute("SELECT playlist_id, playlist_title FROM days WHERE day = ?",
                              (day.isoformat(),)).fetchone()
        return (row["playlist_id"], row["playlist_title"] or "") if row and row["playlist_id"] else None

    def day_kind(self, day: date) -> str | None:
        row = self.db.execute("SELECT kind FROM days WHERE day = ?", (day.isoformat(),)).fetchone()
        return row["kind"] if row else None

    def create_video(self, day: date, clip_names: list[str], kind: str | None = None) -> tuple[int, int]:
        """Новый ролик за день с номером части; возвращает (id, part)."""
        with self.db:
            part = self.db.execute(
                "SELECT COALESCE(MAX(part), 0) + 1 FROM videos WHERE day = ?", (day.isoformat(),)
            ).fetchone()[0]
            # Тип дня берётся тем же запросом: ответ на «Что снимали?» может прийти из окна
            # в другом потоке ровно в этот момент и не должен потеряться между чтением и записью.
            cur = self.db.execute(
                "INSERT INTO videos (day, part, kind, status) "
                "VALUES (?, ?, COALESCE(?, (SELECT kind FROM days WHERE day = ?)), 'pending')",
                (day.isoformat(), part, kind, day.isoformat()),
            )
            video_id = cur.lastrowid
            self.db.executemany(
                "UPDATE clips SET video = ? WHERE name = ? AND day = ?",
                [(video_id, n, day.isoformat()) for n in clip_names],
            )
        return video_id, part

    def update_video(self, video_id: int, **fields) -> None:
        allowed = {"kind", "status", "file", "rec_start", "rec_end", "youtube_id", "privacy", "done_at",
                   "in_playlist", "thumbnail", "hidden"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"Недопустимые поля: {set(fields) - allowed}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.db:
            self.db.execute(f"UPDATE videos SET {cols} WHERE id = ?", (*fields.values(), video_id))

    def video(self, video_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()

    def videos(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self.db.execute("SELECT * FROM videos WHERE status = ? ORDER BY day, part", (status,)).fetchall()
        return self.db.execute("SELECT * FROM videos ORDER BY day, part").fetchall()

    def done_before(self, moment: datetime) -> list[sqlite3.Row]:
        """Переданные в Studio или загруженные не позже moment, чей файл ещё на диске."""
        return self.db.execute(
            "SELECT * FROM videos WHERE status IN ('handed', 'uploaded') AND file IS NOT NULL AND file != '' "
            "AND done_at IS NOT NULL AND done_at <= ? ORDER BY day, part",
            (moment.isoformat(timespec="seconds"),)).fetchall()

    def discard_clips(self, day: date, video_id: int | None = None) -> list[str]:
        """Клипы ролика (или ещё не собранные клипы дня) больше не обрабатывать. Возвращает их имена."""
        where, args = ("video = ?", (video_id,)) if video_id is not None else (
            "day = ? AND status = 'copied' AND video IS NULL", (day.isoformat(),))
        names = [r["name"] for r in self.db.execute(f"SELECT name FROM clips WHERE {where}", args)]
        with self.db:
            self.db.execute(f"UPDATE clips SET status = 'discarded' WHERE {where}", args)
        return names

    def set_failure(self, day: date, reason: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO failures (day, reason, at) VALUES (?, ?, ?) "
                            "ON CONFLICT(day) DO UPDATE SET reason = excluded.reason, at = excluded.at",
                            (day.isoformat(), reason, datetime.now().isoformat(timespec="seconds")))

    def clear_failure(self, day: date) -> None:
        with self.db:
            self.db.execute("DELETE FROM failures WHERE day = ?", (day.isoformat(),))

    def failures(self) -> dict[date, str]:
        rows = self.db.execute("SELECT day, reason FROM failures")
        return {date.fromisoformat(r["day"]): r["reason"] for r in rows}

    def release_interrupted(self) -> list[date]:
        """При запуске: ролики, сборку которых оборвал выход или сбой, снова свободны. Возвращает их дни."""
        rows = self.db.execute("SELECT id, day FROM videos WHERE status = 'pending'").fetchall()
        for r in rows:
            self.release_video(r["id"])
        return sorted({date.fromisoformat(r["day"]) for r in rows})

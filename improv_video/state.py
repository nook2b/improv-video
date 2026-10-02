"""Журнал состояния в SQLite: какие клипы уже взяты и что стало с роликами."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    name   TEXT NOT NULL,
    size   INTEGER NOT NULL,
    day    TEXT NOT NULL,
    status TEXT NOT NULL,          -- copied | skipped (помечен как уже обработанный)
    video  INTEGER REFERENCES videos(id),
    PRIMARY KEY (name, size)
);
CREATE TABLE IF NOT EXISTS videos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day        TEXT NOT NULL,
    part       INTEGER NOT NULL,
    kind       TEXT,               -- training | lesson; NULL, пока не выбран
    status     TEXT NOT NULL,      -- pending | built | uploaded | failed
    file       TEXT,
    rec_start  TEXT,               -- начало и конец съёмки (местное время камеры, ISO)
    rec_end    TEXT,
    youtube_id TEXT,
    privacy    TEXT,               -- что ответил YouTube: unlisted | private | public
    UNIQUE (day, part)
);
"""


class State:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

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

    def unassigned_clips(self, day: date) -> list[str]:
        rows = self.db.execute(
            "SELECT name FROM clips WHERE day = ? AND status = 'copied' AND video IS NULL ORDER BY name",
            (day.isoformat(),),
        )
        return [r["name"] for r in rows]

    def create_video(self, day: date, clip_names: list[str], kind: str | None = None) -> tuple[int, int]:
        """Новый ролик за день с номером части; возвращает (id, part)."""
        with self.db:
            part = self.db.execute(
                "SELECT COALESCE(MAX(part), 0) + 1 FROM videos WHERE day = ?", (day.isoformat(),)
            ).fetchone()[0]
            cur = self.db.execute(
                "INSERT INTO videos (day, part, kind, status) VALUES (?, ?, ?, 'pending')",
                (day.isoformat(), part, kind),
            )
            video_id = cur.lastrowid
            self.db.executemany(
                "UPDATE clips SET video = ? WHERE name = ? AND day = ?",
                [(video_id, n, day.isoformat()) for n in clip_names],
            )
        return video_id, part

    def update_video(self, video_id: int, **fields) -> None:
        allowed = {"kind", "status", "file", "rec_start", "rec_end", "youtube_id", "privacy"}
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

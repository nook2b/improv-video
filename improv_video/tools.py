"""Поиск и запуск внешних утилит (ffmpeg, ffprobe, deep-filter)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


class ToolError(RuntimeError):
    """Внешняя утилита завершилась с ошибкой."""

    def __init__(self, cmd: list[str], stderr: str):
        self.cmd = cmd
        self.stderr = stderr
        tail = stderr.strip().splitlines()[-15:]
        super().__init__(f"{Path(cmd[0]).name} завершился с ошибкой:\n" + "\n".join(tail))


def _bundled_dir() -> Path:
    # В собранном приложении (PyInstaller) утилиты лежат рядом с кодом в папке bin.
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "bin"


def find_tool(name: str) -> str:
    """Путь к утилите: переменная IMPROV_<NAME>, затем вложенная bin/, затем PATH."""
    env = os.environ.get("IMPROV_" + name.upper().replace("-", "_"))
    if env:
        return env
    exe = name + (".exe" if sys.platform == "win32" else "")
    bundled = _bundled_dir() / exe
    if bundled.exists():
        return str(bundled)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"Не найдена утилита {name}")


def run(tool: str, args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Запускает утилиту; при ошибке бросает ToolError с хвостом stderr."""
    cmd = [find_tool(tool), *args]
    if tool in ("ffmpeg", "ffprobe"):
        cmd[1:1] = ["-hide_banner", "-nostdin"] if tool == "ffmpeg" else ["-hide_banner"]
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise ToolError(cmd, proc.stderr)
    return proc


def ffmpeg(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return run("ffmpeg", ["-y", *args], cwd=cwd)


def concat_list(paths: list[Path], list_file: Path) -> Path:
    """Файл для concat-демультиплексора ffmpeg."""
    lines = []
    for p in paths:
        quoted = str(Path(p).resolve()).replace("'", "'\\''")
        lines.append(f"file '{quoted}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list_file

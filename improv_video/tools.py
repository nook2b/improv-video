"""Поиск и запуск внешних утилит (ffmpeg, ffprobe, deep-filter)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable


class Cancelled(RuntimeError):
    """Обработку остановили из меню."""

    def __init__(self):
        super().__init__("Обработка остановлена")


class ToolError(RuntimeError):
    """Внешняя утилита завершилась с ошибкой."""

    def __init__(self, cmd: list[str], stderr: str):
        self.cmd = cmd
        self.stderr = stderr
        tail = stderr.strip().splitlines()[-15:]
        super().__init__(f"{Path(cmd[0]).name} завершился с ошибкой:\n" + "\n".join(tail))


def _base_dir() -> Path:
    # В собранном приложении (PyInstaller) — папка с данными приложения, иначе корень репозитория.
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def resources_dir() -> Path:
    return _base_dir() / "resources"


def _bundled_dirs() -> list[Path]:
    dirs = [_base_dir() / "bin"]
    if getattr(sys, "frozen", False):
        # improv-video.app/Contents/MacOS/improv-video → Contents/Resources/bin
        dirs.append(Path(sys.executable).resolve().parent.parent / "Resources" / "bin")
    return dirs


def find_tool(name: str) -> str:
    """Путь к утилите: переменная IMPROV_<NAME>, затем вложенная bin/, затем PATH."""
    env = os.environ.get("IMPROV_" + name.upper().replace("-", "_"))
    if env:
        return env
    exe = name + (".exe" if sys.platform == "win32" else "")
    for d in _bundled_dirs():
        if (d / exe).exists():
            return str(d / exe)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"Не найдена утилита {name}")


# Остановка: один флаг на всё приложение (задачи идут по одной) и список запущенных процессов.
_cancel = threading.Event()
_procs: set[subprocess.Popen] = set()
_procs_lock = threading.Lock()


def cancel_all() -> None:
    """Останавливает текущую обработку: запущенные утилиты завершаются, новые не стартуют."""
    _cancel.set()
    with _procs_lock:
        for proc in list(_procs):
            try:
                proc.kill()
            except OSError:
                pass


def reset_cancel() -> None:
    _cancel.clear()


def check_cancel() -> None:
    if _cancel.is_set():
        raise Cancelled()


def _read_progress(stream, progress: Callable[[float], None],
                   stats: Callable[[dict], None] | None = None) -> None:
    """Строки ffmpeg -progress: out_time_us=… → секунды обработанного; stats — весь блок (frame, fps, speed…)."""
    block: dict[str, str] = {}
    for line in stream:
        key, _, value = line.strip().partition("=")
        block[key] = value
        if key in ("out_time_us", "out_time_ms") and value.lstrip("-").isdigit():
            progress(max(0.0, int(value) / 1_000_000))
        if key == "progress":
            if stats:
                stats(block)
            block = {}


def run(tool: str, args: list[str], *, cwd: Path | None = None,
        progress: Callable[[float], None] | None = None,
        stats: Callable[[dict], None] | None = None) -> subprocess.CompletedProcess:
    """Запускает утилиту; при ошибке бросает ToolError с хвостом stderr, при остановке — Cancelled.

    progress (только ffmpeg) получает, до какой секунды дошла обработка.
    """
    check_cancel()
    cmd = [find_tool(tool), *args]
    if tool in ("ffmpeg", "ffprobe"):
        cmd[1:1] = ["-hide_banner", "-nostdin"] if tool == "ffmpeg" else ["-hide_banner"]
    if progress and tool == "ffmpeg":
        cmd[1:1] = ["-progress", "pipe:1", "-nostats"]
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=err, text=True,
                                encoding="utf-8", errors="replace")
        with _procs_lock:
            _procs.add(proc)
        try:
            if progress and tool == "ffmpeg":
                _read_progress(proc.stdout, progress, stats)
                stdout = ""
            else:
                stdout = proc.stdout.read()
            proc.wait()
        finally:
            with _procs_lock:
                _procs.discard(proc)
            proc.stdout.close()
        err.seek(0)
        stderr = err.read()
    if _cancel.is_set():
        raise Cancelled()
    if proc.returncode != 0:
        raise ToolError(cmd, stderr)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def ffmpeg(args: list[str], *, cwd: Path | None = None,
           progress: Callable[[float], None] | None = None,
           stats: Callable[[dict], None] | None = None) -> subprocess.CompletedProcess:
    return run("ffmpeg", ["-y", *args], cwd=cwd, progress=progress, stats=stats)


def concat_list(paths: list[Path], list_file: Path) -> Path:
    """Файл для concat-демультиплексора ffmpeg."""
    lines = []
    for p in paths:
        quoted = str(Path(p).resolve()).replace("'", "'\\''")
        lines.append(f"file '{quoted}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list_file

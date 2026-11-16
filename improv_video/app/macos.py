"""Системные функции macOS через стандартные утилиты: osascript, pbcopy, open, caffeinate."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.nook2b.improv-video.plist"


def _q(text: str) -> str:
    """Строка для AppleScript в кавычках."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def osascript(script: str, timeout: float | None = None) -> str | None:
    """Выполняет AppleScript; None, если пользователь нажал «Отмена» или закрыл окно."""
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def trash(path: Path) -> None:
    """В Корзину (можно вернуть), а не удаление насовсем."""
    from Foundation import NSURL, NSFileManager

    ok, _, error = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(str(path)), None, None)
    if not ok:
        raise OSError(f"Не удалось переместить в Корзину {path}: {error}")


def notify(title: str, text: str) -> None:
    osascript(f"display notification {_q(text)} with title {_q(title)}")


def dialog(text: str, buttons: list[str], default: str | None = None, title: str = "improv-video",
           giving_up_after: int | None = None) -> str | None:
    """Окно с кнопками; возвращает текст нажатой кнопки или None (отмена / истекло время)."""
    btns = "{" + ", ".join(_q(b) for b in buttons) + "}"
    script = f"display dialog {_q(text)} with title {_q(title)} buttons {btns}"
    if default:
        script += f" default button {_q(default)}"
    if giving_up_after:
        script += f" giving up after {giving_up_after}"
    out = osascript(script)
    if not out or "gave up:true" in out:
        return None
    # «button returned:Тренировка, gave up:false»
    value = out.split("button returned:", 1)[-1]
    return value.split(", gave up:", 1)[0]


def choose_file(prompt: str, extensions: list[str] | None = None) -> Path | None:
    """Окно выбора файла. Фильтр по типу не ставим: у .cube нет системного типа, и macOS
    делает такие файлы неактивными. Расширение проверяем сами и переспрашиваем при ошибке."""
    while True:
        out = osascript(f"POSIX path of (choose file with prompt {_q(prompt)})")
        if not out:
            return None
        path = Path(out)
        if not extensions or path.suffix.lower().lstrip(".") in {e.lower() for e in extensions}:
            return path
        need = ", ".join("." + e for e in extensions)
        osascript(f"display alert {_q('Нужен файл ' + need)} message {_q(path.name + ' не подходит')}")


def choose_folder(prompt: str) -> Path | None:
    out = osascript(f"POSIX path of (choose folder with prompt {_q(prompt)})")
    return Path(out) if out else None


def copy_to_clipboard(text: str) -> None:
    env = dict(os.environ, LANG="en_US.UTF-8")
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), env=env, check=False)


def reveal(path: Path) -> None:
    subprocess.run(["open", "-R", str(path)], check=False)


def open_path(target: str | Path) -> None:
    subprocess.run(["open", str(target)], check=False)


@contextmanager
def keep_awake():
    """Пока идёт работа: Mac не засыпает (крышка должна быть открыта) и не включает App Nap.

    App Nap притормаживает фоновые приложения, когда за компьютером никого нет: по журналу
    ночью куски кодировались в 3 раза медленнее, чем днём. Активность «по просьбе
    пользователя» отключает App Nap и для запущенных ffmpeg.
    """
    proc, info, token = None, None, None
    try:
        proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    except FileNotFoundError:
        pass
    try:
        from Foundation import NSActivityIdleSystemSleepDisabled, NSActivityUserInitiated, NSProcessInfo

        info = NSProcessInfo.processInfo()
        token = info.beginActivityWithOptions_reason_(NSActivityUserInitiated | NSActivityIdleSystemSleepDisabled,
                                                      "Сборка ролика improv-video")
    except Exception:  # noqa: BLE001 — не Mac или нет PyObjC: остаётся caffeinate
        token = None
    try:
        yield
    finally:
        if token is not None:
            info.endActivity_(token)
        if proc:
            proc.terminate()


def app_bundle() -> Path | None:
    """Путь к improv-video.app, если запущено собранное приложение."""
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None


def launch_at_login_enabled() -> bool:
    return LAUNCH_AGENT.exists()


def set_launch_at_login(enabled: bool) -> None:
    if not enabled:
        LAUNCH_AGENT.unlink(missing_ok=True)
        return
    bundle = app_bundle()
    if bundle is None:
        return
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": "com.nook2b.improv-video",
        "ProgramArguments": ["/usr/bin/open", "-a", str(bundle)],
        "RunAtLoad": True,
    }
    LAUNCH_AGENT.write_bytes(plistlib.dumps(plist))


# ---------- окна по дизайну (improv_video.app.dialogs) ----------

def ask_kind(day, meta: str = "", note: str = "", default: str = "training", timeout: float | None = None,
             playlists=None):
    from . import dialogs

    return dialogs.ask_kind(dialogs.KindInfo(f"{day:%d.%m.%Y}", meta, note, default, playlists), timeout)


def ask_thumbnail(videos: list):
    from . import dialogs

    return dialogs.ask_thumbnail(dialogs.ThumbInfo(videos))


def ask_first_run(clips: int, days: int):
    from . import dialogs

    return dialogs.ask_first_run(clips, days)


def show_ready(name: str, desc: str, file: Path) -> None:
    from . import dialogs

    try:
        size = f"{Path(file).stat().st_size / 1024**3:.1f} ГБ"
    except OSError:
        size = ""
    dialogs.show_ready(dialogs.ReadyInfo(name, desc, Path(file), size),
                       {"copy": copy_to_clipboard, "reveal": reveal})

"""Автообновление: новая версия берётся из релизов GitHub и ставится, когда приложение ничего не делает.

Релизы публичные, поэтому скачиваются без входа. Файл, скачанный самим приложением (а не браузером),
не получает отметку карантина — macOS не спрашивает «открыть программу из интернета?».
Замену приложения делает маленький скрипт уже после выхода: работающее приложение подгружает
свои модули из папки .app, и подменять её на ходу нельзя.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import shlex
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO = "nook2b/improv-video"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = "improv-video-mac.zip"
CHECK_SECONDS = 6 * 3600


@dataclass
class Release:
    version: str
    zip_url: str
    sha256_url: str | None


def parse_version(text: str) -> tuple[int, ...]:
    """«v0.1.14» → (0, 1, 14); непонятное → ()."""
    parts = text.strip().lstrip("v").split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return ()


def is_newer(candidate: str, current: str) -> bool:
    new, cur = parse_version(candidate), parse_version(current)
    return bool(new) and bool(cur) and new > cur


def _ssl_context():
    """Сертификаты из certifi: у Python внутри приложения своих нет — без этого «CERTIFICATE_VERIFY_FAILED»."""
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _open(url: str, timeout: float, accept: str | None = None):
    headers = {"User-Agent": "improv-video", **({"Accept": accept} if accept else {})}
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout,
                                  context=_ssl_context() if url.startswith("https:") else None)


def _get(url: str, timeout: float = 30) -> bytes:
    with _open(url, timeout, "application/vnd.github+json") as r:
        return r.read()


def latest(fetch=_get) -> Release | None:
    """Последний релиз с архивом для Mac или None."""
    data = json.loads(fetch(LATEST_URL))
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
    if ASSET not in assets:
        return None
    return Release(data["tag_name"].lstrip("v"), assets[ASSET], assets.get(ASSET + ".sha256"))


def current_bundle() -> Path | None:
    """Папка improv-video.app, из которой запущено приложение; None — запуск не из сборки."""
    if not getattr(sys, "frozen", False) or sys.platform != "darwin":
        return None
    app = Path(sys.executable).resolve().parents[2]  # .app/Contents/MacOS/improv-video
    return app if app.suffix == ".app" else None


def _download(url: str, dest: Path) -> None:
    with _open(url, 60) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)


def _unzip(archive: Path, dest: Path) -> None:
    if shutil.which("ditto"):  # сохраняет ссылки и права внутри .app
        subprocess.run(["ditto", "-x", "-k", str(archive), str(dest)], check=True, capture_output=True)
    else:
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)


def bundle_version(app: Path) -> str:
    with open(app / "Contents" / "Info.plist", "rb") as f:
        return plistlib.load(f).get("CFBundleShortVersionString", "")


def prepare(release: Release, workdir: Path) -> Path:
    """Скачивает и проверяет новую версию; возвращает путь к распакованному improv-video.app."""
    workdir.mkdir(parents=True, exist_ok=True)
    folder = workdir / release.version
    app = folder / "improv-video.app"
    if app.exists() and bundle_version(app) == release.version:
        return app  # уже скачана, ждёт простоя
    for old in workdir.iterdir():
        if old.is_dir() and old.name != "previous.app":
            shutil.rmtree(old, ignore_errors=True)
    folder.mkdir()
    archive = folder / ASSET
    _download(release.zip_url, archive)
    if release.sha256_url:
        want = _get(release.sha256_url).decode().split()[0].lower()
        got = hashlib.sha256(archive.read_bytes()).hexdigest()
        if got != want:
            raise ValueError(f"Обновление {release.version} повреждено при скачивании")
    _unzip(archive, folder)
    archive.unlink()
    if not app.exists() or bundle_version(app) != release.version:
        raise ValueError(f"В архиве обновления {release.version} нет приложения нужной версии")
    if shutil.which("xattr"):
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(app)], capture_output=True)
    if shutil.which("codesign"):
        r = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, text=True)
        if r.returncode != 0:
            raise ValueError(f"Подпись обновления {release.version} не сходится: {r.stderr.strip()[:200]}")
    return app


def swap_script(new_app: Path, bundle: Path, backup: Path, pid: int) -> str:
    """Скрипт замены: ждёт выхода приложения, ставит новое (старое — в backup) и запускает.
    Если новое не встало — возвращает старое."""
    n, b, k = shlex.quote(str(new_app)), shlex.quote(str(bundle)), shlex.quote(str(backup))
    return (
        f"while kill -0 {pid} 2>/dev/null; do sleep 0.5; done\n"
        f"rm -rf {k}\n"
        f"if mv {b} {k}; then\n"
        f"  if mv {n} {b}; then open {b}; exit 0; fi\n"
        f"  mv {k} {b}\n"
        f"fi\n"
        f"open {b}\n"
    )


def launch_swap(new_app: Path, bundle: Path, workdir: Path, pid: int) -> None:
    script = workdir / "swap.sh"
    script.write_text(swap_script(new_app, bundle, workdir / "previous.app", pid), encoding="utf-8")
    subprocess.Popen(["/bin/sh", str(script)], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

"""Логика приложения: флешка → импорт → сборка → загрузка (сама или через YouTube Studio).

Все операции с журналом идут в одном рабочем потоке через очередь задач; диалоги,
которые ждут ответа человека, открываются в отдельных потоках и тоже ставят задачи в очередь.
"""

from __future__ import annotations

import logging
import queue
import shutil
import threading
import time
from datetime import date, datetime
from functools import partial
from pathlib import Path

from .. import youtube
from ..naming import KINDS
from ..pipeline import (NotEnoughSpace, build_pending, import_card, mark_existing_as_done,
                        new_clips_on_card, source_folders, upload_ready, video_metadata)
from ..state import State
from .config import SUPPORT_DIR, AppConfig

log = logging.getLogger("improv-video")

KIND_BY_LABEL = {label: kind for kind, label in KINDS.items()}
STUDIO_URL = "https://studio.youtube.com"
RETRY_SECONDS = 30 * 60
KIND_WAIT_SECONDS = 12 * 3600


class Controller:
    def __init__(self, config: AppConfig, ui, *, config_path: Path | None = None,
                 token_store=None, volumes_dir: Path = Path("/Volumes")):
        self.config = config
        self.config_path = config_path
        self.ui = ui
        self.tokens = token_store or youtube.KeyringStore()
        self.volumes_dir = volumes_dir
        self.status = "Жду флешку"
        self.manual = []  # последние ролики для ручной загрузки: (название, описание, файл)
        self._queue: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._busy = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ---------- запуск и очередь ----------

    def start(self, watch: bool = True) -> None:
        self._spawn(self._worker, daemon=True, track=False)
        if watch:
            self._spawn(self._watch_volumes, daemon=True, track=False)
            self._spawn(self._retry_timer, daemon=True, track=False)

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)

    def submit(self, task: str, *args) -> None:
        with self._lock:
            self._busy += 1
        self._queue.put((task, args))

    def wait_idle(self, timeout: float = 120) -> None:
        """Для тестов: ждёт, пока очередь и диалоговые потоки закончатся."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                threads = [t for t in self._threads if t.is_alive()]
                idle = self._busy == 0 and not threads
            if idle:
                return
            time.sleep(0.05)
        raise TimeoutError("Контроллер не освободился")

    def _spawn(self, target, *args, daemon=True, track=True) -> None:
        t = threading.Thread(target=target, args=args, daemon=daemon)
        if track:
            with self._lock:
                self._threads.append(t)
        t.start()

    def _worker(self) -> None:
        self.state = State(Path(self.config.archive).expanduser() / "state.sqlite")
        while True:
            item = self._queue.get()
            if item is None:
                return
            task, args = item
            try:
                getattr(self, "_do_" + task)(*args)
            except NotEnoughSpace as e:
                self._say("Нет места на диске", str(e))
            except youtube.NeedsLogin as e:
                self._say("YouTube", f"{e}: меню improv-video → «Войти в YouTube»")
            except youtube.QuotaExceeded as e:
                self._say("YouTube", str(e))
            except Exception as e:  # noqa: BLE001 — показываем человеку любую ошибку
                log.exception("Ошибка в задаче %s", task)
                self._say("Ошибка", str(e)[:200])
            finally:
                self.status = "Жду флешку"
                with self._lock:
                    self._busy -= 1

    def _say(self, title: str, text: str) -> None:
        log.info("%s: %s", title, text)
        self.ui.notify(title, text)

    def _progress(self, text: str) -> None:
        log.info(text)
        self.status = text
        if text.startswith(("Найдено", "Можно извлечь")):
            self.ui.notify("improv-video", text)

    def _save_config(self) -> None:
        self.config.save(self.config_path)

    # ---------- флешка ----------

    def _watch_volumes(self) -> None:
        seen: set[str] = set()
        while not self._stop.is_set():
            try:
                current = {p.name for p in self.volumes_dir.iterdir()} if self.volumes_dir.exists() else set()
            except OSError:
                current = set()
            for name in sorted(current - seen):
                path = self.volumes_dir / name
                try:
                    if source_folders(path) and (path / "DCIM").is_dir():
                        self.submit("source", path)
                except OSError:
                    pass
            seen = current
            self._stop.wait(5)

    def _retry_timer(self) -> None:
        while not self._stop.wait(RETRY_SECONDS):
            self.submit("deliver")

    def _do_source(self, path: Path) -> None:
        """Флешка или папка с клипами."""
        settings = self.config.settings()
        clips = new_clips_on_card(path, self.state)
        if not clips:
            return
        if self.state.is_empty():
            days = len({c.start.date() for c in clips})
            answer = self.ui.dialog(
                f"На карте {len(clips)} клипов примерно за {days} дн. Это первый запуск: "
                "обработать их все или считать уже обработанными?",
                ["Считать обработанными", "Обработать все"], default="Обработать все")
            if answer != "Обработать все":
                n = mark_existing_as_done(path, self.state, settings)
                self._say("improv-video", f"{n} клипов помечены как обработанные. Новые съёмки пойдут автоматически.")
                return
        if not self.config.lut:
            lut = self.ui.choose_file("Выберите LUT для I-Log (.cube)", ["cube"])
            if lut:
                self.config.lut = str(lut)
                self._save_config()
                settings = self.config.settings()
        with self.ui.keep_awake():
            days = import_card(path, self.state, settings, self._progress)
            for day in days:
                self._spawn(self._ask_kind, day)
            for day in days:
                build_pending(day, self.state, settings, None, self._progress)
            self._do_deliver()

    def _ask_kind(self, day: date) -> None:
        labels = list(KINDS.values())
        answer = self.ui.dialog(f"Что снимали {day:%d.%m.%Y}?", labels,
                                default=KINDS.get(self.config.last_kind), giving_up_after=KIND_WAIT_SECONDS)
        if answer in KIND_BY_LABEL:
            self.submit("kind", day, KIND_BY_LABEL[answer])

    def _do_kind(self, day: date, kind: str) -> None:
        for row in self.state.videos():
            if row["day"] == day.isoformat() and row["status"] in ("pending", "built"):
                self.state.update_video(row["id"], kind=kind)
        self.config.last_kind = kind
        self._save_config()
        self._do_deliver()

    def _do_forget_skipped(self) -> None:
        n = self.state.forget_skipped()
        self._say("improv-video", f"Отметка снята с {n} клипов. Вставьте флешку заново — их можно будет обработать."
                  if n else "Клипов с отметкой «обработано» нет.")

    # ---------- выдача ролика ----------

    def _do_deliver(self) -> None:
        ready = [r for r in self.state.videos("built") if r["kind"]]
        if not ready:
            return
        if self.config.upload_mode == "api":
            creds = youtube.credentials(self.tokens)
            upload_ready(self.state, self.config.settings(),
                         partial(youtube.upload, creds=creds, privacy="unlisted"), self._progress_upload)
            return
        for row in ready:
            name, desc, _ = video_metadata(row)
            self.state.update_video(row["id"], status="manual")
            self.manual = ([(name, desc, Path(row["file"]))] + self.manual)[:10]
            self._spawn(self._hand_off, name, desc, Path(row["file"]))

    def _progress_upload(self, text: str) -> None:
        self._progress(text)
        if "загружено" in text:
            self.ui.notify("YouTube", text)

    def hand_off(self, name: str, desc: str, file: Path) -> None:
        """Ручная загрузка (из меню): можно повторить для любого из последних роликов."""
        self._spawn(self._hand_off, name, desc, file)

    def _hand_off(self, name: str, desc: str, file: Path) -> None:
        self.ui.copy_to_clipboard(name)
        self.ui.reveal(file)
        self.ui.open_path(STUDIO_URL)
        text = (f"«{name}» готово.\n\n"
                "1. В YouTube Studio нажмите «Создать → Добавить видео» и перетащите выделенный файл.\n"
                "2. Название уже скопировано — вставьте его (⌘V).\n"
                f"3. Описание: {desc}\n"
                "4. Видимость — «Доступ по ссылке».")
        while True:
            answer = self.ui.dialog(text, ["Скопировать название", "Скопировать описание", "Готово"],
                                    default="Готово")
            if answer == "Скопировать название":
                self.ui.copy_to_clipboard(name)
            elif answer == "Скопировать описание":
                self.ui.copy_to_clipboard(desc)
            else:
                return

    # ---------- YouTube ----------

    def _client_secrets(self) -> Path | None:
        path = Path(self.config.client_secrets) if self.config.client_secrets else None
        if path and path.exists():
            return path
        chosen = self.ui.choose_file("Выберите JSON-ключ OAuth-клиента (client_secret_….json)", ["json"])
        if not chosen:
            return None
        SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
        target = SUPPORT_DIR / "client_secret.json"
        shutil.copyfile(chosen, target)
        self.config.client_secrets = str(target)
        self._save_config()
        return target

    def _do_login(self) -> None:
        secrets = self._client_secrets()
        if not secrets:
            return
        self.status = "Вход в YouTube: продолжите в браузере"
        youtube.login(secrets, self.tokens)
        self._say("YouTube", "Вход сохранён")

    def _do_test_upload(self, file: Path) -> None:
        creds = youtube.credentials(self.tokens)
        self.status = "Тестовая загрузка…"
        result = youtube.upload(file, "Тест improv-video", "Проверка загрузки из improv-video",
                                datetime.now(), creds=creds, privacy="private",
                                progress=lambda p: setattr(self, "status", f"Тестовая загрузка: {p:.0%}"))
        self._say("YouTube", f"Загружено ({result.privacy}): {result.url}")
        self.ui.open_path(result.url)

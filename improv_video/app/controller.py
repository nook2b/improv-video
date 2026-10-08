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
from contextlib import contextmanager
from datetime import date, datetime
from functools import partial
from pathlib import Path

from .. import tools, youtube
from ..naming import KINDS, title
from ..pipeline import (NotEnoughSpace, VideoItem, build_pending, trash_done_videos, import_card, mark_existing_as_done,
                        new_clips_on_card, recent_videos, source_folders, upload_ready, video_metadata)
from ..progress import DayProgress, minutes


def shooting_meta(start, end, seconds: float, clips: int) -> str:
    """«18:05–20:40 · 1.5 ч · 12 клипов» для окна «Что снимали?»."""
    length = f"{seconds / 3600:.1f} ч" if seconds >= 3600 else minutes(seconds)
    word = "клип" if clips % 10 == 1 and clips % 100 != 11 else (
        "клипа" if 2 <= clips % 10 <= 4 and not 12 <= clips % 100 <= 14 else "клипов")
    return f"{start:%H:%M}–{end:%H:%M} · {length} · {clips} {word}"
from ..state import State
from .config import SUPPORT_DIR, AppConfig

log = logging.getLogger("improv-video")

STUDIO_URL = "https://studio.youtube.com"
IDLE = "Жду флешку"
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
        self._status = IDLE
        self.progress: DayProgress | None = None  # сборка дня — для блока прогресса в меню
        self.uploading: dict[int, float] = {}  # id ролика → доля загрузки
        self.note: tuple[str, str] | None = None  # «Новых клипов нет» и т. п. до извлечения флешки
        self.note_volume: Path | None = None
        self.signed_in = False
        self._local = threading.local()
        self.manual = []  # последние ролики для ручной загрузки: (название, описание, файл)
        self._queue: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._busy = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._delivering = False  # идёт выдача роликов (в своём потоке, не в очереди со сборкой)
        self._deliver_again = False

    # ---------- запуск и очередь ----------

    # ---------- состояние для меню (читается из основного потока) ----------

    @property
    def status(self) -> str:
        if self.progress:
            return self.progress.summary()
        if self.uploading:
            vid, fraction = next(iter(self.uploading.items()))
            return f"Загрузка на YouTube · {fraction:.0%}"
        if self._status == IDLE and self.note:
            return self.note[0]
        return self._status

    @status.setter
    def status(self, text: str) -> None:
        self._status = text

    @property
    def delivering(self) -> bool:
        return self._delivering

    @property
    def busy(self) -> bool:
        return self._status != IDLE or self.progress is not None or bool(self.uploading)

    def videos(self) -> list[VideoItem]:
        """Список «Ролики»: своё подключение к журналу в потоке меню."""
        state = getattr(self._local, "state", None)
        if state is None:
            state = self._local.state = State(Path(self.config.archive).expanduser() / "state.sqlite")
        building = None
        if self.progress and self.progress.day:
            building = (self.progress.day, self.progress.overall())
        return recent_videos(state, upload_mode=self.config.upload_mode, uploading=dict(self.uploading),
                             building=building)

    def stop_processing(self) -> None:
        """«Остановить обработку»: несобранный день соберётся при следующей вставке флешки."""
        if self.busy:
            tools.cancel_all()

    # ---------- запуск и очередь ----------

    def start(self, watch: bool = True) -> None:
        try:
            self.signed_in = self.tokens.load() is not None
        except Exception:  # noqa: BLE001 — связка ключей недоступна: считаем, что не вошли
            self.signed_in = False
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
        archive = Path(self.config.archive).expanduser()
        self.state = State(archive / "state.sqlite")
        self._recover(archive)
        while True:
            item = self._queue.get()
            if item is None:
                return
            task, args = item
            tools.reset_cancel()
            try:
                getattr(self, "_do_" + task)(*args)
            except Exception as e:  # noqa: BLE001
                self._report(task, e)
            finally:
                self.status = IDLE
                self.progress = None
                with self._lock:
                    self._busy -= 1

    def _report(self, task: str, e: Exception) -> None:
        if isinstance(e, tools.Cancelled):
            self._say("improv-video", "Обработка остановлена. День соберётся при следующей вставке флешки.")
        elif isinstance(e, NotEnoughSpace):
            self._say("Нет места на диске", str(e))
        elif isinstance(e, youtube.NeedsLogin):
            self.signed_in = False
            self._say("YouTube", f"{e}: меню improv-video → «Войти в YouTube»")
        elif isinstance(e, youtube.QuotaExceeded):
            self._say("YouTube", str(e))
        else:  # показываем человеку любую ошибку
            log.error("Ошибка в задаче %s", task, exc_info=e)
            self._say("Ошибка", str(e)[:200])

    def _recover(self, archive: Path) -> None:
        """Прошлый запуск оборвался посреди сборки: день соберётся заново, временные куски — в корзину."""
        days = self.state.release_interrupted()
        for tmp in archive.glob("*/improv-*"):
            if tmp.is_dir():
                shutil.rmtree(tmp, ignore_errors=True)
        if days:
            log.info("Прерванная сборка: %s соберётся при следующей вставке флешки",
                     ", ".join(f"{d:%d.%m.%Y}" for d in days))

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
            if self.note_volume and self.note_volume.name not in current:
                self.note, self.note_volume = None, None  # флешку извлекли — подсказка больше не нужна
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
        self.cleanup()
        while not self._stop.wait(RETRY_SECONDS):
            self.submit("deliver")
            self.cleanup()

    def cleanup(self) -> None:
        """Автоудаление: файлы переданных и загруженных роликов — в Корзину через N дней."""
        try:
            with self._own_state() as state:
                moved = trash_done_videos(state, self.config.delete_after_days, self.ui.trash)
        except Exception:  # noqa: BLE001 — не удалось сейчас, попробуем через полчаса
            log.exception("Автоудаление роликов")
            return
        for name in moved:
            log.info("В Корзину (прошло %s дн. после загрузки): %s", self.config.delete_after_days, name)
        if moved:
            self.ui.notify("improv-video", f"В Корзину: {', '.join(moved)}" if len(moved) < 3
                           else f"В Корзину: {len(moved)} роликов, уже загруженных на YouTube")

    def _do_source(self, path: Path) -> None:
        """Флешка или папка с клипами."""
        settings = self.config.settings()
        self.note, self.note_volume = None, None
        clips = new_clips_on_card(path, self.state)
        if not clips and not self.state.days_with_unassigned():
            self.note = ("Новых клипов нет", "Всё на карте уже обработано. Можно извлечь флешку")
            self.note_volume = path if path.parent == self.volumes_dir else None
            self._say("improv-video", "Новых клипов нет")
            return
        if self.state.is_empty():
            days = len({c.start.date() for c in clips})
            answer = self.ui.ask_first_run(len(clips), days)
            if answer is None:
                return  # окно закрыли — спросим при следующей вставке
            if answer != "all":
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
            summary: dict = {}
            days = import_card(path, self.state, settings, self._progress,
                               on_copy=lambda f: setattr(self, "status", f"Копирование клипов · {f:.0%}"),
                               summary=summary)
            for n, day in enumerate(sorted(days), 1):
                note = f"день {n} из {len(days)}" if len(days) > 1 else ""
                self._spawn(self._ask_kind, day, shooting_meta(*summary[day]) if day in summary else "", note)
            # Новые дни и те, что раньше не собрались (флешку вынули, сбой)
            todo = sorted(set(days) | set(self.state.days_with_unassigned()))
            for n, day in enumerate(todo, 1):
                kind = self.state.day_kind(day)
                self.progress = DayProgress(title(kind, day) if kind else f"{day:%d.%m.%Y}", n, len(todo),
                                            reading_card=not settings.copy_clips, day=day)
                try:
                    vid = build_pending(day, self.state, settings, None, self._progress, source=path,
                                        progress=self.progress)
                    if vid is not None:
                        row = self.state.video(vid)
                        name = title(row["kind"], day, row["part"]) if row["kind"] else f"Ролик {day:%d.%m.%Y}"
                        self._say("improv-video", f"{name} готов за {minutes(self.progress.elapsed())}")
                        self.deliver()  # первый ролик можно смотреть, пока собираются остальные
                except tools.Cancelled:
                    raise
                except NotEnoughSpace as e:  # другие дни могут поместиться
                    self._say("Нет места на диске", f"{e}. Освободите место и вставьте флешку заново.")
                except Exception as e:  # noqa: BLE001 — один день не должен останавливать остальные
                    log.exception("Не собрался день %s", day)
                    self._say("Не удалось собрать", f"{day:%d.%m.%Y}: {str(e)[:150]}. Повторю при следующей вставке флешки.")
            self.progress = None
            if not settings.copy_clips:
                self._say("improv-video", "Можно извлечь флешку")

    def _ask_kind(self, day: date, meta: str = "", note: str = "") -> None:
        kind = self.ui.ask_kind(day, meta, note, self.config.last_kind, KIND_WAIT_SECONDS)
        if kind in KINDS:
            self.set_kind(day, kind)

    def set_kind(self, day: date, kind: str) -> None:
        """Ответ на «Что снимали?» записывается сразу, а не в очередь: там может часами идти сборка
        других дней. Выдача ролика (окно «Ролик готов» или загрузка) — в очередь, когда дойдёт."""
        with self._own_state() as state:
            state.set_day_kind(day, kind)
        self.config.last_kind = kind
        self._save_config()
        progress = self.progress
        if progress is not None and progress.day == day:
            progress.label = title(kind, day)
        self.deliver()

    def _do_forget_skipped(self) -> None:
        n = self.state.forget_skipped()
        self._say("improv-video", f"Отметка снята с {n} клипов. Вставьте флешку заново — их можно будет обработать."
                  if n else "Клипов с отметкой «обработано» нет.")

    # ---------- выдача ролика ----------

    def _do_deliver(self) -> None:
        self.deliver()

    def deliver(self) -> None:
        """Выдать готовые ролики (окно «Ролик готов» или загрузка) сразу, в своём потоке:
        сборка остальных дней с флешки при этом идёт дальше. Одна выдача за раз — без двойной загрузки."""
        with self._lock:
            if self._delivering:
                self._deliver_again = True
                return
            self._delivering = True
        self._spawn(self._deliver_loop)

    def _deliver_loop(self) -> None:
        while True:
            try:
                with self._own_state() as state:
                    self._deliver_ready(state)
            except Exception as e:  # noqa: BLE001
                self._report("deliver", e)
            with self._lock:
                if not self._deliver_again:
                    self._delivering = False
                    return
                self._deliver_again = False

    def _deliver_ready(self, state: State) -> None:
        ready = [r for r in state.videos("built") if r["kind"]]
        if not ready:
            return
        if self.config.upload_mode == "api":
            creds = youtube.credentials(self.tokens)
            try:
                upload_ready(state, self.config.settings(),
                             partial(youtube.upload, creds=creds, privacy="unlisted"), self._progress_upload,
                             on_progress=self.uploading.__setitem__)
            finally:
                self.uploading.clear()
            return
        for row in ready:
            name, desc, _ = video_metadata(row)
            state.update_video(row["id"], status="manual")
            self.manual = ([(name, desc, Path(row["file"]))] + self.manual)[:10]
            self._spawn(self._hand_off, name, desc, Path(row["file"]), row["id"])

    def _progress_upload(self, text: str) -> None:
        self._progress(text)
        if "загружено" in text:
            self.ui.notify("YouTube", text)

    @contextmanager
    def _own_state(self):
        """Своё подключение к журналу для действий из меню и окон: не ждать очередь со сборкой."""
        state = State(Path(self.config.archive).expanduser() / "state.sqlite")
        try:
            yield state
        finally:
            state.close()

    def hand_off_video(self, video_id: int) -> None:
        """Строка «Загрузить вручную» в списке «Ролики» — сразу, даже во время сборки."""
        with self._own_state() as state:
            row = state.video(video_id)
            if not row or not row["file"]:
                return
            name, desc, _ = video_metadata(row)
            state.update_video(video_id, status="manual")
        self._spawn(self._hand_off, name, desc, Path(row["file"]), video_id)

    def _mark_handed(self, video_id: int) -> None:
        """«Готово» в окне «Ролик готов»: ролик передан в YouTube Studio."""
        with self._own_state() as state:
            row = state.video(video_id)
            if row and row["status"] == "manual":
                state.update_video(video_id, status="handed", done_at=datetime.now().isoformat(timespec="seconds"))

    def ask_kind(self, day: date, meta: str = "") -> None:
        """Строка «Ждёт выбора типа» в списке «Ролики»."""
        self._spawn(self._ask_kind, day, meta)

    def hand_off(self, name: str, desc: str, file: Path) -> None:
        """Ручная загрузка (из меню): можно повторить для любого из последних роликов."""
        self._spawn(self._hand_off, name, desc, file)

    def _hand_off(self, name: str, desc: str, file: Path, video_id: int | None = None) -> None:
        """Окно «Ролик готов»: Finder с файлом, YouTube Studio, название в буфере."""
        self.ui.copy_to_clipboard(name)
        self.ui.reveal(file)
        self.ui.open_path(STUDIO_URL)
        self.ui.show_ready(name, desc, file)
        if video_id is not None:
            self._mark_handed(video_id)

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
        self.signed_in = True
        self._say("YouTube", "Вход сохранён")

    def _do_logout(self) -> None:
        self.tokens.clear()
        self.signed_in = False
        self._say("YouTube", "Вы вышли из YouTube")

    def _do_test_upload(self, file: Path) -> None:
        creds = youtube.credentials(self.tokens)
        self.status = "Тестовая загрузка…"
        result = youtube.upload(file, "Тест improv-video", "Проверка загрузки из improv-video",
                                datetime.now(), creds=creds, privacy="private",
                                progress=lambda p: setattr(self, "status", f"Тестовая загрузка: {p:.0%}"))
        self._say("YouTube", f"Загружено ({result.privacy}): {result.url}")
        self.ui.open_path(result.url)

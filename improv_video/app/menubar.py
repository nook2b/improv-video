"""Меню в строке меню macOS (rumps). Колбэки меню не блокируют интерфейс: работа уходит в очередь."""

from __future__ import annotations

import logging
import threading

import rumps

from .. import __version__
from . import macos
from .config import LOG_FILE, AppConfig
from .controller import Controller

DENOISE = {"off": "Выключено", "weak": "Слабое", "medium": "Среднее (голос)", "strong": "Сильное (DeepFilterNet)"}
QUALITY = {1080: "1080p (меньше места, быстрее)", 2160: "4K"}
MODES = {"manual": "Вручную через YouTube Studio (до аудита)", "api": "Автоматически «по ссылке» (после аудита)"}


def _in_thread(fn):
    def run(*args):
        threading.Thread(target=fn, args=args, daemon=True).start()
    return run


class MenuBarApp(rumps.App):
    def __init__(self):
        super().__init__("improv-video", title="🎬", quit_button=None)
        self.config = AppConfig.load()
        self.controller = Controller(self.config, macos)
        self.status_item = rumps.MenuItem("Жду флешку")
        self.stop_item = rumps.MenuItem("Остановить обработку")
        self.videos_menu = rumps.MenuItem("Ролики")
        self.youtube_item = rumps.MenuItem("Войти в YouTube…", callback=self.toggle_youtube)
        self.denoise_menu = rumps.MenuItem("Шумоподавление")
        for key, label in DENOISE.items():
            self.denoise_menu.add(rumps.MenuItem(label, callback=self._set_denoise(key)))
        self.mode_menu = rumps.MenuItem("Загрузка на YouTube")
        for key, label in MODES.items():
            self.mode_menu.add(rumps.MenuItem(label, callback=self._set_mode(key)))
        self.login_item = rumps.MenuItem("Запускать при входе в систему", callback=self.toggle_login_item)
        self.auto_item = rumps.MenuItem("Автоцвет (как «Авто» в Lumetri)", callback=self.toggle_auto)
        self.copy_item = rumps.MenuItem("Копировать клипы на Mac", callback=self.toggle_copy)
        self.quality_menu = rumps.MenuItem("Качество видео")
        for h, label in QUALITY.items():
            self.quality_menu.add(rumps.MenuItem(label, callback=self._set_quality(h)))
        self.menu = [
            self.status_item,
            self.stop_item,
            None,
            rumps.MenuItem("Обработать папку с клипами…", callback=self.process_folder),
            self.videos_menu,
            rumps.MenuItem("Открыть архив", callback=lambda _: macos.open_path(self.config.archive)),
            rumps.MenuItem("Вернуть клипы, отмеченные как обработанные…", callback=self.forget_skipped),
            None,
            self.youtube_item,
            rumps.MenuItem("Проверить загрузку на YouTube…", callback=self.test_upload),
            self.mode_menu,
            None,
            rumps.MenuItem("Выбрать LUT…", callback=self.choose_lut),
            self.auto_item,
            self.quality_menu,
            self.copy_item,
            self.denoise_menu,
            rumps.MenuItem("Папка архива…", callback=self.choose_archive),
            self.login_item,
            rumps.MenuItem("Открыть журнал", callback=lambda _: macos.open_path(LOG_FILE)),
            None,
            rumps.MenuItem("Выйти", callback=lambda _: rumps.quit_application()),
        ]
        self._refresh_checks()
        self.controller.start()
        rumps.Timer(self._tick, 1).start()

    # ---------- обновление меню (основной поток) ----------

    def _tick(self, _):
        c = self.controller
        progress = c.progress
        self.title = f"⏳ {progress.overall():.0%}" if progress else ("⏳" if c.busy else "🎬")
        self.status_item.title = c.status
        self.stop_item.set_callback(self.stop_processing if c.busy else None)
        self.youtube_item.title = "Выйти из YouTube" if c.signed_in else "Войти в YouTube…"
        try:
            items = c.videos()
        except Exception:  # noqa: BLE001 — журнал занят или недоступен: покажем в следующую секунду
            return
        rows = [(it.key, f"{it.title} — {it.detail}") for it in items]
        if rows != getattr(self, "_video_rows", None):
            self._video_rows = rows
            self.videos_menu.clear()
            if not items:
                self.videos_menu.add(rumps.MenuItem("Роликов пока нет"))
            for it in items:
                self.videos_menu.add(rumps.MenuItem(f"{it.title} — {it.detail}", callback=self._video_action(it)))

    def _video_action(self, it):
        """Клик по строке «Ролики» — действие по статусу."""
        c = self.controller
        if it.status == "kind_needed":
            return lambda _: c.ask_kind(it.day)
        if it.status in ("manual", "handed") and it.video_id:
            return lambda _: c.submit("hand_off_video", it.video_id)
        if it.status == "uploaded" and it.url:
            return lambda _: macos.open_path(it.url)
        if it.status == "failed":
            return lambda _: macos.open_path(LOG_FILE)
        return None

    def stop_processing(self, _):
        self.controller.stop_processing()

    def toggle_youtube(self, _):
        self.controller.submit("logout" if self.controller.signed_in else "login")

    def _refresh_checks(self):
        for key, label in DENOISE.items():
            self.denoise_menu[label].state = int(self.config.denoise == key)
        for key, label in MODES.items():
            self.mode_menu[label].state = int(self.config.upload_mode == key)
        self.login_item.state = int(macos.launch_at_login_enabled())
        self.auto_item.state = int(self.config.auto_brightness)
        self.copy_item.state = int(self.config.copy_clips)
        for h, label in QUALITY.items():
            self.quality_menu[label].state = int(self.config.max_height == h)

    # ---------- действия ----------

    def _set_denoise(self, key):
        def cb(_):
            self.config.denoise = key
            self.config.save()
            self._refresh_checks()
        return cb

    def _set_mode(self, key):
        def cb(_):
            self.config.upload_mode = key
            self.config.save()
            self._refresh_checks()
            if key == "api":
                self.controller.submit("deliver")
        return cb

    def toggle_copy(self, _):
        self.config.copy_clips = not self.config.copy_clips
        self.config.save()
        self._refresh_checks()

    def _set_quality(self, h):
        def cb(_):
            self.config.max_height = h
            self.config.save()
            self._refresh_checks()
        return cb

    def toggle_auto(self, _):
        self.config.auto_brightness = not self.config.auto_brightness
        self.config.save()
        self._refresh_checks()

    def toggle_login_item(self, _):
        macos.set_launch_at_login(not macos.launch_at_login_enabled())
        self._refresh_checks()

    @_in_thread
    def forget_skipped(self, _):
        answer = macos.dialog("Клипы, которые при первом запуске были отмечены «Считать обработанными», "
                              "снова станут новыми. Продолжить?", ["Отмена", "Вернуть"], default="Вернуть")
        if answer == "Вернуть":
            self.controller.submit("forget_skipped")

    @_in_thread
    def process_folder(self, _):
        folder = macos.choose_folder("Папка с клипами VID_*.mp4 (или карта камеры)")
        if folder:
            self.controller.submit("source", folder)

    @_in_thread
    def test_upload(self, _):
        file = macos.choose_file("Короткое видео для проверки загрузки (станет приватным)", ["mp4", "mov"])
        if file:
            self.controller.submit("test_upload", file)

    @_in_thread
    def choose_lut(self, _):
        lut = macos.choose_file("LUT для I-Log (.cube)", ["cube"])
        if lut:
            self.config.lut = str(lut)
            self.config.save()
            macos.notify("improv-video", f"LUT: {lut.name}")

    @_in_thread
    def choose_archive(self, _):
        folder = macos.choose_folder("Папка архива (лучше на внешнем диске)")
        if folder:
            self.config.archive = str(folder)
            self.config.save()
            macos.notify("improv-video", "Папка архива изменена — перезапустите приложение")


def main() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    config = AppConfig.load()
    logging.getLogger("improv-video").info(
        "improv-video %s: архив %s, копировать клипы: %s, качество %sp",
        __version__, config.archive, "да" if config.copy_clips else "нет", config.max_height)
    MenuBarApp().run()

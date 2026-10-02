"""Меню в строке меню macOS (rumps). Колбэки меню не блокируют интерфейс: работа уходит в очередь."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import rumps

from . import macos
from .config import LOG_FILE, AppConfig
from .controller import Controller

DENOISE = {"off": "Выключено", "weak": "Слабое", "medium": "Среднее (голос)", "strong": "Сильное (DeepFilterNet)"}
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
        self.manual_menu = rumps.MenuItem("Ручная загрузка")
        self.denoise_menu = rumps.MenuItem("Шумоподавление")
        for key, label in DENOISE.items():
            self.denoise_menu.add(rumps.MenuItem(label, callback=self._set_denoise(key)))
        self.mode_menu = rumps.MenuItem("Загрузка на YouTube")
        for key, label in MODES.items():
            self.mode_menu.add(rumps.MenuItem(label, callback=self._set_mode(key)))
        self.login_item = rumps.MenuItem("Запускать при входе в систему", callback=self.toggle_login_item)
        self.menu = [
            self.status_item,
            None,
            rumps.MenuItem("Обработать папку с клипами…", callback=self.process_folder),
            self.manual_menu,
            rumps.MenuItem("Открыть архив", callback=lambda _: macos.open_path(self.config.archive)),
            rumps.MenuItem("Вернуть клипы, отмеченные как обработанные…", callback=self.forget_skipped),
            None,
            rumps.MenuItem("Войти в YouTube…", callback=lambda _: self.controller.submit("login")),
            rumps.MenuItem("Проверить загрузку на YouTube…", callback=self.test_upload),
            self.mode_menu,
            None,
            rumps.MenuItem("Выбрать LUT…", callback=self.choose_lut),
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
        busy = self.controller.status != "Жду флешку"
        self.title = "⏳" if busy else "🎬"
        self.status_item.title = self.controller.status
        names = [m[0] for m in self.controller.manual]
        if names != getattr(self, "_manual_names", None):
            self._manual_names = names
            self.manual_menu.clear()
            if not names:
                self.manual_menu.add(rumps.MenuItem("Пока нет роликов"))
            for name, desc, file in self.controller.manual:
                self.manual_menu.add(rumps.MenuItem(
                    name, callback=lambda _, n=name, d=desc, f=file: self.controller.hand_off(n, d, f)))

    def _refresh_checks(self):
        for key, label in DENOISE.items():
            self.denoise_menu[label].state = int(self.config.denoise == key)
        for key, label in MODES.items():
            self.mode_menu[label].state = int(self.config.upload_mode == key)
        self.login_item.state = int(macos.launch_at_login_enabled())

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
    MenuBarApp().run()

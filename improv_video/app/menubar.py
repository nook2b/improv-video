"""Меню в строке меню macOS по дизайну «Меню improv-video».

Сверху — что происходит сейчас, посередине — ролики и частые действия, внизу — настройки в трёх
подменю. Обычные пункты системные; блок статуса, строки «Ролики» и значок рисует views.py.
Колбэки меню не блокируют интерфейс: работа уходит в очередь контроллера.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import rumps
from AppKit import (
    NSAttributedString,
    NSColor,
    NSEventTrackingRunLoopMode,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSImageLeft,
    NSMenu,
    NSMenuItem,
    NSMutableAttributedString,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
    NSRunLoop,
    NSTextAlignmentRight,
    NSTextTab,
)

from .. import __version__
from . import macos, updater, views
from . import menu_model as mm
from .config import LOG_FILE, SUPPORT_DIR, AppConfig
from ..pipeline import is_camera_card
from .controller import Controller

TAB = 236  # где кончаются значения справа («1080p», «не вошли») перед стрелкой подменю


DELETE_CHOICES = {0: "Не удалять", 1: "Через день", 3: "Через 3 дня", 7: "Через неделю"}


def _in_thread(fn):
    def run(*args):
        threading.Thread(target=fn, args=args, daemon=True).start()
    return run


def _attributed(text: str, value: str = "", *, size: float = 0, dim: float = 0.5, color=None):
    """Заголовок пункта с приглушённым значением справа («YouTube … вручную»)."""
    fnt = NSFont.menuFontOfSize_(size)
    para = NSMutableParagraphStyle.alloc().init()
    para.setTabStops_([NSTextTab.alloc().initWithTextAlignment_location_options_(NSTextAlignmentRight, TAB, {})])
    base = {NSFontAttributeName: fnt, NSParagraphStyleAttributeName: para}
    if color is not None:
        base[NSForegroundColorAttributeName] = color
    out = NSMutableAttributedString.alloc().initWithString_attributes_(text, base)
    if value:
        attrs = dict(base)
        attrs[NSForegroundColorAttributeName] = (color or NSColor.labelColor()).colorWithAlphaComponent_(dim)
        out.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_("\t" + value, attrs))
    return out


def _set_title(item: rumps.MenuItem, text: str, value: str = "") -> None:
    item._menuitem.setAttributedTitle_(_attributed(text, value))


def _header(text: str):
    """Заголовок группы в подменю («Качество видео»)."""
    if hasattr(NSMenuItem, "sectionHeaderWithTitle_"):  # macOS 14+
        return NSMenuItem.sectionHeaderWithTitle_(text)
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(text, None, "")
    item.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
        text, {NSFontAttributeName: NSFont.systemFontOfSize_weight_(11, 0.3),
               NSForegroundColorAttributeName: NSColor.tertiaryLabelColor()}))
    item.setEnabled_(False)
    return item


def _note(text: str):
    """Пояснение мелким шрифтом без действия."""
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(text, None, "")
    item.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
        text, {NSFontAttributeName: NSFont.systemFontOfSize_(11),
               NSForegroundColorAttributeName: NSColor.tertiaryLabelColor()}))
    item.setEnabled_(False)
    return item


def _raw(parent: rumps.MenuItem, nsitem) -> None:
    """Добавить в подменю системный пункт мимо rumps (заголовок, пояснение, своя вьюха)."""
    if parent._menu is None:  # как делает сам rumps при первом add
        parent._menu = NSMenu.alloc().init()
        parent._menuitem.setSubmenu_(parent._menu)
    parent._menu.addItem_(nsitem)


class MenuBarApp(rumps.App):
    def __init__(self, start: bool = True, updated_from: str = ""):
        super().__init__("improv-video", title=None, icon=str(views.ICONS / "menubar.png"), template=True,
                         quit_button=None)
        self.config = AppConfig.load()
        self.controller = Controller(self.config, macos)
        self._icon_state = None
        self._video_keys = None
        self._pending_update: tuple[str, Path] | None = None  # скачанная версия ждёт простоя
        self.update_state = mm.UpdateState(updated_from=updated_from,
                                           updated_at=datetime.now() if updated_from else None)
        self._version_text = ""

        # Верх: что происходит сейчас
        self.status_item = rumps.MenuItem("status")
        self.status_view = views.status_view(mm.StatusBlock(title="Жду флешку"))
        self.status_item._menuitem.setView_(self.status_view)
        self.stop_item = rumps.MenuItem("Остановить обработку", callback=self.stop_processing)

        # Ролики и частые действия
        self.videos_menu = rumps.MenuItem("Ролики")
        self.videos_menu.add(rumps.MenuItem("…"))

        # Настройки
        self.youtube_menu = rumps.MenuItem("YouTube")
        self.processing_menu = rumps.MenuItem("Обработка")
        self.app_menu = rumps.MenuItem("Приложение")
        self.quit_item = rumps.MenuItem("Выйти", callback=self.quit_app, key="q")

        self.menu = [
            self.status_item,
            self.stop_item,
            None,
            self.videos_menu,
            rumps.MenuItem("Обработать папку…", callback=self.process_folder),
            rumps.MenuItem("Открыть архив", callback=lambda _: macos.open_path(self.config.archive)),
            None,
            self.youtube_menu,
            self.processing_menu,
            self.app_menu,
            None,
            self.quit_item,
        ]
        self._build_youtube()
        self._build_processing()
        self._build_app()
        self._refresh_settings()
        if start:
            self.controller.start()
            timer = rumps.Timer(self._tick, 1)
            timer.start()
            # Пока меню открыто, обновлять блок прогресса тоже
            NSRunLoop.currentRunLoop().addTimer_forMode_(timer._nstimer, NSEventTrackingRunLoopMode)
            threading.Thread(target=self._update_loop, daemon=True).start()
        self._tick(None)

    # ---------- подменю настроек ----------

    def _build_youtube(self):
        m = self.youtube_menu
        self.account_nsitem = NSMenuItem.alloc().init()
        self.account_view = views.account_view(False, self.config.channel_name)
        self.account_nsitem.setView_(self.account_view)
        _raw(m, self.account_nsitem)
        self.login_item = rumps.MenuItem("Войти…", callback=self.toggle_youtube)
        m.add(self.login_item)
        m.add(rumps.separator)
        _raw(m, _header("Способ загрузки"))
        self.mode_manual = rumps.MenuItem("Вручную через YouTube Studio", callback=self._set_mode("manual"))
        self.mode_api = rumps.MenuItem("Автоматически «по ссылке»", callback=self._set_mode("api"))
        m.add(self.mode_manual)
        m.add(self.mode_api)
        _raw(m, _note("«Проверить загрузку…» покажет, ставит ли YouTube «по ссылке»"))
        m.add(rumps.separator)
        self.test_item = rumps.MenuItem("Проверить загрузку…", callback=self.test_upload)
        m.add(self.test_item)

    def _build_processing(self):
        m = self.processing_menu
        _raw(m, _header("Качество видео"))
        self.q1080 = rumps.MenuItem("1080p", callback=self._set_quality(1080))
        self.q2160 = rumps.MenuItem("4K", callback=self._set_quality(2160))
        m.add(self.q1080)
        m.add(self.q2160)
        _set_title(self.q1080, "1080p", "меньше места, быстрее")
        m.add(rumps.separator)
        self.auto_item = rumps.MenuItem("Автоцвет", callback=self.toggle_auto)
        m.add(self.auto_item)
        self.denoise_menu = rumps.MenuItem("Шумоподавление")
        self.denoise_items = {}
        for key, (label, hint) in mm.DENOISE.items():
            item = rumps.MenuItem(label, callback=self._set_denoise(key))
            self.denoise_menu.add(item)
            if hint:
                _set_title(item, label, hint)
            self.denoise_items[key] = item
        m.add(self.denoise_menu)
        self.lut_item = rumps.MenuItem("LUT…", callback=self.choose_lut)
        m.add(self.lut_item)
        m.add(rumps.separator)
        self.copy_item = rumps.MenuItem("Копировать клипы на Mac", callback=self.toggle_copy)
        m.add(self.copy_item)

    def _build_app(self):
        m = self.app_menu
        self.archive_item = rumps.MenuItem("Папка архива…", callback=self.choose_archive)
        m.add(self.archive_item)
        self.login_at_start = rumps.MenuItem("Запускать при входе в систему", callback=self.toggle_login_item)
        m.add(self.login_at_start)
        m.add(rumps.separator)
        m.add(rumps.MenuItem("Вернуть клипы, отмеченные как обработанные…", callback=self.forget_skipped))
        m.add(rumps.MenuItem("Открыть журнал", callback=lambda _: macos.open_path(LOG_FILE)))
        m.add(rumps.separator)
        self.delete_menu = rumps.MenuItem("Удалять ролики после загрузки")
        self.delete_items = {}
        for days, label in DELETE_CHOICES.items():
            item = rumps.MenuItem(label, callback=self._set_delete_days(days))
            self.delete_menu.add(item)
            self.delete_items[days] = item
        _raw(self.delete_menu, _note("Файл — в Корзину; дни от «Готово» или загрузки"))
        m.add(self.delete_menu)
        self.auto_update_item = rumps.MenuItem("Обновлять автоматически", callback=self.toggle_auto_update)
        m.add(self.auto_update_item)
        m.add(rumps.MenuItem("Проверить обновления", callback=self.check_updates))
        self.version_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(f"Версия {__version__}", None, "")
        self.version_item.setEnabled_(False)
        _raw(m, self.version_item)

    def _refresh_settings(self):
        """Галочки и значения справа — после любого изменения настроек."""
        c, cfg = self.controller, self.config
        signed = c.signed_in
        _set_title(self.youtube_menu, "YouTube", mm.youtube_value(signed, cfg.upload_mode))
        _set_title(self.processing_menu, "Обработка", mm.quality_value(cfg.max_height))
        self.login_item.title = "Выйти" if signed else "Войти…"
        if self.account_view.signed_in != signed:
            self.account_view.signed_in = signed
            self.account_view.setNeedsDisplay_(True)
        self.mode_manual.state = int(cfg.upload_mode != "api")
        self.mode_api.state = int(cfg.upload_mode == "api")
        self.mode_api.set_callback(self._set_mode("api") if signed else None)
        self.test_item.set_callback(self.test_upload if signed else None)

        self.q1080.state = int(cfg.max_height < 2160)
        self.q2160.state = int(cfg.max_height >= 2160)
        self.auto_item.state = int(cfg.auto_brightness)
        label, _ = mm.DENOISE.get(cfg.denoise, mm.DENOISE["medium"])
        _set_title(self.denoise_menu, "Шумоподавление", label)
        for key, item in self.denoise_items.items():
            item.state = int(cfg.denoise == key)
        lut = mm.ellipsize_middle(Path(cfg.lut).name) if cfg.lut else "не выбран"
        _set_title(self.lut_item, "LUT:", lut + "…")
        self.copy_item.state = int(cfg.copy_clips)

        archive = mm.short_path(str(Path(cfg.archive).expanduser()), str(Path.home()))
        _set_title(self.archive_item, "Папка архива:", mm.ellipsize_middle(archive, 30) + "…")
        self.login_at_start.state = int(macos.launch_at_login_enabled())
        self.auto_update_item.state = int(cfg.auto_update)
        for days, item in self.delete_items.items():
            item.state = int(cfg.delete_after_days == days)
        _set_title(self.delete_menu, "Удалять ролики после загрузки",
                   DELETE_CHOICES.get(cfg.delete_after_days, f"через {cfg.delete_after_days} дн.").lower())

    # ---------- обновление раз в секунду ----------

    def _tick(self, _):
        c = self.controller
        try:
            videos = c.videos()
        except Exception:  # noqa: BLE001 — журнал занят: покажем в следующую секунду
            videos = []
        busy = c.busy
        model = mm.status_block(progress=c.progress, status=c.status, busy=busy, note=c.note, videos=videos,
                                update=mm.update_notice(__version__, self.update_state, datetime.now()))
        version_text = mm.update_line(__version__, self.update_state)
        if version_text != self._version_text:
            self._version_text = version_text
            self.version_item.setTitle_(version_text)
        if self.status_view.update(model):
            menu = self.status_item._menuitem.menu()
            if menu is not None:
                menu.itemChanged_(self.status_item._menuitem)
        self.stop_item._menuitem.setHidden_(not busy)
        self.quit_item.title = "Выйти…" if busy else "Выйти"
        self._update_icon(*mm.icon_state(progress=c.progress, busy=busy, videos=videos))
        if self._pending_update and not busy and not c.delivering:
            self._install_update()

        tracking = NSRunLoop.currentRunLoop().currentMode() == NSEventTrackingRunLoopMode
        if not tracking:  # подменю перестраиваем только при закрытом меню
            key = (c.signed_in, self.config.lut, self.config.archive)
            if key != self._settings_key:  # вход в YouTube, LUT и папка меняются не из меню
                self._settings_key = key
                self._refresh_settings()
            self._refresh_videos(videos)

    _settings_key = None

    def _update_icon(self, state: str, text: str) -> None:
        key = (state, text)
        if key == self._icon_state:
            return
        item = getattr(getattr(self, "_nsapp", None), "nsstatusitem", None)
        if item is None:
            return
        self._icon_state = key
        button = item.button()
        button.setImage_(views.menubar_image(state))
        button.setImagePosition_(NSImageLeft)
        button.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
            f" {text}" if text else "",
            {NSFontAttributeName: NSFont.monospacedDigitSystemFontOfSize_weight_(13, 0.23)}))

    def _refresh_videos(self, videos) -> None:
        rows = [mm.video_row(v) for v in videos]
        keys = [(r.key, r.item.status, r.title, r.detail, r.fraction) for r in rows]
        waiting = mm.waiting_count(videos)
        _set_title(self.videos_menu, "Ролики", str(waiting) if waiting else "")
        if keys == self._video_keys:
            return
        self._video_keys = keys
        m = self.videos_menu
        m.clear()
        if not rows:
            item = NSMenuItem.alloc().init()
            item.setView_(views.note_view("Роликов пока нет", "Появятся после первой флешки или «Обработать папку…»",
                                          300, (12, 10, 12)))
            _raw(m, item)
            return
        for row in rows:
            item = rumps.MenuItem(row.key, callback=(lambda _, r=row: self._video_action(r)) if row.action else None)
            item._menuitem.setView_(views.video_row_view(row, self._video_action))
            m.add(item)
        m.add(rumps.separator)
        note = NSMenuItem.alloc().init()
        note.setView_(views.note_view(
            None, "Последние 10 роликов · не собранный день повторится при следующей вставке флешки",
            views.VIDEOS_WIDTH, (28, 3, 4)))
        _raw(m, note)

    def _video_action(self, row) -> None:
        """Клик по строке «Ролики» — действие по статусу."""
        c, it = self.controller, row.item
        if row.action == "kind":
            span = it.detail.partition(" · ")[2]  # «Ждёт выбора типа · 18:05–20:40»
            c.ask_kind(it.day, span)
        elif row.action == "hand_off" and it.video_id:
            c.hand_off_video(it.video_id)
        elif row.action == "open_url" and it.url:
            macos.open_path(it.url)
        elif row.action == "open_log":
            macos.open_path(LOG_FILE)

    # ---------- действия ----------

    def stop_processing(self, _):
        self.controller.stop_processing()

    def quit_app(self, _):
        if self.controller.busy:
            from . import dialogs

            label = self.controller.progress.label if self.controller.progress else "Несобранный день"
            if not dialogs.confirm_quit(label):
                return
            self.controller.stop_processing()
        rumps.quit_application()

    def toggle_youtube(self, _):
        self.controller.submit("logout" if self.controller.signed_in else "login")

    def _changed(self):
        self.config.save()
        self._refresh_settings()

    def _set_denoise(self, key):
        def cb(_):
            self.config.denoise = key
            self._changed()
        return cb

    def _set_mode(self, key):
        def cb(_):
            self.config.upload_mode = key
            self._changed()
            if key == "api":
                self.controller.submit("deliver")
        return cb

    def _set_quality(self, h):
        def cb(_):
            self.config.max_height = h
            self._changed()
        return cb

    def toggle_copy(self, _):
        self.config.copy_clips = not self.config.copy_clips
        self._changed()

    def toggle_auto(self, _):
        self.config.auto_brightness = not self.config.auto_brightness
        self._changed()

    # ---------- обновление приложения ----------

    def _update_loop(self) -> None:
        time.sleep(60)  # не мешать запуску
        while True:
            if self.config.auto_update:
                self._check_update(manual=False)
            time.sleep(updater.CHECK_SECONDS)

    @_in_thread
    def check_updates(self, _):
        self._check_update(manual=True)

    def _check_update(self, manual: bool) -> None:
        log = logging.getLogger("improv-video")

        def tell(text: str) -> None:
            """Ручная проверка отвечает окном: уведомления macOS может молча не показать."""
            log.info("Проверка обновлений: %s", text)
            if manual:
                threading.Thread(target=macos.dialog, args=(text, ["OK"]), daemon=True).start()

        if updater.current_bundle() is None:
            tell("Обновление работает только в собранном приложении")
            return
        u = self.update_state
        if u.stage in ("checking", "downloading"):
            return  # проверка уже идёт
        try:
            u.stage = "checking"
            release = updater.latest()
            u.checked_at = datetime.now()
            if release is None or not updater.is_newer(release.version, __version__):
                u.stage = "latest"
                tell(f"У вас последняя версия: {__version__}")
                return
            if self._pending_update and self._pending_update[0] == release.version:
                u.stage = "ready"
                tell(f"Версия {release.version} уже скачана и поставится, когда закончится обработка")
                return
            tell(f"Нашлась версия {release.version} (у вас {__version__}). Скачиваю — приложение "
                 "перезапустится само" + (", когда закончится обработка" if self.controller.busy else ""))
            log.info("Обновление: скачиваю %s", release.version)
            u.stage, u.version = "downloading", release.version
            app = updater.prepare(release, SUPPORT_DIR / "update")
            u.stage = "ready"
            self._pending_update = (release.version, app)
            log.info("Обновление %s скачано, поставлю, когда обработка закончится", release.version)
            if self.controller.busy:
                macos.notify("improv-video", f"Версия {release.version} поставится, когда закончится обработка")
        except Exception as e:  # noqa: BLE001 — нет сети, GitHub недоступен: попробуем в следующий раз
            u.stage, u.checked_at = "error", datetime.now()
            log.warning("Обновление не удалось: %s", e)
            if manual:
                threading.Thread(target=macos.dialog, args=(f"Не удалось проверить обновления: {str(e)[:200]}",
                                                            ["OK"]), daemon=True).start()

    def _install_update(self) -> None:
        version, app = self._pending_update
        self._pending_update = None
        bundle = updater.current_bundle()
        if bundle is None or not app.exists():
            return
        logging.getLogger("improv-video").info("Обновление до %s: перезапуск", version)
        updater.launch_swap(app, bundle, SUPPORT_DIR / "update", os.getpid())
        rumps.quit_application()

    def _set_delete_days(self, days: int):
        def cb(_):
            self.config.delete_after_days = days
            self._changed()
            self.controller.cleanup()
        return cb

    def toggle_auto_update(self, _):
        self.config.auto_update = not self.config.auto_update
        self._changed()

    def toggle_login_item(self, _):
        macos.set_launch_at_login(not macos.launch_at_login_enabled())
        self._refresh_settings()

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
        file = macos.choose_file("Короткое видео для проверки загрузки (загрузится «по ссылке»)", ["mp4", "mov"])
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
        if folder and is_camera_card(folder):
            macos.dialog(f"«{folder}» — это карта камеры. На неё нельзя складывать ролики: на карте почти "
                         "нет места, и сборка, читая и записывая одну карту, идёт очень медленно. "
                         "Выберите папку на Mac или внешнем диске.", ["OK"])
            return
        if folder:
            self.config.archive = str(folder)
            self.config.save()
            macos.notify("improv-video", "Папка архива изменена — перезапустите приложение")


def describe_menu(menu, depth: int = 0) -> list[str]:
    """Текстовый слепок меню для самопроверки: заголовки, галочки, скрытые и неактивные пункты."""
    lines = []
    for i in range(menu.numberOfItems()):
        item = menu.itemAtIndex_(i)
        if item.isSeparatorItem():
            lines.append("  " * depth + "—")
            continue
        title = item.attributedTitle().string() if item.attributedTitle() else item.title()
        flags = []
        if item.state():
            flags.append("✓")
        if item.isHidden():
            flags.append("скрыт")
        if item.view() is not None:
            flags.append(f"view {type(item.view()).__name__} {item.view().frame().size.height:.0f}pt")
        elif not item.isEnabled() or (item.action() is None and not item.hasSubmenu()):
            flags.append("неактивен")
        lines.append("  " * depth + title.replace("\t", " │ ") + (f"  [{', '.join(flags)}]" if flags else ""))
        if item.hasSubmenu():
            lines += describe_menu(item.submenu(), depth + 1)
    return lines


def render_selftest(outdir: Path) -> int:
    """Для CI: картинки нарисованных частей и слепок меню без запуска приложения."""
    from . import dialogs

    written = views.render_demo(outdir) + dialogs.render_demo(outdir)
    from datetime import date

    from ..progress import DayProgress

    app = MenuBarApp(start=False)
    # Прогнать меню со всеми состояниями, как в работе: ролики всех статусов, сборка, пусто
    _, rows = views.demo_models()
    videos = [r.item for r in rows]
    app._refresh_videos(videos)
    app._refresh_videos(videos)  # без изменений — ветка «ничего не делать»
    app.controller.progress = DayProgress("Тренировка 06.10.2026", 1, 2, reading_card=True, day=date(2026, 10, 6))
    app.controller.progress.start("encode")
    app._tick(None)
    app.controller.progress = None
    app._tick(None)
    app._refresh_videos([])
    text = describe_menu(app._menu._menu if hasattr(app._menu, "_menu") else app._menu)
    (outdir / "menu.txt").write_text("\n".join(text) + "\n", encoding="utf-8")
    print("\n".join(text))
    print(f"{len(written)} картинок в {outdir}")
    return 0


def main() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    config = AppConfig.load()
    if is_camera_card(Path(config.archive).expanduser()):
        # Архив на карте камеры: места нет, сборка медленная — назад в папку по умолчанию на Mac
        logging.getLogger("improv-video").warning("Папка архива была на карте камеры (%s) — вернул %s",
                                                  config.archive, AppConfig.archive)
        macos.notify("improv-video", "Папка архива была на карте камеры — вернул её в «Фильмы»")
        config.archive = AppConfig.archive
        config.save()
    logging.getLogger("improv-video").info(
        "improv-video %s: архив %s, копировать клипы: %s, качество %sp",
        __version__, config.archive, "да" if config.copy_clips else "нет", config.max_height)
    updated_from = ""
    if config.last_version != __version__:
        updated_from = config.last_version  # пусто при первом запуске
        config.last_version = __version__
        config.save()
    MenuBarApp(updated_from=updated_from).run()

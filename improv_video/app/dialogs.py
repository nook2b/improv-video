"""Собственные окна по дизайну (макеты 06–08): «Что снимали?», «Ролик готов», первый запуск, выход.

Окно — нативное NSWindow без заголовка; содержимое рисуется одной вьюхой (Canvas) по размерам
и цветам дизайн-системы, кнопки и карточки — области на ней. Функции ask_* вызываются из рабочих
потоков: окно открывается в основном потоке, поток ждёт ответа.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import objc
from AppKit import (
    NSAlert,
    NSApp,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSFloatingWindowLevel,
    NSMakePoint,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSTextField,
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSTrackingMouseMoved,
    NSView,
    NSWindow,
    NSWindowCloseButton,
    NSWindowMiniaturizeButton,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskTitled,
    NSWindowTitleHidden,
    NSWindowZoomButton,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

from ..naming import KINDS
from .views import _rgba, color, draw_icon, draw_line, draw_wrapped, fill_round, font, is_dark, render_png, text_width

# Цвета окон, которых нет в меню (tokens/colors.css)
WINDOW = {False: "#FBFAF8", True: "#1F1F21"}  # --bg-window
SHEET = {False: "#FFFFFF", True: "#262628"}  # --bg-sheet
CONTROL = {False: "#FFFFFF", True: "#2C2C2F"}  # --bg-control
CONTROL_HOVER = {False: "#F6F5F2", True: "#333336"}
ACCENT_HOVER = {False: "#2459C4", True: "#76A6FF"}
ACCENT_SOFT = {False: (47, 111, 235, 0.10), True: (90, 147, 255, 0.14)}
RING = {False: (0, 0, 0, 0.10), True: (255, 255, 255, 0.08)}  # 0.5px кольцо --shadow-control
BORDER_STRONG = {False: (0, 0, 0, 0.18), True: (255, 255, 255, 0.18)}

_open: set = set()  # открытые окна: держим ссылки, пока не закрыты


# ---------- холст с кнопками ----------

class Canvas(NSView):
    """Рисует окно функцией paint(canvas, dark, draw) → высота; области-кнопки — через hit()."""

    def isFlipped(self):
        return True

    def acceptsFirstResponder(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    @objc.python_method
    def setup(self, paint, on_click, on_key, background):
        self.paint, self.on_click, self.on_key, self.background = paint, on_click, on_key, background
        self.hits, self.hover, self.state = [], None, {}
        self.area = None

    @objc.python_method
    def hit(self, key, x, y, w, h) -> None:
        self.hits.append(((x, y, w, h), key))

    @objc.python_method
    def measure(self) -> float:
        return self.paint(self, False, False)

    def drawRect_(self, rect):
        dark = is_dark(self)
        _rgba(self.background[dark]).set()
        NSBezierPath.fillRect_(self.bounds())
        self.hits = []
        self.paint(self, dark, True)

    def updateTrackingAreas(self):
        if self.area is not None:
            self.removeTrackingArea_(self.area)
        opts = NSTrackingMouseEnteredAndExited | NSTrackingMouseMoved | NSTrackingActiveAlways | NSTrackingInVisibleRect
        self.area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(self.bounds(), opts, self, None)
        self.addTrackingArea_(self.area)
        objc.super(Canvas, self).updateTrackingAreas()

    @objc.python_method
    def _key_at(self, event):
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        for (x, y, w, h), key in self.hits:
            if x <= p.x <= x + w and y <= p.y <= y + h:
                return key
        return None

    def mouseMoved_(self, event):
        key = self._key_at(event)
        if key != self.hover:
            self.hover = key
            self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        if self.hover is not None:
            self.hover = None
            self.setNeedsDisplay_(True)

    def mouseDown_(self, event):
        if self._key_at(event) is None:
            self.window().performWindowDragWithEvent_(event)  # окно без заголовка двигается за фон

    def mouseUp_(self, event):
        key = self._key_at(event)
        if key is not None:
            self.on_click(key)

    def keyDown_(self, event):
        code = event.keyCode()
        if code in (36, 76):  # Return, Enter
            self.on_key("return")
        elif code == 53:  # Esc
            self.on_key("escape")
        else:
            objc.super(Canvas, self).keyDown_(event)


@dataclass
class Btn:
    key: str
    label: str
    variant: str = "secondary"  # primary | secondary | ghost | danger
    icon: str | None = None
    width: float | None = None


def button_width(b: Btn) -> float:
    if b.width:
        return b.width
    w = 12 * 2 + text_width(b.label, font(13, 500))
    return w + (14 + 6 if b.icon else 0)


def draw_button(c, b: Btn, x, y, dark: bool, draw: bool) -> float:
    """Кнопка дизайн-системы, размер md: 28 px, радиус 8, 13/500."""
    w = button_width(b)
    if not draw:
        return w
    hover = c.hover == b.key
    if b.variant == "primary":
        fill_round(x, y, w, 28, 8, _rgba(ACCENT_HOVER[dark]) if hover else color("accent", dark))
        ink = _rgba("#FFFFFF")
    elif b.variant == "ghost":
        if hover:
            fill_round(x, y, w, 28, 8, color("bg-row-hover", dark))
        ink = color("text-primary", dark)
    else:
        fill_round(x - 0.5, y - 0.5, w + 1, 29, 8.5, _rgba(RING[dark]))
        fill_round(x, y, w, 28, 8, _rgba(CONTROL_HOVER[dark] if hover else CONTROL[dark]))
        ink = color("text-danger" if b.variant == "danger" else "text-primary", dark)
    content = text_width(b.label, font(13, 500)) + (14 + 6 if b.icon else 0)
    cx = x + (w - content) / 2
    if b.icon:
        draw_icon(b.icon, cx, y + 7, 14, ink)
        cx += 14 + 6
    draw_line(b.label, cx, y, 28, font(13, 500), ink)
    c.hit(b.key, x, y, w, 28)
    return w


def draw_buttons_right(c, buttons: list[Btn], right, y, dark, draw) -> None:
    x = right
    for b in reversed(buttons):
        x -= button_width(b)
        draw_button(c, b, x, y, dark, draw)
        x -= 8


# ---------- окно ----------

def _window(width: float, canvas: Canvas):
    height = canvas.measure()
    style = NSWindowStyleMaskTitled | NSWindowStyleMaskFullSizeContentView
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, width, height), style, NSBackingStoreBuffered, False)
    win.setTitlebarAppearsTransparent_(True)
    win.setTitleVisibility_(NSWindowTitleHidden)
    for b in (NSWindowCloseButton, NSWindowMiniaturizeButton, NSWindowZoomButton):
        btn = win.standardWindowButton_(b)
        if btn is not None:
            btn.setHidden_(True)
    win.setReleasedWhenClosed_(False)
    win.setLevel_(NSFloatingWindowLevel)
    win.setBackgroundColor_(NSColor.windowBackgroundColor())
    canvas.setFrame_(NSMakeRect(0, 0, width, height))
    win.setContentView_(canvas)
    win.center()
    return win


def _show(win) -> None:
    _open.add(win)
    NSApp.activateIgnoringOtherApps_(True)
    win.makeKeyAndOrderFront_(None)
    win.makeFirstResponder_(win.contentView())


def _close(win) -> None:
    win.orderOut_(None)
    win.close()
    _open.discard(win)


def _ask(build: Callable[[Callable[[object], None]], object], timeout: float | None = None):
    """Открыть окно в основном потоке и дождаться ответа (из рабочего потока)."""
    done, box = threading.Event(), {}

    def answer(value):
        if "value" not in box:
            box["value"] = value
            done.set()

    def open_window():
        box["win"] = build(answer)
        _show(box["win"])

    AppHelper.callAfter(open_window)
    if not done.wait(timeout):
        answer(None)
    win = box.get("win")
    if win is not None:
        AppHelper.callAfter(_close, win)
    return box["value"]


# ---------- «Что снимали?» (макет 06) ----------

@dataclass
class KindInfo:
    day: str  # «06.10.2026»
    meta: str = ""  # «18:05–20:40 · 1.5 ч · 12 клипов»
    day_note: str = ""  # «день 1 из 2»
    default: str = "training"  # как в прошлый раз
    playlists: object = None  # controller.PlaylistChoice — строка «Плейлист», если есть вход с доступом к ним


NO_PLAYLIST = "без плейлиста"


def _playlist_of(entry):
    """[id, название] из настроек → объект с .id и .title; None — без плейлиста."""
    from ..youtube import Playlist

    return Playlist(entry[0], entry[1]) if entry else None


def chosen_playlist(c, info: KindInfo, kind: str):
    """Выбранный в строке плейлист, а если его не трогали — последний для этого типа
    (или для типа «как в прошлый раз»)."""
    if "playlist" in c.state:
        return c.state["playlist"]
    by_kind = info.playlists.by_kind
    return _playlist_of(by_kind.get(kind) if kind in by_kind else by_kind.get(info.default))


class _MenuTarget(NSObject):
    def pick_(self, sender):
        self.callback(sender.tag())


def _pick_playlist(c, info: KindInfo, x: float, y: float) -> None:
    """Выпадающий список: плейлисты канала, «Новый плейлист…», «Без плейлиста»."""
    items = list(info.playlists.playlists)
    menu = NSMenu.alloc().init()
    menu.setAutoenablesItems_(False)
    target = _MenuTarget.alloc().init()
    picked = {}
    target.callback = lambda tag: picked.setdefault("tag", tag)
    current = chosen_playlist(c, info, info.default)
    for i, p in enumerate(items):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(p.title, "pick:", "")
        item.setTarget_(target)
        item.setTag_(i)
        item.setState_(int(bool(current) and current.id == p.id))
        menu.addItem_(item)
    if items:
        menu.addItem_(NSMenuItem.separatorItem())
    for tag, label in ((-1, "Новый плейлист…"), (-2, "Без плейлиста")):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, "pick:", "")
        item.setTarget_(target)
        item.setTag_(tag)
        menu.addItem_(item)
    menu.popUpMenuPositioningItem_atLocation_inView_(None, NSMakePoint(x, y), c)
    tag = picked.get("tag")
    if tag is None:
        return
    if tag >= 0:
        c.state["playlist"] = items[tag]
    elif tag == -2:
        c.state["playlist"] = None
    else:
        name = _ask_text("Новый плейлист", "Название, например имя команды", "Создать")
        if not name:
            return
        try:
            created = info.playlists.create(name)
        except Exception as e:  # noqa: BLE001 — нет сети, нет доступа
            _alert(f"Не удалось создать плейлист: {str(e)[:200]}")
            return
        info.playlists.playlists = sorted(items + [created], key=lambda p: p.title.lower())
        c.state["playlist"] = created
    c.setNeedsDisplay_(True)


def _ask_text(title: str, placeholder: str, ok: str) -> str:
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.addButtonWithTitle_(ok)
    alert.addButtonWithTitle_("Отмена")
    field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 260, 24))
    field.setPlaceholderString_(placeholder)
    alert.setAccessoryView_(field)
    alert.window().setInitialFirstResponder_(field)
    return field.stringValue().strip() if alert.runModal() == 1000 else ""  # 1000 — первая кнопка


def _alert(text: str) -> None:
    alert = NSAlert.alloc().init()
    alert.setMessageText_(text)
    alert.runModal()


KIND_LABELS = KINDS


def kind_painter(info: KindInfo):
    def paint(c, dark, draw):
        x0, w = 20, 420 - 40
        y = 20
        tag = "improv-video" + (f" · {info.day_note}" if info.day_note else "")
        draw_line(tag, x0, y, 11, font(11, 500), color("text-tertiary", dark), draw=draw)
        if draw:  # «Ответить позже»
            hover = c.hover == "later"
            if hover:
                fill_round(x0 + w - 24, y - 2, 24, 24, 8, color("bg-row-hover", dark))
            draw_icon("x", x0 + w - 24 + 5, y - 2 + 5, 14, color("text-primary" if hover else "text-secondary", dark))
            c.hit("later", x0 + w - 24, y - 2, 24, 24)
        y += 11 + 6
        y += draw_wrapped(f"Что снимали {info.day}?", x0, y, w - 36, font(17, 600), color("text-primary", dark), draw)
        if info.meta:
            y += 6
            if draw:
                draw_icon("clock", x0, y, 12, color("text-secondary", dark))
            draw_line(info.meta, x0 + 17, y, 12, font(12, digits=True), color("text-secondary", dark), draw=draw)
            y += 12
        y += 16
        if info.playlists is not None:  # «Плейлист  [Команда А ▾]» — выбрать до карточки
            if draw:
                _playlist_row(c, info, x0, y, w, dark)
            y += 30 + 6
            y += draw_wrapped("Выберите плейлист, затем — что снимали", x0, y, w, font(11),
                              color("text-tertiary", dark), draw)
            y += 12
        card_w = (w - 10) / 2
        for i, kind in enumerate(KINDS):  # по два в ряд
            cx, cy = x0 + i % 2 * (card_w + 10), y + i // 2 * (88 + 10)
            if draw:
                _kind_card(c, kind, kind == info.default, cx, cy, card_w, info, dark)
        rows = (len(KINDS) + 1) // 2
        y += rows * 88 + (rows - 1) * 10 + 16
        y += draw_wrapped("Ролик собирается уже сейчас. Ответить можно и позже — из меню «Ролики».",
                          x0, y, w, font(12), color("text-tertiary", dark), draw)
        return y + 18
    return paint


def _playlist_row(c, info: KindInfo, x, y, w, dark) -> None:
    draw_line("Плейлист", x, y, 30, font(13, 500), color("text-secondary", dark))
    bx, bw = x + 80, w - 80
    hover = c.hover == "playlist"
    fill_round(bx - 0.5, y - 0.5, bw + 1, 31, 8.5, _rgba(BORDER_STRONG[dark] if hover else RING[dark]))
    fill_round(bx, y, bw, 30, 8, _rgba(CONTROL_HOVER[dark] if hover else CONTROL[dark]))
    p = chosen_playlist(c, info, info.default)
    label = p.title if p else NO_PLAYLIST
    ink = color("text-primary" if p else "text-tertiary", dark)
    draw_line(_fit_middle(label, "", bw - 40, font(13)), bx + 12, y, 30, font(13), ink)
    draw_line("▾", 0, y, 30, font(12), color("text-secondary", dark), right=bx + bw - 12)
    c.hit("playlist", bx, y, bw, 30)
    c.state["playlist_at"] = (bx, y + 30)


def _kind_card(c, kind, chosen, x, y, w, info, dark) -> None:
    hover = c.hover == kind
    if chosen:
        accent = color("accent", dark)
        fill_round(x - 1.5, y - 1.5, w + 3, 91, 13.5, accent)
        fill_round(x, y, w, 88, 12, _rgba(WINDOW[dark]))
        fill_round(x, y, w, 88, 12, _rgba(ACCENT_SOFT[dark]))
        draw_line("как в прошлый раз", x + 14, y + 12, 11, font(11, 500), accent)
        draw_line("↩", 0, y + 10, 11, font(11, 500), color("text-tertiary", dark), right=x + w - 12)
    else:
        fill_round(x - 0.5, y - 0.5, w + 1, 89, 12.5, _rgba(BORDER_STRONG[dark] if hover else RING[dark]))
        fill_round(x, y, w, 88, 12, _rgba(CONTROL_HOVER[dark] if hover else CONTROL[dark]))
    name = KIND_LABELS[kind]
    draw_line(f"«{name} {info.day}»", x + 14, y + 88 - 12 - 14, 14, font(11), color("text-secondary", dark))
    draw_line(name, x + 14, y + 88 - 12 - 14 - 5 - 18, 18, font(15, 600), color("text-primary", dark))
    c.hit(kind, x, y, w, 88)


def build_kind(info: KindInfo, answer):
    canvas = Canvas.alloc().initWithFrame_(NSMakeRect(0, 0, 420, 300))

    def reply(kind):
        if kind is None or info.playlists is None:
            answer(kind)
        else:
            answer((kind, chosen_playlist(canvas, info, kind)))

    def click(key):
        if key == "playlist":
            _pick_playlist(canvas, info, *canvas.state.get("playlist_at", (0, 0)))
        else:
            reply(None if key == "later" else key)

    canvas.setup(kind_painter(info), click, lambda k: reply(info.default if k == "return" else None), WINDOW)
    return _window(420, canvas)


def ask_kind(info: KindInfo, timeout: float | None = None):
    """Ключ из KINDS, (ключ, плейлист или None), если показан выбор плейлиста, или None — ответить позже."""
    return _ask(lambda answer: build_kind(info, answer), timeout)


# ---------- «Обложка» ----------

IMAGE_TYPES = ("jpg", "jpeg", "png", "heic", "heif", "webp", "gif", "bmp", "tif", "tiff")


@dataclass
class ThumbInfo:
    videos: list  # [(id ролика, название)], новые сверху


def thumb_painter(info: ThumbInfo):
    def paint(c, dark, draw):
        primary, secondary, tertiary = (color(n, dark) for n in ("text-primary", "text-secondary", "text-tertiary"))
        x0, w = 20, 420 - 40
        y = 20
        draw_line("improv-video · обложка", x0, y, 11, font(11, 500), tertiary, draw=draw)
        y += 11 + 6
        y += draw_wrapped("Обложка для ролика на YouTube", x0, y, w, font(17, 600), primary, draw)
        y += 14
        if draw:  # «Ролик  [Шоу 08.10.2026 ▾]»
            draw_line("Ролик", x0, y, 30, font(13, 500), secondary)
            bx, bw = x0 + 80, w - 80
            hover = c.hover == "video"
            fill_round(bx - 0.5, y - 0.5, bw + 1, 31, 8.5, _rgba(BORDER_STRONG[dark] if hover else RING[dark]))
            fill_round(bx, y, bw, 30, 8, _rgba(CONTROL_HOVER[dark] if hover else CONTROL[dark]))
            name = c.state.get("video", info.videos[0] if info.videos else (None, "—"))[1]
            draw_line(_fit_middle(name, "", bw - 40, font(13)), bx + 12, y, 30, font(13), primary)
            draw_line("▾", 0, y, 30, font(12), secondary, right=bx + bw - 12)
            c.hit("video", bx, y, bw, 30)
            c.state["video_at"] = (bx, y + 30)
        y += 30 + 12
        zone_h = 130
        if draw:  # место для картинки: перетащить сюда или «Выбрать файл…»
            over = c.state.get("drag_over") or c.hover == "pick"
            fill_round(x0 - 1, y - 1, w + 2, zone_h + 2, 13, color("accent", dark) if over else _rgba(BORDER_STRONG[dark]))
            fill_round(x0, y, w, zone_h, 12, _rgba(WINDOW[dark]))  # заливки ниже полупрозрачные — не поверх рамки
            fill_round(x0, y, w, zone_h, 12, _rgba(ACCENT_SOFT[dark]) if over else color("bg-field", dark))
            image = c.state.get("image")
            if image:
                draw_icon("circle-check", x0 + w / 2 - 9, y + 34, 18, color("accent", dark))
                _centered(_fit_middle(Path(image).name, "", w - 40, font(13, 500)), y + 60, 18, font(13, 500), primary, x0 + w / 2)
                _centered("Перетащите другую, чтобы заменить", y + 82, 15, font(12), secondary, x0 + w / 2)
            else:
                draw_icon("folder-open", x0 + w / 2 - 9, y + 34, 18, secondary)
                _centered("Перетащите картинку сюда", y + 60, 18, font(13, 500), primary, x0 + w / 2)
                _centered("или нажмите, чтобы выбрать файл", y + 82, 15, font(12), secondary, x0 + w / 2)
            c.hit("pick", x0, y, w, zone_h)
        y += zone_h + 10
        y += draw_wrapped("Подойдёт JPEG, PNG или HEIC. Свою обложку YouTube разрешает, когда у канала "
                          "подтверждён номер телефона.", x0, y, w, font(11), tertiary, draw)
        y += 16
        buttons = [Btn("cancel", "Отмена", "ghost")]
        if c.state.get("image"):
            buttons.append(Btn("set", "Поставить", "primary"))
        draw_buttons_right(c, buttons, x0 + w, y, dark, draw)
        return y + 28 + 16
    return paint


def _centered(text, y, line, fnt, col, cx) -> None:
    draw_line(text, cx - text_width(text, fnt) / 2, y, line, fnt, col)


class ThumbCanvas(Canvas):
    """Холст окна «Обложка»: принимает перетащенную картинку."""

    def draggingEntered_(self, sender):
        if self._dragged_image(sender) is None:
            return 0  # NSDragOperationNone
        self.state["drag_over"] = True
        self.setNeedsDisplay_(True)
        return 1  # NSDragOperationCopy

    def draggingExited_(self, sender):
        self.state["drag_over"] = False
        self.setNeedsDisplay_(True)

    def performDragOperation_(self, sender):
        image = self._dragged_image(sender)
        self.state["drag_over"] = False
        if image is None:
            return False
        self.state["image"] = image
        self.setNeedsDisplay_(True)
        return True

    @objc.python_method
    def _dragged_image(self, sender):
        from Foundation import NSURL

        urls = sender.draggingPasteboard().readObjectsForClasses_options_(
            [NSURL], {"NSPasteboardURLReadingFileURLsOnlyKey": True}) or []
        for url in urls:
            path = Path(str(url.path()))
            if path.suffix.lower().lstrip(".") in IMAGE_TYPES:
                return path
        return None


def _choose_image():
    from AppKit import NSOpenPanel

    panel = NSOpenPanel.openPanel()
    panel.setAllowedFileTypes_(list(IMAGE_TYPES))
    panel.setMessage_("Картинка для обложки")
    if panel.runModal() == 1:  # NSModalResponseOK
        return Path(str(panel.URL().path()))
    return None


def _pick_video(c, info: ThumbInfo, x: float, y: float) -> None:
    menu = NSMenu.alloc().init()
    target = _MenuTarget.alloc().init()
    picked = {}
    target.callback = lambda tag: picked.setdefault("tag", tag)
    current = c.state.get("video", info.videos[0] if info.videos else None)
    for i, (vid, name) in enumerate(info.videos):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(name, "pick:", "")
        item.setTarget_(target)
        item.setTag_(i)
        item.setState_(int(bool(current) and current[0] == vid))
        menu.addItem_(item)
    menu.popUpMenuPositioningItem_atLocation_inView_(None, NSMakePoint(x, y), c)
    if "tag" in picked:
        c.state["video"] = info.videos[picked["tag"]]
        c.setNeedsDisplay_(True)


def build_thumb(info: ThumbInfo, answer):
    canvas = ThumbCanvas.alloc().initWithFrame_(NSMakeRect(0, 0, 420, 300))

    def finish():
        if canvas.state.get("image") and info.videos:
            answer((canvas.state.get("video", info.videos[0])[0], canvas.state["image"]))

    def click(key):
        if key == "video":
            _pick_video(canvas, info, *canvas.state.get("video_at", (0, 0)))
        elif key == "pick":
            image = _choose_image()
            if image:
                canvas.state["image"] = image
                canvas.setNeedsDisplay_(True)
        elif key == "set":
            finish()
        else:
            answer(None)

    canvas.setup(thumb_painter(info), click, lambda k: finish() if k == "return" else answer(None), WINDOW)
    canvas.registerForDraggedTypes_(["public.file-url"])
    return _window(420, canvas)


def ask_thumbnail(info: ThumbInfo):
    """(id ролика, путь к картинке) или None."""
    return _ask(lambda answer: build_thumb(info, answer))


# ---------- «Ролик готов» (макет 07) ----------

@dataclass
class ReadyInfo:
    name: str
    description: str
    file: Path
    size: str = ""  # «4.2 ГБ»


STEPS = ["В YouTube Studio нажмите «Создать → Добавить видео»", "Перетащите выделенный файл из Finder",
         "Вставьте название (⌘V)", "Видимость — «Доступ по ссылке»"]


def ready_painter(info: ReadyInfo):
    def paint(c, dark, draw):
        primary, secondary, tertiary = (color(n, dark) for n in ("text-primary", "text-secondary", "text-tertiary"))
        W = 420
        x0, w = 20, W - 40
        y = 20
        if draw:
            draw_icon("circle-check", x0, y + 2, 18, color("text-success", dark))
        tx = x0 + 18 + 10
        y += draw_wrapped(f"{info.name} готово", tx, y, W - tx - 20, font(17, 600), primary, draw)
        y += 5
        draw_line("Finder с файлом и YouTube Studio уже открыты", tx, y, 16, font(12), secondary, draw=draw)
        y += 16 + 14
        copied = c.state.get("copied", "name")
        fields = [("Название · уже в буфере", "name", info.name)]
        if info.description:
            fields.append(("Описание", "description", info.description))
        for i, (label, key, value) in enumerate(fields):
            if i:
                y += 10
            draw_line(label, x0, y, 11, font(11, 500), tertiary, draw=draw)
            y += 11 + 5
            done = copied == key
            b = Btn(f"copy-{key}", "Скопировано" if done else "Скопировать", icon="check" if done else "copy",
                    width=128)
            field_w = w - 128 - 8
            if draw:
                fill_round(x0, y, field_w, 28, 8, color("bg-field", dark))
                fnt = font(13, digits=True)
                text = value
                while text_width(text, fnt) > field_w - 20 and len(text) > 1:
                    text = text[:-2] + "…"
                draw_line(text, x0 + 10, y, 28, fnt, primary)
            draw_button(c, b, x0 + field_w + 8, y, dark, draw)
            y += 28
        y += 16
        if draw:
            color("border-subtle", dark).set()
            NSBezierPath.fillRect_(NSMakeRect(x0, y, w, 0.5))
        y += 14
        for i, step in enumerate(STEPS, 1):
            if i > 1:
                y += 9
            if draw:
                _rgba(BORDER_STRONG[dark]).set()
                ring = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x0 + 0.25, y + 0.25, 17.5, 17.5))
                ring.setLineWidth_(0.5)
                ring.stroke()
                num_w = text_width(str(i), font(11, 500))
                draw_line(str(i), x0 + (18 - num_w) / 2, y, 18, font(11, 500), secondary)
            h = draw_wrapped(step, x0 + 28, y, w - 28, font(13), primary, draw)
            y += max(18, h)
        y += 16
        if draw:
            color("border-subtle", dark).set()
            NSBezierPath.fillRect_(NSMakeRect(0, y, W, 0.5))
        y += 12
        buttons = [Btn("reveal", "Показать файл", "ghost", "folder-open"), Btn("done", "Готово", "primary")]
        right = W - 16
        if draw:
            draw_buttons_right(c, buttons, right, y, dark, True)
            used = sum(button_width(b) for b in buttons) + 8 + 8
            room = right - used - (x0 + 18)
            fnt = font(12)
            label = _fit_middle(info.file.name, f" · {info.size}" if info.size else "", room, fnt)
            draw_icon("film", x0, y + 8, 12, secondary)
            draw_line(label, x0 + 18, y, 28, fnt, secondary)
        y += 28
        return y + 14
    return paint


def _fit_middle(name: str, tail: str, room: float, fnt) -> str:
    """Имя файла с многоточием посередине, хвост («· 4.2 ГБ») всегда виден."""
    if text_width(name + tail, fnt) <= room:
        return name + tail
    head, end = name, ""
    stem, dot, ext = name.rpartition(".")
    if dot:
        head, end = stem, "." + ext
    while len(head) > 1 and text_width(head + "…" + end + tail, fnt) > room:
        head = head[:-1]
    return head.rstrip() + "…" + end + tail


def build_ready(info: ReadyInfo, answer, actions):
    canvas = Canvas.alloc().initWithFrame_(NSMakeRect(0, 0, 420, 400))

    def click(key):
        if key == "copy-name":
            actions["copy"](info.name)
            canvas.state["copied"] = "name"
        elif key == "copy-description":
            actions["copy"](info.description)
            canvas.state["copied"] = "description"
        elif key == "reveal":
            actions["reveal"](info.file)
        elif key == "done":
            answer(True)
        canvas.setNeedsDisplay_(True)

    canvas.setup(ready_painter(info), click, lambda k: answer(True), WINDOW)
    return _window(420, canvas)


def show_ready(info: ReadyInfo, actions: dict) -> None:
    """actions: copy(text), reveal(path). Ждёт «Готово»."""
    _ask(lambda answer: build_ready(info, answer, actions))


# ---------- первый запуск (макет 08) ----------

def _plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def first_run_painter(clips: int, days: int):
    def paint(c, dark, draw):
        primary, secondary, tertiary = (color(n, dark) for n in ("text-primary", "text-secondary", "text-tertiary"))
        x0, w = 20, 420 - 40
        y = 20
        draw_line("improv-video · первый запуск", x0, y, 11, font(11, 500), tertiary, draw=draw)
        y += 11 + 6
        title = f"На карте {clips} {_plural(clips, 'клип', 'клипа', 'клипов')} примерно за {days} дн."
        y += draw_wrapped(title, x0, y, w, font(17, 600), primary, draw)
        y += 6
        y += draw_wrapped("Это первый запуск: обработать их все или считать уже обработанными?",
                          x0, y, w, font(13), secondary, draw)
        y += 16
        tile_w = (w - 10) / 2
        tiles = [("Обработать все", f"{days} {_plural(days, 'ролик', 'ролика', 'роликов')} по дням съёмки"),
                 ("Считать обработанными", "Пойдут только новые съёмки")]
        heights = [10 + 16 + 3 + draw_wrapped(sub, 0, 0, tile_w - 24, font(12), secondary, False) + 10
                   for _, sub in tiles]
        tile_h = max(heights)
        if draw:
            for i, (head, sub) in enumerate(tiles):
                tx = x0 + i * (tile_w + 10)
                fill_round(tx, y, tile_w, tile_h, 10, color("bg-field", dark))
                draw_line(head, tx + 12, y + 10, 16, font(12, 500), primary)
                draw_wrapped(sub, tx + 12, y + 10 + 16 + 3, tile_w - 24, font(12), secondary)
        y += tile_h + 16
        draw_line("Передумаете — Приложение ▸ Вернуть клипы…", x0, y, 15, font(11), tertiary, draw=draw)
        y += 15 + 16
        draw_buttons_right(c, [Btn("skip", "Считать обработанными"), Btn("all", "Обработать все", "primary")],
                           x0 + w, y, dark, draw)
        return y + 28 + 16
    return paint


def build_first_run(clips: int, days: int, answer):
    canvas = Canvas.alloc().initWithFrame_(NSMakeRect(0, 0, 420, 300))
    canvas.setup(first_run_painter(clips, days), answer,
                 lambda k: answer("all" if k == "return" else None), WINDOW)
    return _window(420, canvas)


def ask_first_run(clips: int, days: int) -> str | None:
    """all | skip | None (закрыли — спросить при следующей вставке)."""
    return _ask(lambda answer: build_first_run(clips, days, answer))


# ---------- выход во время обработки ----------

def quit_painter(label: str):
    def paint(c, dark, draw):
        x0, w = 20, 300 - 40
        y = 20
        y += draw_wrapped("Остановить обработку и выйти?", x0, y, w, font(13, 600), color("text-primary", dark), draw)
        y += 6
        y += draw_wrapped(f"{label} соберётся заново при следующей вставке флешки.", x0, y, w, font(12),
                          color("text-secondary", dark), draw)
        y += 14
        draw_buttons_right(c, [Btn("cancel", "Отмена"), Btn("quit", "Выйти", "danger")], x0 + w, y, dark, draw)
        return y + 28 + 20
    return paint


def confirm_quit(label: str) -> bool:
    """Модально в основном потоке (вызывается из меню)."""
    result = {"quit": False}
    canvas = Canvas.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 160))

    def finish(key):
        result["quit"] = key == "quit"
        NSApp.stopModal()

    canvas.setup(quit_painter(label), finish, lambda k: finish("cancel"), SHEET)
    win = _window(300, canvas)
    _show(win)
    NSApp.runModalForWindow_(win)
    _close(win)
    return result["quit"]


# ---------- картинки для самопроверки ----------

class _DemoChoice:
    from ..youtube import Playlist as _P

    playlists = [_P("PLa", "Команда А"), _P("PLm", "Мастер-классы")]
    by_kind = {"show": ["PLa", "Команда А"], "masterclass": ["PLm", "Мастер-классы"]}

    @staticmethod
    def create(name):
        raise RuntimeError("демо")


def render_demo(outdir: Path) -> list[Path]:
    from AppKit import NSAppearance, NSAppearanceNameAqua, NSAppearanceNameDarkAqua

    painters = {
        "dialog-kind": (420, kind_painter(KindInfo("06.10.2026", "18:05–20:40 · 1.5 ч · 12 клипов", "день 1 из 2")),
                        WINDOW),
        "dialog-ready": (420, ready_painter(ReadyInfo("Тренировка 06.10.2026", "",
                                                      Path("Тренировка 06.10.2026.mp4"), "4.2 ГБ")), WINDOW),
        "dialog-kind-playlist": (420, kind_painter(KindInfo(
            "06.10.2026", "18:05–20:40 · 1.5 ч · 12 клипов", "", "show", _DemoChoice())), WINDOW),
        "dialog-first-run": (420, first_run_painter(20, 3), WINDOW),
        "dialog-thumb": (420, thumb_painter(ThumbInfo([(3, "Шоу 08.10.2026"), (2, "Тренировка 06.10.2026")])),
                         WINDOW),
        "dialog-quit": (300, quit_painter("Тренировка 06.10.2026"), SHEET),
    }
    written = []
    for dark in (False, True):
        for name, (width, paint, bg) in painters.items():
            canvas = Canvas.alloc().initWithFrame_(NSMakeRect(0, 0, width, 100))
            canvas.setup(paint, lambda k: None, lambda k: None, bg)
            canvas.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameDarkAqua if dark else NSAppearanceNameAqua))
            canvas.setFrameSize_((width, canvas.measure()))
            path = outdir / f"{name}-{'dark' if dark else 'light'}.png"
            render_png(canvas, path, dark)
            written.append(path)
    # Окна целиком (без показа): то же, что делают ask_kind / show_ready / ask_first_run
    for build in (lambda ans: build_kind(KindInfo("06.10.2026", "18:05–20:40 · 1.5 ч · 12 клипов"), ans),
                  lambda ans: build_ready(ReadyInfo("Тренировка 06.10.2026", "",
                                                    Path("video.mp4"), "4.2 ГБ"), ans,
                                          {"copy": lambda t: None, "reveal": lambda p: None}),
                  lambda ans: build_first_run(20, 3, ans)):
        answers = []
        win = build(answers.append)
        canvas = win.contentView()
        canvas.displayIfNeeded()
        for key in [k for _, k in canvas.hits]:  # нажать каждую кнопку
            canvas.on_click(key)
        canvas.on_key("return")
        canvas.on_key("escape")
        win.close()
        print(f"окно {canvas.frame().size.width:.0f}×{canvas.frame().size.height:.0f}: ответы {answers}")
    return written


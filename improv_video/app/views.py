"""Нарисованные части меню по дизайн-системе improv-video: блок статуса, строки «Ролики», значок.

Обычные пункты меню (текст, галочка, ▸) — системные; здесь только то, что в дизайне своё.
Размеры и цвета — из макета «Меню improv-video» и tokens/colors.css дизайн-системы.
"""

from __future__ import annotations

from pathlib import Path

import objc
from AppKit import (
    NSAppearanceNameAqua,
    NSAppearanceNameDarkAqua,
    NSAttributedString,
    NSBezierPath,
    NSBitmapImageRep,
    NSColor,
    NSCompositingOperationSourceAtop,
    NSCompositingOperationSourceOver,
    NSFont,
    NSFontAttributeName,
    NSFontWeightMedium,
    NSFontWeightRegular,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSImage,
    NSLineBreakByWordWrapping,
    NSMakeRect,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
    NSRectFillUsingOperation,
    NSStringDrawingUsesLineFragmentOrigin,
    NSView,
)

from ..tools import resources_dir
from . import menu_model as mm

ICONS = resources_dir() / "icons"
MENU_PAD = 5  # отступ содержимого меню macOS от края
STATUS_WIDTH = 308
VIDEOS_WIDTH = 340


# ---------- цвета (tokens/colors.css) ----------

def _rgba(hex_or_rgba):
    if isinstance(hex_or_rgba, tuple):
        r, g, b, a = hex_or_rgba
        return NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255, g / 255, b / 255, a)
    h = hex_or_rgba.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255, g / 255, b / 255, 1.0)


LIGHT = {
    "text-primary": "#1D1D1F", "text-secondary": "#6B6A67", "text-tertiary": "#9C9A96",
    "text-danger": "#D6453D", "text-success": "#2E9D5B",
    "accent": "#2F6FEB", "bg-field": (0, 0, 0, 0.04), "bg-row-hover": (0, 0, 0, 0.035),
    "border-subtle": (0, 0, 0, 0.07), "progress-track": (0, 0, 0, 0.07),
    "status-idle": "#A3A19C", "status-active": "#2F6FEB", "status-success": "#2E9D5B",
    "status-warning": "#C98A12", "status-danger": "#D6453D",
}
DARK = {
    "text-primary": "#F2F2F3", "text-secondary": "#A3A3A8", "text-tertiary": "#6E6E73",
    "text-danger": "#F06A5F", "text-success": "#46BE76",
    "accent": "#5A93FF", "bg-field": (255, 255, 255, 0.06), "bg-row-hover": (255, 255, 255, 0.045),
    "border-subtle": (255, 255, 255, 0.06), "progress-track": (255, 255, 255, 0.08),
    "status-idle": "#6E6E73", "status-active": "#5A93FF", "status-success": "#46BE76",
    "status-warning": "#C98A12", "status-danger": "#F06A5F",
}
_color_cache: dict[tuple[bool, str], object] = {}


def color(name: str, dark: bool):
    key = (dark, name)
    if key not in _color_cache:
        _color_cache[key] = _rgba((DARK if dark else LIGHT)[name])
    return _color_cache[key]


def tone_color(tone: str, dark: bool):
    return color("status-" + tone, dark)


def is_dark(view) -> bool:
    match = view.effectiveAppearance().bestMatchFromAppearancesWithNames_(
        [NSAppearanceNameAqua, NSAppearanceNameDarkAqua])
    return match == NSAppearanceNameDarkAqua


# ---------- шрифты и текст ----------

_WEIGHTS = {400: NSFontWeightRegular, 500: NSFontWeightMedium, 600: NSFontWeightSemibold}


def font(size: float, weight: int = 400, digits: bool = False):
    w = _WEIGHTS[weight]
    if digits:
        return NSFont.monospacedDigitSystemFontOfSize_weight_(size, w)
    return NSFont.systemFontOfSize_weight_(size, w)


def _attr(text: str, fnt, col, wrap: bool = False):
    attrs = {NSFontAttributeName: fnt, NSForegroundColorAttributeName: col}
    if wrap:
        para = NSMutableParagraphStyle.alloc().init()
        para.setLineBreakMode_(NSLineBreakByWordWrapping)
        para.setLineHeightMultiple_(1.08)
        attrs[NSParagraphStyleAttributeName] = para
    return NSAttributedString.alloc().initWithString_attributes_(text, attrs)


def text_width(text: str, fnt) -> float:
    return _attr(text, fnt, NSColor.blackColor()).size().width


def draw_line(text: str, x: float, y: float, line: float, fnt, col, *, right: float | None = None,
              draw: bool = True) -> float:
    """Одна строка в полосе высотой line (по центру). right — выровнять по правому краю. Возвращает ширину."""
    s = _attr(text, fnt, col)
    size = s.size()
    if draw:
        left = right - size.width if right is not None else x
        s.drawAtPoint_((left, y + (line - size.height) / 2))
    return size.width


def draw_wrapped(text: str, x: float, y: float, width: float, fnt, col, draw: bool = True) -> float:
    """Текст с переносами; возвращает высоту."""
    s = _attr(text, fnt, col, wrap=True)
    rect = s.boundingRectWithSize_options_((width, 10_000), NSStringDrawingUsesLineFragmentOrigin)
    h = rect.size.height
    if draw:
        s.drawWithRect_options_(NSMakeRect(x, y, width, h + 2), NSStringDrawingUsesLineFragmentOrigin)
    return h


# ---------- примитивы дизайн-системы ----------

def fill_round(x, y, w, h, r, col) -> None:
    col.set()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(x, y, w, h), r, r).fill()


def status_dot(cx, cy, tone: str, dark: bool, size: float = 6) -> None:
    fill_round(cx - size / 2, cy - size / 2, size, size, size / 2, tone_color(tone, dark))


def progress_bar(x, y, w, h, fraction: float, dark: bool, tone: str = "active") -> None:
    fill_round(x, y, w, h, h / 2, color("progress-track", dark))
    f = max(0.0, min(1.0, fraction))
    if f > 0:
        fill = color("accent", dark) if tone == "active" else tone_color(tone, dark)
        fill_round(x, y, max(h, w * f), h, h / 2, fill)


_icon_cache: dict[tuple, object] = {}


def icon(name: str, pt: float, col):
    """Иконка Lucide (шаблон из resources/icons), закрашенная цветом."""
    key = (name, pt, col.description())
    if key in _icon_cache:
        return _icon_cache[key]
    src = NSImage.alloc().initWithContentsOfFile_(str(ICONS / f"{name}.png"))
    out = NSImage.alloc().initWithSize_((pt, pt))
    out.lockFocus()
    rect = NSMakeRect(0, 0, pt, pt)
    src.drawInRect_fromRect_operation_fraction_(rect, NSMakeRect(0, 0, 0, 0), NSCompositingOperationSourceOver, 1.0)
    col.set()
    NSRectFillUsingOperation(rect, NSCompositingOperationSourceAtop)
    out.unlockFocus()
    _icon_cache[key] = out
    return out


def draw_icon(name: str, x, y, pt, col) -> None:
    icon(name, pt, col).drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
        NSMakeRect(x, y, pt, pt), NSMakeRect(0, 0, 0, 0), NSCompositingOperationSourceOver, 1.0, True, None)


def menubar_image(state: str):
    """Значок строки меню 18×18 pt: idle/work — плёнка, waiting — с точкой, error — со значком «!»."""
    name = {"waiting": "menubar-dot", "error": "menubar-alert"}.get(state, "menubar")
    img = NSImage.alloc().initWithContentsOfFile_(str(ICONS / f"{name}.png"))
    img.setSize_((18, 18))
    img.setTemplate_(True)
    return img


# ---------- блок статуса (верх меню) ----------

class StatusView(NSView):
    """Покой / ждёт человека / «Новых клипов нет» / прогресс сборки — макеты 01–03."""

    def isFlipped(self):
        return True

    @objc.python_method
    def update(self, model) -> bool:
        """Обновить; True, если изменилась высота (меню надо перестроить)."""
        self.model = model
        h = self.measure(False)
        changed = abs(h - self.frame().size.height) > 0.5
        if changed:
            self.setFrameSize_((STATUS_WIDTH, h))
        self.setNeedsDisplay_(True)
        return changed

    def drawRect_(self, rect):
        self.measure(True)

    @objc.python_method
    def measure(self, draw: bool) -> float:
        """Раскладка; draw=False — только высота."""
        dark = is_dark(self) if draw else False
        m = self.model
        if m.progress is not None:
            return self._progress(m.progress, dark, draw)
        x0, y = MENU_PAD + 9, 8
        if m.icon:
            if draw:
                draw_icon(m.icon, x0, y + 1.5, 13, color("text-success", dark))
            tx = x0 + 13 + 8
        else:
            if draw:
                status_dot(x0 + 3, y + 8, m.tone, dark)
            tx = x0 + 6 + 8
        width = STATUS_WIDTH - tx - MENU_PAD - 9
        draw_line(m.title, tx, y, 16, font(13, 600), color("text-primary", dark), draw=draw)
        y += 16
        if m.subtitle:
            y += 3
            y += draw_wrapped(m.subtitle, tx, y, width, font(12), color("text-secondary", dark), draw)
        return y + 9

    @objc.python_method
    def _progress(self, p, dark: bool, draw: bool) -> float:
        x0, y = MENU_PAD + 9, 9
        w = STATUS_WIDTH - 2 * x0
        primary, secondary, tertiary = (color(n, dark) for n in ("text-primary", "text-secondary", "text-tertiary"))
        # Заголовок: «Тренировка 06.10.2026 · день 1 из 2»
        tw = draw_line(p.title, x0, y, 16, font(13, 600), primary, draw=draw)
        if p.day_note:
            draw_line(" " + p.day_note, x0 + tw, y, 16, font(13), secondary, draw=draw)
        y += 16 + 7
        if draw:
            progress_bar(x0, y, w, 4, p.fraction, dark, "idle" if p.stalled else "active")
        y += 4 + 7
        draw_line(p.percent, x0, y, 14, font(12, 500, digits=True), primary, draw=draw)
        if p.right:
            draw_line(p.right, 0, y, 14, font(12, digits=True), secondary, right=x0 + w, draw=draw)
        y += 14
        if p.stalled:
            y += 10
            inner = w - 18 - 13 - 8
            sub_h = draw_wrapped("Карта отошла или читается медленно. Клипы не потеряются", 0, 0, inner,
                                 font(12), secondary, draw=False)
            box_h = 8 + 16 + 2 + sub_h + 8
            if draw:
                fill_round(x0, y, w, box_h, 8, color("bg-field", dark))
                draw_icon("circle-alert", x0 + 9, y + 8 + 1.5, 13, tone_color("warning", dark))
                draw_line("Нет прогресса — проверьте флешку", x0 + 9 + 13 + 8, y + 8, 16, font(12, 600), primary)
                draw_wrapped("Карта отошла или читается медленно. Клипы не потеряются",
                             x0 + 9 + 13 + 8, y + 8 + 16 + 2, inner, font(12), secondary)
            y += box_h
        y += 10
        for i, s in enumerate(p.stages):
            if i:
                y += 6
            if draw:
                self._stage(s, x0, y, w, dark)
            y += 16
        if p.reading_card:
            y += 10
            if draw:
                color("border-subtle", dark).set()
                NSBezierPath.fillRect_(NSMakeRect(x0, y, w, 0.5))
            y += 8
            if draw:
                status_dot(x0 + 6, y + 8, "warning", dark)
                draw_line("Не вынимайте флешку", x0 + 20, y, 16, font(12, 500), primary)
                draw_line("идёт чтение", 0, y, 16, font(12), tertiary, right=x0 + w)
            y += 16
        if p.queued:  # вторая флешка вставлена во время сборки — её очередь после этой
            y += 6 if p.reading_card else 10
            if draw:
                status_dot(x0 + 6, y + 8, "idle", dark)
                draw_line(f"В очереди: {p.queued}", x0 + 20, y, 16, font(12, 500), primary)
                draw_line("после этой", 0, y, 16, font(12), tertiary, right=x0 + w)
            y += 16
        return y + 10

    @objc.python_method
    def _stage(self, s, x, y, w, dark) -> None:
        primary, secondary, tertiary = (color(n, dark) for n in ("text-primary", "text-secondary", "text-tertiary"))
        tx = x + 12 + 8
        if s.state == "done":
            draw_icon("check", x, y + 2, 12, tertiary)
            draw_line(s.title, tx, y, 16, font(12), secondary)
            draw_line(s.right, 0, y, 16, font(12, digits=True), tertiary, right=x + w)
        elif s.state == "active":
            if s.stalled:
                draw_icon("clock", x, y + 2, 12, tone_color("warning", dark))
            else:
                draw_icon("loader", x, y + 2, 12, color("accent", dark))
            draw_line(s.title, tx, y, 16, font(12, 500), primary)
            pct_w = 28
            bar_x = x + w - pct_w - 8 - 72
            progress_bar(bar_x, y + 6.5, 72, 3, s.fraction, dark, "idle" if s.stalled else "active")
            draw_line(s.right, 0, y, 16, font(12, digits=True), secondary, right=x + w)
        else:
            status_dot(x + 6, y + 8, "idle", dark, size=4)
            draw_line(s.title, tx, y, 16, font(12), tertiary)


# ---------- строки «Ролики» ----------

class VideoRowView(NSView):
    """Строка подменю «Ролики» (макет 04): точка, название, статус, действие справа."""

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def mouseUp_(self, event):
        item = self.enclosingMenuItem()
        if item is not None and item.menu() is not None:
            item.menu().cancelTracking()
        if self.handler and self.row.action:
            self.handler(self.row)

    def drawRect_(self, rect):
        dark = is_dark(self)
        r = self.row
        x, w = MENU_PAD, VIDEOS_WIDTH - 2 * MENU_PAD
        item = self.enclosingMenuItem()
        if item is not None and item.isHighlighted() and r.action:
            fill_round(x, 0, w, self.frame().size.height, 6, color("bg-row-hover", dark))
        cx = x + 8
        status_dot(cx + 6, 6 + 8, r.tone, dark)
        tx = cx + 12 + 8
        right = x + w - 8
        acc_w = 0.0
        if r.accessory or r.accessory_icon:
            acc_col = color("text-primary" if r.accessory_strong else
                            ("text-tertiary" if r.action == "open_log" else "text-secondary"), dark)
            ax = right
            if r.accessory_icon:
                size = 12 if r.accessory_icon == "external-link" else 11
                ax -= size
                draw_icon(r.accessory_icon, ax, 6 + (16 - size) / 2, size, acc_col)
                acc_w += size
                if r.accessory:
                    ax -= 4
                    acc_w += 4
            if r.accessory:
                acc_w += draw_line(r.accessory, 0, 6, 16, font(12, 500 if r.accessory_strong else 400), acc_col,
                                   right=ax)
        title_w = right - tx - (acc_w + 8 if acc_w else 0)
        draw_line(_fit(r.title, font(13, 500), title_w), tx, 6, 16, font(13, 500), color("text-primary", dark))
        detail_col = color("text-danger" if r.detail_danger else "text-secondary", dark)
        if r.fraction is not None:
            progress_bar(tx, 6 + 16 + 5 + 6, 96, 3, r.fraction, dark)
            draw_line(r.detail, tx + 96 + 8, 6 + 16 + 5, 15, font(12, digits=True), detail_col)
        else:
            draw_line(_fit(r.detail, font(12), right - tx), tx, 6 + 16 + 2, 15, font(12), detail_col)


def _fit(text: str, fnt, width: float) -> str:
    if text_width(text, fnt) <= width:
        return text
    while len(text) > 1 and text_width(text + "…", fnt) > width:
        text = text[:-1]
    return text.rstrip() + "…"


class NoteView(NSView):
    """Текст без действия: «Роликов пока нет», подпись под списком."""

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        self.measure(True)

    @objc.python_method
    def measure(self, draw: bool) -> float:
        dark = is_dark(self) if draw else False
        w = self.frame().size.width
        left, top, bottom = self.inset
        x = MENU_PAD + left
        width = w - x - MENU_PAD - 8
        y = top
        if self.title_:
            y += draw_wrapped(self.title_, x, y, width, font(13, 500), color("text-secondary", dark), draw)
            y += 4
            y += draw_wrapped(self.text_, x, y, width, font(12), color("text-tertiary", dark), draw)
        else:
            y += draw_wrapped(self.text_, x, y, width, font(11), color("text-tertiary", dark), draw)
        return y + bottom


class AccountView(NSView):
    """Шапка подменю YouTube (макет 05): плитка со значком, вошли / не вошли."""


    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        dark = is_dark(self)
        x, y = MENU_PAD + 9, 7
        if self.signed_in:
            fill_round(x, y, 28, 28, 8, _rgba("#2C2C2F" if dark else "#FFFFFF"))
            color("border-subtle", dark).set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(x + 0.25, y + 0.25, 27.5, 27.5),
                                                                    8, 8).stroke()
            draw_icon("youtube", x + 6, y + 6, 16, color("text-primary", dark))
        else:
            fill_round(x, y, 28, 28, 8, color("bg-field", dark))
            draw_icon("youtube", x + 6, y + 6, 16, color("text-tertiary", dark))
        tx = x + 28 + 10
        if self.signed_in:
            draw_line(self.name or "YouTube", tx, y, 15, font(13, 600), color("text-primary", dark))
            status_dot(tx + 3, y + 15 + 3 + 7, "success", dark)
            draw_line("Вход сохранён", tx + 11, y + 15 + 3, 14, font(12), color("text-secondary", dark))
        else:
            draw_line("Не вошли", tx, y, 15, font(13, 600), color("text-primary", dark))
            draw_line("Нужно для автоматической загрузки", tx, y + 15 + 3, 14, font(12),
                      color("text-secondary", dark))


# ---------- фабрики (свои init… у NSView в PyObjC неудобны) ----------

def status_view(model) -> StatusView:
    view = StatusView.alloc().initWithFrame_(NSMakeRect(0, 0, STATUS_WIDTH, 40))
    view.model = model
    view.setFrameSize_((STATUS_WIDTH, view.measure(False)))
    return view


def video_row_view(row, handler) -> VideoRowView:
    h = 6 + 16 + (5 + 15 if row.fraction is not None else 2 + 15) + 6
    view = VideoRowView.alloc().initWithFrame_(NSMakeRect(0, 0, VIDEOS_WIDTH, h))
    view.row, view.handler = row, handler
    return view


def note_view(title: str | None, text: str, width: float, inset: tuple[float, float, float]) -> NoteView:
    view = NoteView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 30))
    view.title_, view.text_, view.inset = title, text, inset
    view.setFrameSize_((width, view.measure(False)))
    return view


def account_view(signed_in: bool, name: str = "") -> AccountView:
    view = AccountView.alloc().initWithFrame_(NSMakeRect(0, 0, 280, 7 + 28 + 8))
    view.signed_in, view.name = signed_in, name
    return view


# ---------- для самопроверки в сборке ----------

def render_png(view, path: Path, dark: bool = False) -> None:
    """Рисует view в PNG @2x с прозрачным фоном — CI выкладывает картинки, чтобы проверить вид без Mac."""
    from AppKit import NSAppearance

    view.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameDarkAqua if dark else NSAppearanceNameAqua))
    size = view.bounds().size
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(  # noqa: E501
        None, int(size.width * 2), int(size.height * 2), 8, 4, True, False, "NSDeviceRGBColorSpace", 0, 0)
    rep.setSize_(size)
    view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
    data = rep.representationUsingType_properties_(4, {})  # 4 = NSBitmapImageFileTypePNG
    data.writeToFile_atomically_(str(path), True)


def demo_models():
    """Состояния из макета — для самопроверки и картинок в CI."""
    from datetime import date

    from ..pipeline import VideoItem

    stages = [mm.StageRow("Замер и автоцвет", "done", 1.0, "3 мин"), mm.StageRow("Кодирование", "active", 0.61, "61%"),
              mm.StageRow("Звук", "pending"), mm.StageRow("Склейка", "pending")]
    work = mm.ProgressBlock("Тренировка 06.10.2026", "· день 1 из 2", 0.42, "42%", "осталось ~18 мин", False,
                            stages, reading_card=True, queued="флешка «HotBaby»")
    stalled_stages = [mm.StageRow(s.title, s.state, s.fraction, s.right, s.state == "active") for s in stages]
    stalled = mm.ProgressBlock("Тренировка 06.10.2026", "· день 1 из 2", 0.42, "42%", "без изменений 3 мин", True,
                               stalled_stages, reading_card=True)
    statuses = {
        "idle": mm.StatusBlock(title="Жду флешку", subtitle="Вставьте карту камеры — ролик дня соберётся сам"),
        "waiting": mm.StatusBlock(tone=mm.WARNING, title="2 ролика ждут вас", subtitle="Выбрать тип · загрузить вручную"),
        "note": mm.StatusBlock(tone=mm.SUCCESS, icon="circle-check", title="Новых клипов нет",
                               subtitle="Всё на карте уже обработано. Можно извлечь флешку"),
        "work": mm.StatusBlock(tone=mm.ACTIVE, progress=work),
        "stalled": mm.StatusBlock(tone=mm.ACTIVE, progress=stalled),
    }
    d = date(2026, 10, 7)
    items = [
        VideoItem("v5", "07.10.2026", "kind_needed", "Ждёт выбора типа · 18:05–20:40", d, 5),
        VideoItem("v4", "Тренировка 06.10.2026 (часть 2)", "uploading", "Загружается · 40%", d, 4, fraction=0.4),
        VideoItem("v3", "Тренировка 06.10.2026", "manual", "Загрузить вручную", d, 3),
        VideoItem("v2", "Занятие 05.10.2026", "uploaded", "На YouTube · по ссылке", d, 2, url="https://youtu.be/x"),
        VideoItem("d2026-10-04", "04.10.2026", "failed", "Не собрался: флешку вынули", d),
    ]
    return statuses, [mm.video_row(v) for v in items]


def render_demo(outdir: Path) -> list[Path]:
    """Картинки всех состояний из макета, светлая и тёмная тема."""
    outdir.mkdir(parents=True, exist_ok=True)
    statuses, rows = demo_models()
    written = []
    for dark in (False, True):
        theme = "dark" if dark else "light"
        for name, model in statuses.items():
            path = outdir / f"status-{name}-{theme}.png"
            render_png(status_view(model), path, dark)
            written.append(path)
        for row in rows:
            path = outdir / f"row-{row.item.status}-{theme}.png"
            render_png(video_row_view(row, None), path, dark)
            written.append(path)
        for signed in (True, False):
            path = outdir / f"account-{'in' if signed else 'out'}-{theme}.png"
            render_png(account_view(signed, "Иван improv"), path, dark)
            written.append(path)
        note = note_view(None, "Последние 10 роликов. Несобранный день соберётся при следующей вставке флешки",
                         VIDEOS_WIDTH, (28, 3, 4))
        path = outdir / f"row-footer-{theme}.png"
        render_png(note, path, dark)
        written.append(path)
    return written

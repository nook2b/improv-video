"""Что показывает меню — без AppKit, чтобы проверять тестами. Рисует improv_video.app.views."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..pipeline import VideoItem
from ..progress import DayProgress, StageView, minutes

# Тон точки статуса (StatusDot в дизайн-системе)
IDLE, ACTIVE, SUCCESS, WARNING, DANGER = "idle", "active", "success", "warning", "danger"
WAITING = ("kind_needed", "manual")  # ролики, которые ждут человека


@dataclass
class StageRow:
    title: str
    state: str  # done | active | pending
    fraction: float = 0.0
    right: str = ""  # «3 мин» для готового этапа
    stalled: bool = False


@dataclass
class ProgressBlock:
    title: str  # «Тренировка 06.10.2026»
    day_note: str  # «· день 1 из 2» или ""
    fraction: float
    percent: str  # «42%»
    right: str  # «осталось ~18 мин» / «без изменений 3 мин» / ""
    stalled: bool
    stages: list[StageRow] = field(default_factory=list)
    reading_card: bool = False


@dataclass
class StatusBlock:
    """Верх меню. Либо строка с точкой/значком (покой, ждёт человека, заметка), либо прогресс."""

    tone: str = IDLE
    icon: str | None = None  # circle-check вместо точки
    title: str = ""
    subtitle: str = ""
    progress: ProgressBlock | None = None


@dataclass
class VideoRow:
    key: str
    tone: str
    title: str
    detail: str
    detail_danger: bool = False
    accessory: str = ""  # «Выбрать…», «В Studio», «при вставке»
    accessory_icon: str | None = None  # arrow-right, external-link
    accessory_strong: bool = False
    fraction: float | None = None  # полоса загрузки
    action: str | None = None  # kind | hand_off | open_url | open_log
    item: VideoItem | None = None


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def progress_block(p: DayProgress) -> ProgressBlock:
    stalled = p.stalled_for()
    eta = p.eta_seconds()
    if stalled:
        right = f"без изменений {minutes(stalled)}"
    elif eta is not None:
        right = f"осталось ~{minutes(eta)}"
    else:
        right = ""
    rows = []
    for s in p.stages():
        rows.append(_stage_row(s, bool(stalled)))
    overall = p.overall()
    return ProgressBlock(
        title=p.label,
        day_note=f"· день {p.index} из {p.total}" if p.total > 1 else "",
        fraction=overall,
        percent=f"{overall:.0%}",
        right=right,
        stalled=bool(stalled),
        stages=rows,
        reading_card=p.reading_card,
    )


def _stage_row(s: StageView, stalled: bool) -> StageRow:
    if s.state == "done":
        return StageRow(s.title, "done", 1.0, minutes(s.seconds))
    if s.state == "active":
        return StageRow(s.title, "active", s.fraction, f"{s.fraction:.0%}", stalled)
    return StageRow(s.title, "pending")


@dataclass
class UpdateState:
    """Обновление приложения — для блока вверху меню и строки версии в «Приложение»."""

    stage: str = "idle"  # idle | checking | downloading | ready | latest | error
    version: str = ""  # найденная новая версия
    checked_at: datetime | None = None
    updated_from: str = ""  # с какой версии обновились при этом запуске
    updated_at: datetime | None = None


UPDATED_NOTICE = timedelta(hours=12)  # столько после обновления вверху меню видно «Обновлено до …»


def update_line(current: str, u: UpdateState) -> str:
    """Строка версии: «Версия 0.1.18 · последняя, проверено в 14:05»."""
    at = f"{u.checked_at:%H:%M}" if u.checked_at else ""
    tail = {
        "checking": "проверяю обновления…",
        "downloading": f"скачиваю {u.version}…",
        "ready": f"{u.version} поставится после обработки",
        "latest": f"последняя, проверено в {at}",
        "error": f"не удалось проверить в {at}",
    }.get(u.stage, "")
    return f"Версия {current}" + (f" · {tail}" if tail else "")


def update_notice(current: str, u: UpdateState, now: datetime) -> tuple[StatusBlock, bool] | None:
    """Блок вверху меню про обновление и важнее ли он роликов, ждущих человека."""
    if u.stage == "downloading":
        return StatusBlock(tone=ACTIVE, title=f"Скачиваю обновление {u.version}",
                           subtitle="Потом приложение перезапустится само"), True
    if u.stage == "ready":
        return StatusBlock(tone=ACTIVE, title=f"Обновление {u.version} скачано",
                           subtitle="Поставится, когда закончится обработка"), True
    if u.updated_from and u.updated_at and now - u.updated_at < UPDATED_NOTICE:
        return StatusBlock(tone=SUCCESS, icon="circle-check", title=f"Обновлено до {current}",
                           subtitle=f"Было {u.updated_from}"), False
    return None


def status_block(*, progress: DayProgress | None, status: str, busy: bool, note: tuple[str, str] | None,
                 videos: list[VideoItem], update: tuple[StatusBlock, bool] | None = None) -> StatusBlock:
    if progress is not None:
        return StatusBlock(tone=ACTIVE, progress=progress_block(progress))
    if busy:
        return StatusBlock(tone=ACTIVE, title=status)
    if note:
        return StatusBlock(tone=SUCCESS, icon="circle-check", title=note[0], subtitle=note[1])
    if update and update[1]:
        return update[0]
    waiting = [v for v in videos if v.status in WAITING]
    if waiting:
        n = len(waiting)
        what = []
        if any(v.status == "kind_needed" for v in waiting):
            what.append("выбрать тип")
        if any(v.status == "manual" for v in waiting):
            what.append("загрузить вручную")
        text = " · ".join(what)
        return StatusBlock(tone=WARNING, title=f"{n} {_plural(n, 'ролик ждёт', 'ролика ждут', 'роликов ждут')} вас",
                           subtitle=text[:1].upper() + text[1:])
    if update:
        return update[0]
    last = next((v for v in videos if v.status in ("uploaded", "handed")), None)
    if last:
        where = "на YouTube" if last.status == "uploaded" else "передан в Studio"
        return StatusBlock(title="Жду флешку", subtitle=f"Последний: {last.title} · {where}")
    return StatusBlock(title="Жду флешку", subtitle="Вставьте карту камеры — ролик дня соберётся сам")


def video_row(v: VideoItem) -> VideoRow:
    s = v.status
    if s == "kind_needed":
        return VideoRow(v.key, WARNING, v.title, v.detail, accessory="Выбрать…", accessory_strong=True,
                        action="kind", item=v)
    if s == "manual":
        return VideoRow(v.key, WARNING, v.title, v.detail, accessory="В Studio", accessory_icon="arrow-right",
                        action="hand_off", item=v)
    if s == "handed":
        return VideoRow(v.key, SUCCESS, v.title, v.detail, action="hand_off", item=v)
    if s == "uploaded":
        return VideoRow(v.key, SUCCESS, v.title, v.detail, accessory_icon="external-link", action="open_url", item=v)
    if s in ("uploading", "building"):
        return VideoRow(v.key, ACTIVE, v.title, v.detail, fraction=v.fraction or 0.0, item=v)
    if s == "failed":
        return VideoRow(v.key, DANGER, v.title, v.detail, detail_danger=True, accessory="при вставке",
                        action="open_log", item=v)
    return VideoRow(v.key, IDLE, v.title, v.detail, item=v)  # queued


def waiting_count(videos: list[VideoItem]) -> int:
    return sum(v.status in WAITING for v in videos)


def icon_state(*, progress: DayProgress | None, busy: bool, videos: list[VideoItem]) -> tuple[str, str]:
    """Значок в строке меню: (idle | work | waiting | error, текст справа)."""
    if progress is not None:
        return "work", f"{progress.overall():.0%}"
    if busy:
        return "work", ""
    if any(v.status == "failed" for v in videos):
        return "error", ""
    if waiting_count(videos):
        return "waiting", ""
    return "idle", ""


def youtube_value(signed_in: bool, upload_mode: str) -> str:
    if not signed_in:
        return "не вошли"
    return "автоматически" if upload_mode == "api" else "вручную"


def quality_value(max_height: int) -> str:
    return "4K" if max_height >= 2160 else "1080p"


DENOISE = {"off": ("Выключено", ""), "weak": ("Слабое", ""), "medium": ("Среднее", "голос"),
           "strong": ("Сильное", "DeepFilterNet")}


def short_path(path: str, home: str) -> str:
    return "~" + path[len(home):] if home and path.startswith(home) else path


def ellipsize_middle(text: str, limit: int = 28) -> str:
    if len(text) <= limit:
        return text
    head = (limit - 1) // 2
    return text[:head] + "…" + text[-(limit - 1 - head):]

"""Командная строка ядра (до появления интерфейса).

python -m improv_video import /Volumes/CARD --archive ~/Footage
python -m improv_video build ~/Footage/2026-10-01 --kind training --lut ilog.cube
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .naming import KINDS
from .pipeline import Settings, build_day, build_pending, import_card, mark_existing_as_done
from .scan import list_clips
from .state import State


def _settings(a: argparse.Namespace) -> Settings:
    return Settings(
        archive=Path(a.archive).expanduser(),
        lut=Path(a.lut) if a.lut else None,
        profile=a.profile,
        denoise=a.denoise,
        rnnoise_model=Path(a.rnnoise) if a.rnnoise else None,
        auto_brightness=not a.no_brightness,
        max_height=a.max_height,
        x265_preset=a.x265_preset,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="improv_video")
    p.add_argument("--archive", default="~/Footage", help="папка архива")
    p.add_argument("--lut", help="LUT-файл .cube")
    p.add_argument("--profile", choices=["ilog", "normal"], default="ilog")
    p.add_argument("--denoise", choices=["off", "weak", "medium", "strong"], default="weak")
    p.add_argument("--rnnoise", help="модель RNNoise .rnnn (для --denoise medium)")
    p.add_argument("--no-brightness", action="store_true", help="без автояркости")
    p.add_argument("--max-height", type=int, default=2160)
    p.add_argument("--x265-preset", default="medium")
    sub = p.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import", help="скопировать новые клипы с карты и собрать ролики")
    imp.add_argument("volume")
    imp.add_argument("--kind", choices=list(KINDS), required=True, help="тип для всех новых дней")
    imp.add_argument("--skip-existing", action="store_true",
                     help="первый запуск: пометить всё на карте как уже обработанное")

    b = sub.add_parser("build", help="собрать ролик из папки дня (вручную)")
    b.add_argument("folder")
    b.add_argument("--kind", choices=list(KINDS), required=True)
    b.add_argument("--part", type=int, default=1)
    b.add_argument("-o", "--out")

    a = p.parse_args(argv)
    settings = _settings(a)

    if a.cmd == "build":
        folder = Path(a.folder).expanduser()
        clips = list_clips(folder)
        day = date.fromisoformat(folder.name)
        out = Path(a.out) if a.out else folder / "video.mp4"
        r = build_day(day, [c.path for c in clips], a.kind, a.part, settings, out)
        print(f"\n{r.title}\n{r.description}\n{r.file} · {r.duration / 60:.1f} мин · {r.encoder}")
        return 0

    state = State(settings.archive / "state.sqlite")
    if a.skip_existing:
        n = mark_existing_as_done(Path(a.volume), state, settings)
        print(f"Помечено как обработанные: {n} клипов")
        return 0
    days = import_card(Path(a.volume), state, settings)
    for day in days:
        r = build_pending(day, a.kind, state, settings)
        if r:
            print(f"\n{r.title}\n{r.description}\n{r.file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Настройки приложения: ~/Library/Application Support/improv-video/config.json."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ..pipeline import Settings
from ..tools import resources_dir

_SAVE_LOCK = threading.Lock()
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "improv-video"
LOG_FILE = Path.home() / "Library" / "Logs" / "improv-video.log"


@dataclass
class AppConfig:
    archive: str = str(Path.home() / "Movies" / "improv-video")
    lut: str = ""
    client_secrets: str = ""
    denoise: str = "medium"  # off | weak | medium | strong
    auto_brightness: bool = True
    copy_clips: bool = False  # мало места на Mac: клипы читаются прямо с флешки
    max_height: int = 1080  # 1080 или 2160
    # manual — до аудита API: файл готовится, загрузка в YouTube Studio руками;
    # api — после аудита: загрузка «по ссылке» сама.
    upload_mode: str = "manual"
    last_kind: str = "training"
    playlists: list = field(default_factory=list)  # плейлисты канала [[id, название]] — с прошлой проверки
    kind_playlists: dict = field(default_factory=dict)  # тип → [id, название]: последний выбранный для типа
    channel_name: str = "Иван improv"  # подпись в подменю YouTube
    delete_after_days: int = 3  # файл ролика — в Корзину через столько дней после «Готово» или загрузки; 0 — не удалять
    auto_update: bool = True  # новые версии из релизов GitHub ставятся сами, когда ничего не обрабатывается
    last_version: str = ""  # с какой версией запускались в прошлый раз — для «Обновлено до …»

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        path = path or SUPPORT_DIR / "config.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path | None = None) -> None:
        path = path or SUPPORT_DIR / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with _SAVE_LOCK:  # сохраняют и меню, и окна в своих потоках — по одному
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)

    def settings(self) -> Settings:
        return Settings(
            archive=Path(self.archive).expanduser(),
            lut=Path(self.lut) if self.lut else None,
            profile="ilog" if self.lut else "normal",
            denoise=self.denoise,
            rnnoise_model=resources_dir() / "rnnoise" / "bd.rnnn",
            auto_brightness=self.auto_brightness,
            copy_clips=self.copy_clips,
            max_height=self.max_height,
        )

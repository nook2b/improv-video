"""Вход в YouTube и загрузка роликов через YouTube Data API v3."""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

PRIVACY_RU = {"unlisted": "Доступ по ссылке", "private": "Ограниченный доступ", "public": "Открытый доступ"}
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
MANAGE_SCOPE = "https://www.googleapis.com/auth/youtube"  # плейлисты и обложки
SCOPES = [UPLOAD_SCOPE, MANAGE_SCOPE]
CHUNK = 64 * 1024 * 1024
RETRIABLE_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 8


class NeedsLogin(RuntimeError):
    """Входа нет, или Google его отозвал — нужно «Войти в YouTube» заново."""


class QuotaExceeded(RuntimeError):
    """Дневная квота API или лимит загрузок канала — повторить завтра."""


class ThumbnailNotAllowed(RuntimeError):
    """Свои обложки YouTube разрешает только каналам с подтверждённым телефоном."""


@dataclass
class Playlist:
    id: str
    title: str


class TokenStore(Protocol):
    def load(self) -> str | None: ...
    def save(self, data: str) -> None: ...
    def clear(self) -> None: ...


class KeyringStore:
    """Токен в системной связке ключей (Keychain на Mac, диспетчер учётных данных на Windows)."""

    service, key = "improv-video", "youtube-token"

    def load(self) -> str | None:
        import keyring

        return keyring.get_password(self.service, self.key)

    def save(self, data: str) -> None:
        import keyring

        keyring.set_password(self.service, self.key, data)

    def clear(self) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(self.service, self.key)
        except PasswordDeleteError:
            pass


class FileTokenStore:
    """Токен в файле, читать и писать его может только ваша учётная запись (права 600).

    Не в связке ключей: приложение подписано без сертификата Apple, у каждой версии подпись своя,
    и macOS после каждого обновления спрашивала пароль от Mac, чтобы отдать токен. Вход, сохранённый
    раньше в связке ключей, переезжает сюда при первом чтении (macOS спросит пароль последний раз).
    """

    def __init__(self, path: Path, legacy: TokenStore | None = None):
        self.path, self.legacy = Path(path), legacy

    def load(self) -> str | None:
        if self.path.exists():
            return self.path.read_text(encoding="utf-8") or None
        if self.legacy is None:
            return None
        try:
            data = self.legacy.load()
        except Exception:  # noqa: BLE001 — связка ключей недоступна или пароль не ввели
            return None
        if data:
            self.save(data)
            try:
                self.legacy.clear()
            except Exception:  # noqa: BLE001
                pass
        return data

    def save(self, data: str) -> None:
        import os

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
        if self.legacy is not None:
            try:
                self.legacy.clear()
            except Exception:  # noqa: BLE001
                pass


def login(client_secrets: Path, store: TokenStore):
    """Открывает браузер для входа в Google и сохраняет токен."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline",
                                  success_message="Готово. Окно можно закрыть.")
    store.save(creds.to_json())
    return creds


def credentials(store: TokenStore):
    """Сохранённый вход; при необходимости обновляет токен. NeedsLogin, если входа нет."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    raw = store.load()
    if not raw:
        raise NeedsLogin("Войдите в YouTube")
    # Разрешения — те, что выдал Google при входе: при обновлении токена просить больше нельзя
    creds = Credentials.from_authorized_user_info(json.loads(raw))
    if not creds.valid:
        if not creds.refresh_token:
            raise NeedsLogin("Войдите в YouTube заново")
        try:
            creds.refresh(Request())
        except RefreshError as e:
            store.clear()
            raise NeedsLogin("Войдите в YouTube заново") from e
        store.save(creds.to_json())
    return creds


def can_manage(store: TokenStore) -> bool:
    """Вход есть и с доступом к плейлистам и обложкам (вход до версии 0.1.21 — только загрузка)."""
    raw = store.load()
    if not raw:
        return False
    scopes = json.loads(raw).get("scopes") or []
    return MANAGE_SCOPE in (scopes.split() if isinstance(scopes, str) else scopes)


def _service(creds):
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _http_error(e) -> tuple[int, str]:
    text = e.content.decode("utf-8", "replace") if isinstance(e.content, bytes) else str(e.content)
    return int(getattr(e.resp, "status", 0)), text


def _call(request):
    """Короткий запрос API: 401 → NeedsLogin, 403 по квоте → QuotaExceeded."""
    from googleapiclient.errors import HttpError

    try:
        return request.execute(num_retries=3)
    except HttpError as e:
        code, text = _http_error(e)
        if code == 401 or "insufficientPermissions" in text or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in text:
            raise NeedsLogin("Войдите в YouTube заново") from e
        if code == 403 and "quotaExceeded" in text:
            raise QuotaExceeded("Дневная квота YouTube API исчерпана — повторю завтра") from e
        raise


def list_playlists(creds=None, service=None) -> list[Playlist]:
    """Плейлисты своего канала по алфавиту."""
    service = service or _service(creds)
    out, token = [], None
    while True:
        resp = _call(service.playlists().list(part="snippet", mine=True, maxResults=50, pageToken=token))
        out += [Playlist(it["id"], it["snippet"]["title"]) for it in resp.get("items", [])]
        token = resp.get("nextPageToken")
        if not token:
            return sorted(out, key=lambda p: p.title.lower())


def create_playlist(title: str, creds=None, service=None, privacy: str = "unlisted") -> Playlist:
    """Новый плейлист; по умолчанию «по ссылке», как и ролики (видимость можно поменять в Studio)."""
    service = service or _service(creds)
    resp = _call(service.playlists().insert(part="snippet,status", body={
        "snippet": {"title": title[:150]}, "status": {"privacyStatus": privacy}}))
    return Playlist(resp["id"], resp["snippet"]["title"])


def add_to_playlist(playlist_id: str, video_id: str, creds=None, service=None) -> None:
    service = service or _service(creds)
    _call(service.playlistItems().insert(part="snippet", body={
        "snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}))


THUMB_MAX_BYTES = 2 * 1024 * 1024  # ограничение YouTube


def prepare_thumbnail(image: Path, out: Path) -> Path:
    """Картинка любого формата → JPEG до 1280 px по ширине и не больше 2 МБ."""
    import shutil
    import subprocess

    from .tools import ffmpeg

    if image.suffix.lower() in (".heic", ".heif") and shutil.which("sips"):  # фото с iPhone: ffmpeg их не всегда читает
        png = out.with_suffix(".png")
        subprocess.run(["sips", "-s", "format", "png", str(image), "--out", str(png)], check=True, capture_output=True)
        image = png
    for q in (2, 4, 7, 12):
        ffmpeg(["-i", str(image), "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-q:v", str(q), str(out)])
        if out.stat().st_size <= THUMB_MAX_BYTES:
            return out
    raise ValueError("Картинка слишком большая для обложки даже после сжатия")


def set_thumbnail(video_id: str, image: Path, creds=None, service=None) -> None:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    service = service or _service(creds)
    try:
        _call(service.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(image), mimetype="image/jpeg")))
    except HttpError as e:
        code, text = _http_error(e)
        if code == 403:
            raise ThumbnailNotAllowed("YouTube не принял обложку: свои обложки можно ставить, когда у канала "
                                      "подтверждён номер телефона (youtube.com/verify)") from e
        raise


@dataclass
class UploadResult:
    video_id: str
    privacy: str  # что YouTube реально поставил: до аудита API-проекта будет private

    @property
    def url(self) -> str:
        return f"https://youtu.be/{self.video_id}"


def request_body(title: str, description: str, recorded_at: datetime, privacy: str = "unlisted") -> dict:
    return {
        "snippet": {"title": title[:100], "description": description, "categoryId": "22"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        # Время камеры местное: добавляем часовой пояс компьютера.
        "recordingDetails": {"recordingDate": recorded_at.astimezone().isoformat()},
    }


def upload(
    file: Path,
    title: str,
    description: str,
    recorded_at: datetime,
    *,
    creds=None,
    service=None,
    privacy: str = "unlisted",
    progress: Callable[[float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> UploadResult:
    """Возобновляемая загрузка кусками по 64 МБ с повторами при сбоях сети и сервера."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    if service is None:
        from googleapiclient.discovery import build

        service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    media = MediaFileUpload(str(file), mimetype="video/mp4", chunksize=CHUNK, resumable=True)
    request = service.videos().insert(
        part="snippet,status,recordingDetails",
        body=request_body(title, description, recorded_at, privacy),
        media_body=media,
        notifySubscribers=False,
    )
    response, retries = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            retries = 0
            if status and progress:
                progress(status.progress())
        except HttpError as e:
            code = int(getattr(e.resp, "status", 0))
            text = (e.content or b"").decode("utf-8", "replace") if isinstance(e.content, bytes) else str(e.content)
            if code == 403 and ("quotaExceeded" in text or "uploadLimitExceeded" in text):
                raise QuotaExceeded("Лимит загрузок на сегодня исчерпан — видео уйдёт завтра") from e
            if code == 401:
                raise NeedsLogin("Войдите в YouTube заново") from e
            if code not in RETRIABLE_STATUS or retries >= MAX_RETRIES:
                raise
            retries += 1
            sleep(min(2**retries, 300) + random.random())
        except (ConnectionError, TimeoutError, OSError):
            if retries >= MAX_RETRIES:
                raise
            retries += 1
            sleep(min(2**retries, 300) + random.random())
    if progress:
        progress(1.0)
    return UploadResult(video_id=response["id"], privacy=response.get("status", {}).get("privacyStatus", privacy))

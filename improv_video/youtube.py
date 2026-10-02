"""Вход в YouTube и загрузка роликов через YouTube Data API v3."""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CHUNK = 64 * 1024 * 1024
RETRIABLE_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 8


class NeedsLogin(RuntimeError):
    """Входа нет, или Google его отозвал — нужно «Войти в YouTube» заново."""


class QuotaExceeded(RuntimeError):
    """Дневная квота API или лимит загрузок канала — повторить завтра."""


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
    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
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

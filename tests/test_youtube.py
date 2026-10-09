from datetime import datetime

import pytest
from googleapiclient.errors import HttpError

from improv_video import youtube


class Resp(dict):
    def __init__(self, status):
        super().__init__()
        self.status = status
        self.reason = "x"


class FakeRequest:
    def __init__(self, script):
        self.script = list(script)

    def next_chunk(self):
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class FakeService:
    def __init__(self, script):
        self.request = FakeRequest(script)
        self.kwargs = None

    def videos(self):
        return self

    def insert(self, **kwargs):
        self.kwargs = kwargs
        return self.request


def http_error(code, reason=""):
    return HttpError(Resp(code), f'{{"error": {{"errors": [{{"reason": "{reason}"}}]}}}}'.encode())


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "v.mp4"
    p.write_bytes(b"0" * 1024)
    return p


def test_body_and_retries(video):
    svc = FakeService([http_error(503), ConnectionError(), (None, {"id": "XYZ", "status": {"privacyStatus": "unlisted"}})])
    r = youtube.upload(video, "Тренировка 01.10.2026", "Снято 01.10.2026, 18:05–20:40",
                       datetime(2026, 10, 1, 18, 5), service=svc, sleep=lambda _: None)
    assert (r.video_id, r.privacy, r.url) == ("XYZ", "unlisted", "https://youtu.be/XYZ")
    body = svc.kwargs["body"]
    assert body["status"]["privacyStatus"] == "unlisted"
    assert body["snippet"]["title"] == "Тренировка 01.10.2026"
    assert body["recordingDetails"]["recordingDate"].startswith("2026-10-01T18:05:00")
    assert svc.kwargs["notifySubscribers"] is False


def test_quota(video):
    svc = FakeService([http_error(403, "quotaExceeded")])
    with pytest.raises(youtube.QuotaExceeded):
        youtube.upload(video, "t", "d", datetime(2026, 10, 1), service=svc, sleep=lambda _: None)


def test_needs_login_without_token():
    class Empty:
        def load(self):
            return None

    with pytest.raises(youtube.NeedsLogin):
        youtube.credentials(Empty())


def test_upload_is_unlisted_and_not_made_for_kids():
    from datetime import datetime

    from improv_video.youtube import request_body

    body = request_body("Шоу 08.10.2026", "Снято 08.10.2026, 18:05–20:40", datetime(2026, 10, 8, 18, 5))
    assert body["status"]["selfDeclaredMadeForKids"] is False  # «Нет, это видео не для детей»
    assert body["status"]["privacyStatus"] == "unlisted"
    assert body["snippet"]["title"] == "Шоу 08.10.2026"


class _Req:
    def __init__(self, resp):
        self.resp = resp

    def execute(self, num_retries=0):
        return self.resp


class _FakeService:
    """Ровно те вызовы API, что делает приложение для плейлистов."""

    def __init__(self):
        self.inserted = []

    def playlists(self):
        svc = self

        class P:
            def list(self, part, mine, maxResults, pageToken=None):
                pages = {None: ({"items": [{"id": "2", "snippet": {"title": "шоу"}}], "nextPageToken": "n"}),
                         "n": {"items": [{"id": "1", "snippet": {"title": "Команда А"}}]}}
                return _Req(pages[pageToken])

            def insert(self, part, body):
                svc.inserted.append(("playlist", body))
                return _Req({"id": "new", "snippet": body["snippet"]})
        return P()

    def playlistItems(self):
        svc = self

        class I:
            def insert(self, part, body):
                svc.inserted.append(("item", body))
                return _Req({})
        return I()


def test_playlists_api_calls():
    from improv_video import youtube

    svc = _FakeService()
    assert [p.title for p in youtube.list_playlists(service=svc)] == ["Команда А", "шоу"]  # все страницы, по алфавиту
    p = youtube.create_playlist("Мастер-классы", service=svc)
    assert (p.id, p.title) == ("new", "Мастер-классы")
    assert svc.inserted[0][1]["status"]["privacyStatus"] == "unlisted"
    youtube.add_to_playlist("new", "vid1", service=svc)
    assert svc.inserted[1][1]["snippet"] == {"playlistId": "new",
                                             "resourceId": {"kind": "youtube#video", "videoId": "vid1"}}


def test_can_manage_needs_new_scope():
    import json

    from improv_video import youtube

    class Store:
        def __init__(self, raw):
            self.raw = raw

        def load(self):
            return self.raw

    assert not youtube.can_manage(Store(None))
    assert not youtube.can_manage(Store(json.dumps({"scopes": [youtube.UPLOAD_SCOPE]})))  # вход до 0.1.21
    assert youtube.can_manage(Store(json.dumps({"scopes": youtube.SCOPES})))


def test_prepare_thumbnail_fits_youtube_limits(tmp_path):
    import subprocess

    from improv_video import youtube
    from improv_video.probe import probe

    src = tmp_path / "cover.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=3000x1688", "-frames:v", "1",
                    str(src)], check=True)
    out = youtube.prepare_thumbnail(src, tmp_path / "thumb.jpg")
    m = probe(out)
    assert m.width == 1280 and out.stat().st_size <= youtube.THUMB_MAX_BYTES


def test_file_token_store_moves_token_out_of_keychain(tmp_path):
    import stat

    from improv_video.youtube import FileTokenStore

    class Keychain:
        def __init__(self):
            self.data, self.reads = '{"token": "t"}', 0

        def load(self):
            self.reads += 1
            return self.data

        def save(self, data):
            self.data = data

        def clear(self):
            self.data = None

    keychain = Keychain()
    store = FileTokenStore(tmp_path / "support" / "youtube-token.json", keychain)
    assert store.load() == '{"token": "t"}'  # из связки ключей — последний раз
    assert keychain.data is None and keychain.reads == 1
    assert store.load() == '{"token": "t"}' and keychain.reads == 1  # дальше — только файл, без пароля
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    store.save('{"token": "u"}')
    assert store.load() == '{"token": "u"}' and stat.S_IMODE(store.path.stat().st_mode) == 0o600
    store.clear()
    assert store.load() is None and not store.path.exists()

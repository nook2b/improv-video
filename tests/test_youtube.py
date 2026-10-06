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

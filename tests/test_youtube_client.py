import json

import httplib2
import pytest
from googleapiclient.errors import HttpError

from network.providers import youtube
from network.providers.youtube import QuotaExceeded, YouTubeClient, YouTubeError


def _http_error(status, reason=""):
    resp = httplib2.Response({"status": status})
    body = {"error": {"code": status, "message": reason or "err",
                      "errors": [{"reason": reason, "message": reason}] if reason else []}}
    return HttpError(resp, json.dumps(body).encode())


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
        self.inserted = None

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        self.inserted = body
        return self.request


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "v.mp4"
    p.write_bytes(b"0" * 1000)
    return p


def test_retries_5xx_and_connection_errors(video):
    sleeps = []
    svc = FakeService([_http_error(503), ConnectionResetError("reset"), (None, {"id": "abc"})])
    client = YouTubeClient(service=svc, sleep=sleeps.append)
    assert client.upload(video, {"snippet": {}})["id"] == "abc"
    assert len(sleeps) == 2 and all(s <= 60 for s in sleeps)


@pytest.mark.parametrize("reason", ["quotaExceeded", "uploadLimitExceeded"])
def test_quota_reasons(video, reason):
    client = YouTubeClient(service=FakeService([_http_error(403, reason)]), sleep=lambda s: None)
    with pytest.raises(QuotaExceeded):
        client.upload(video, {})


def test_client_errors_are_not_retried(video):
    client = YouTubeClient(service=FakeService([_http_error(400, "invalidTitle")]), sleep=lambda s: None)
    with pytest.raises(YouTubeError, match="invalidTitle"):
        client.upload(video, {})


def test_gives_up_after_max_retries(video, monkeypatch):
    monkeypatch.setattr(youtube, "MAX_RETRIES", 3)
    client = YouTubeClient(service=FakeService([_http_error(500)] * 5), sleep=lambda s: None)
    with pytest.raises(YouTubeError, match="after 3 retries"):
        client.upload(video, {})


def test_missing_login_message(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "SECRETS_DIR", tmp_path)
    with pytest.raises(YouTubeError, match="youtube-login --channel gta6"):
        youtube.load_credentials("gta6")

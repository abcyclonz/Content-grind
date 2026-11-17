"""YouTube Data API v3: OAuth (installed app), resumable upload, own-upload lookup.

Verified against developers.google.com/youtube/v3 on 2026-11-17:
- videos.insert has its own bucket: 100 calls/day by default (1 unit each);
  other methods share 10,000 units/day. Quotas reset at midnight Pacific.
- Since 2026-11-02, uploads from unverified API projects are NOT forced private.
- Retry 500/502/503/504 with exponential backoff (Google's sample: up to 10
  retries, sleep random() * 2**n).
- snippet.title <= 100 chars, description <= 5000 bytes, tags <= 500 chars,
  none may contain < or >. status.publishAt needs privacyStatus=private.
- status.containsSyntheticMedia discloses altered/synthetic content.

Tokens live outside the repo: ~/.content-network/youtube_token_<channel>.json
"""
from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Callable

from network.config import SECRETS_DIR

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",  # find our own uploads after a crash
]
RETRY_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 10
CHUNK = 8 * 1024 * 1024  # multiple of 256 KB
log = logging.getLogger("youtube")


class YouTubeError(RuntimeError):
    pass


class QuotaExceeded(YouTubeError):
    """Daily quota or channel upload limit hit: stop cleanly, do not fail the job."""


def token_path(channel_id: str) -> Path:
    return SECRETS_DIR / f"youtube_token_{channel_id}.json"


def login(client_secret_file: str, channel_id: str) -> Path:
    """Browser OAuth consent for the YouTube channel this config uploads to."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True,
                                  authorization_prompt_message="Opening the browser to sign in: {url}")
    path = token_path(channel_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    return path


def load_credentials(channel_id: str):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    path = token_path(channel_id)
    if not path.exists():
        raise YouTubeError(f"not logged in to YouTube for {channel_id}; run: "
                           f"python -m network.cli youtube-login --channel {channel_id}")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise YouTubeError(
                f"YouTube login for {channel_id} expired ({e}). Run youtube-login again. If this happens "
                "every week, set the OAuth consent screen's publishing status to 'In production'."
            ) from None
        path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _http_status(err) -> int:
    return int(getattr(getattr(err, "resp", None), "status", 0) or 0)


def _reason(err) -> str:
    try:
        return err.error_details[0].get("reason", "")  # googleapiclient HttpError
    except (AttributeError, IndexError, TypeError):
        return ""


class YouTubeClient:
    def __init__(self, creds=None, service=None, sleep: Callable[[float], None] = time.sleep):
        if service is None:
            from googleapiclient.discovery import build
            service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        self.yt = service
        self.sleep = sleep

    def channel(self) -> dict:
        items = self.yt.channels().list(part="snippet,contentDetails", mine=True).execute().get("items", [])
        if not items:
            raise YouTubeError("this Google account has no YouTube channel")
        return items[0]

    def upload(self, path: Path, body: dict, on_progress: Callable[[float], None] | None = None) -> dict:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
        media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=CHUNK, resumable=True)
        request = self.yt.videos().insert(part="snippet,status", body=body, media_body=media)
        response, retry = None, 0
        while response is None:
            try:
                status, response = request.next_chunk()
                if status and on_progress:
                    on_progress(status.progress())
            except HttpError as e:
                code, reason = _http_status(e), _reason(e)
                if reason in ("quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded"):
                    raise QuotaExceeded(f"YouTube refused the upload: {reason}") from None
                if code not in RETRY_STATUS:
                    raise YouTubeError(f"upload failed: HTTP {code} {reason or e}") from None
                retry = self._backoff(retry, f"HTTP {code}")
            except (OSError, ConnectionError) as e:  # includes timeouts and dropped sockets
                retry = self._backoff(retry, type(e).__name__)
        return response

    def _backoff(self, retry: int, why: str) -> int:
        retry += 1
        if retry > MAX_RETRIES:
            raise YouTubeError(f"upload failed after {MAX_RETRIES} retries ({why})")
        wait = min(60.0, random.random() * 2 ** retry)
        log.warning("upload hiccup (%s); retry %d in %.1fs", why, retry, wait)
        self.sleep(wait)
        return retry

    def find_upload_with_tag(self, tag: str, look_back: int = 50) -> dict | None:
        """Find one of our recent uploads carrying an internal tag (used after a crash)."""
        uploads = self.channel()["contentDetails"]["relatedPlaylists"]["uploads"]
        items = self.yt.playlistItems().list(part="contentDetails", playlistId=uploads,
                                             maxResults=look_back).execute().get("items", [])
        ids = [i["contentDetails"]["videoId"] for i in items]
        if not ids:
            return None
        videos = self.yt.videos().list(part="snippet,status", id=",".join(ids)).execute().get("items", [])
        return next((v for v in videos if tag in (v.get("snippet", {}).get("tags") or [])), None)

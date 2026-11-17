"""Twitch login with the Device Code Grant Flow (public client, no secret).

Verified against dev.twitch.tv/docs/authentication on 2026-11-17:
- POST id.twitch.tv/oauth2/device {client_id, scopes} -> device_code, user_code,
  verification_uri, interval, expires_in.
- Poll POST id.twitch.tv/oauth2/token with grant_type
  urn:ietf:params:oauth:grant-type:device_code; 400 "authorization_pending" until done.
- Public-client refresh tokens are ONE-TIME USE and expire after 30 days, so
  every refresh must save the new refresh token immediately.
- Apps must validate the token at start-up and hourly (GET oauth2/validate).

The token file lives outside the repo, in ~/.content-network/.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from network.config import SECRETS_DIR

DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
SCOPES = ("user:read:chat",)
TOKEN_FILE = SECRETS_DIR / "twitch_token.json"
VALIDATE_EVERY = 3600


class TwitchAuthError(RuntimeError):
    pass


@dataclass
class TwitchToken:
    access_token: str
    refresh_token: str
    scopes: list[str]
    user_id: str = ""
    login: str = ""
    validated_at: float = 0.0
    obtained_at: float = field(default_factory=time.time)


def save_token(token: TwitchToken, path: Path = TOKEN_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(token), indent=2), encoding="utf-8")
    os.replace(tmp, path)  # refresh tokens are single-use: never lose the new one


def load_token(path: Path = TOKEN_FILE) -> TwitchToken | None:
    if not path.exists():
        return None
    return TwitchToken(**json.loads(path.read_text(encoding="utf-8")))


def _token_from(data: dict) -> TwitchToken:
    return TwitchToken(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        scopes=list(data.get("scope") or []),
    )


def validate(client: httpx.Client, access_token: str) -> dict | None:
    """Return the validate response, or None if the token is no longer valid."""
    r = client.get(VALIDATE_URL, headers={"Authorization": f"OAuth {access_token}"})
    if r.status_code == 401:
        return None
    r.raise_for_status()
    return r.json()


def device_login(
    client_id: str,
    show: Callable[[str], None] = print,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    path: Path = TOKEN_FILE,
) -> TwitchToken:
    own = client is None
    client = client or httpx.Client(timeout=20)
    try:
        r = client.post(DEVICE_URL, data={"client_id": client_id, "scopes": " ".join(SCOPES)})
        if r.status_code != 200:
            raise TwitchAuthError(f"device code request failed: HTTP {r.status_code} {r.text[:200]}")
        d = r.json()
        show(f"Open {d['verification_uri']} and enter the code {d['user_code']}")
        interval = int(d.get("interval") or 5)
        deadline = time.time() + int(d.get("expires_in") or 1800)
        while time.time() < deadline:
            sleep(interval)
            t = client.post(TOKEN_URL, data={
                "client_id": client_id,
                "scopes": " ".join(SCOPES),
                "device_code": d["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
            if t.status_code == 200:
                token = _token_from(t.json())
                break
            message = (t.json().get("message") or "") if t.headers.get("content-type", "").startswith(
                "application/json") else t.text
            if message == "authorization_pending":
                continue
            if message == "slow_down":  # not in Twitch's docs, but standard OAuth
                interval += 5
                continue
            raise TwitchAuthError(f"login failed: {message or t.status_code}")
        else:
            raise TwitchAuthError("the code expired before it was entered; run twitch-login again")

        info = validate(client, token.access_token)
        if info is None:
            raise TwitchAuthError("Twitch returned a token that does not validate")
        token.user_id, token.login = info.get("user_id") or "", info.get("login") or ""
        token.validated_at = time.time()
        save_token(token, path)
        return token
    finally:
        if own:
            client.close()


class TokenManager:
    """Hands out a valid access token; validates hourly and refreshes on demand."""

    def __init__(self, client_id: str, path: Path = TOKEN_FILE, client: httpx.Client | None = None):
        self.client_id = client_id
        self.path = path
        self.client = client or httpx.Client(timeout=20)
        token = load_token(path)
        if token is None:
            raise TwitchAuthError("not logged in to Twitch; run: python -m network.cli twitch-login")
        self.token = token

    def refresh(self) -> TwitchToken:
        r = self.client.post(TOKEN_URL, data={
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self.token.refresh_token,
        })
        if r.status_code != 200:
            raise TwitchAuthError(
                f"token refresh failed (HTTP {r.status_code}); run twitch-login again. "
                "Refresh tokens expire 30 days after login."
            )
        new = _token_from(r.json())
        new.user_id, new.login = self.token.user_id, self.token.login
        new.validated_at = time.time()
        self.token = new
        save_token(new, self.path)
        return new

    def access_token(self) -> str:
        if time.time() - self.token.validated_at >= VALIDATE_EVERY:
            info = validate(self.client, self.token.access_token)
            if info is None:
                self.refresh()
            else:
                self.token.validated_at = time.time()
                self.token.user_id = info.get("user_id") or self.token.user_id
                self.token.login = info.get("login") or self.token.login
                save_token(self.token, self.path)
        return self.token.access_token

    @property
    def user_id(self) -> str:
        return self.token.user_id

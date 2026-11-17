"""Minimal Helix client (users, streams, EventSub subscriptions).

Get Streams accepts user or app tokens, up to 100 repeated user_login params
(verified 2026-11-17). On a 401 the token is refreshed once and the call retried.
"""
from __future__ import annotations

import httpx

from network.twitch.auth import TokenManager

HELIX = "https://api.twitch.tv/helix"


class HelixError(RuntimeError):
    pass


class Helix:
    def __init__(self, client_id: str, tokens: TokenManager, client: httpx.Client | None = None):
        self.client_id = client_id
        self.tokens = tokens
        self.client = client or httpx.Client(timeout=20)

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        for attempt in (1, 2):
            headers = {"Client-Id": self.client_id,
                       "Authorization": f"Bearer {self.tokens.access_token()}"}
            r = self.client.request(method, f"{HELIX}{path}", headers=headers, **kw)
            if r.status_code == 401 and attempt == 1:
                self.tokens.refresh()
                continue
            if r.status_code >= 400:
                raise HelixError(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
            return r
        raise HelixError("unreachable")

    def users(self, logins: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(logins), 100):
            params = [("login", l) for l in logins[i:i + 100]]
            for u in self._request("GET", "/users", params=params).json().get("data", []):
                out[u["login"]] = u
        return out

    def live_streams(self, logins: list[str]) -> dict[str, dict]:
        """login -> stream object, for those currently live."""
        out: dict[str, dict] = {}
        for i in range(0, len(logins), 100):
            params = [("user_login", l) for l in logins[i:i + 100]] + [("type", "live")]
            for s in self._request("GET", "/streams", params=params).json().get("data", []):
                out[s["user_login"]] = s
        return out

    def subscribe_chat(self, session_id: str, broadcaster_id: str) -> str:
        body = {
            "type": "channel.chat.message",
            "version": "1",
            "condition": {"broadcaster_user_id": broadcaster_id, "user_id": self.tokens.user_id},
            "transport": {"method": "websocket", "session_id": session_id},
        }
        data = self._request("POST", "/eventsub/subscriptions", json=body).json()
        return data["data"][0]["id"]

    def unsubscribe(self, subscription_id: str) -> None:
        try:
            self._request("DELETE", "/eventsub/subscriptions", params={"id": subscription_id})
        except HelixError:
            pass  # already gone (e.g. the socket closed)

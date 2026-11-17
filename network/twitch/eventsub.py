"""EventSub WebSocket chat listener (runs in a background thread).

Verified 2026-11-17 (dev.twitch.tv/docs/eventsub):
- URL wss://eventsub.wss.twitch.tv/ws; subscribe within 10 s of session_welcome.
- No message or keepalive for keepalive_timeout_seconds -> assume lost, reconnect, resubscribe.
- session_reconnect -> connect to reconnect_url; existing subscriptions carry over.
- WebSocket subscriptions need a USER access token (app tokens fail).
- channel.chat.message v1 with a user token holding user:read:chat; condition
  {broadcaster_user_id, user_id (the account reading chat)}.
"""
from __future__ import annotations

import collections
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from websockets.sync.client import connect

from network.twitch.api import Helix, HelixError

EVENTSUB_URL = "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=30"
log = logging.getLogger("twitch.chat")


@dataclass
class ChatMessage:
    broadcaster_login: str
    chatter_login: str
    text: str
    emote_count: int
    received_at: float
    emotes: tuple[str, ...] = ()   # emote names, e.g. ("KEKW", "kaiLaugh")


def parse_notification(msg: dict, now: float) -> ChatMessage | None:
    if msg.get("metadata", {}).get("subscription_type") != "channel.chat.message":
        return None
    ev = msg.get("payload", {}).get("event", {})
    body = ev.get("message") or {}
    fragments = body.get("fragments") or []
    return ChatMessage(
        broadcaster_login=ev.get("broadcaster_user_login", ""),
        chatter_login=ev.get("chatter_user_login", ""),
        text=body.get("text", ""),
        emote_count=sum(1 for f in fragments if f.get("type") == "emote"),
        received_at=now,
        emotes=tuple(f.get("text", "") for f in fragments if f.get("type") == "emote"),
    )


class ChatListener(threading.Thread):
    def __init__(self, helix: Helix, on_message: Callable[[ChatMessage], None],
                 url: str = EVENTSUB_URL, connector=connect):
        super().__init__(daemon=True, name="twitch-chat")
        self.helix = helix
        self.on_message = on_message
        self.url = url
        self.connector = connector
        self._wanted: dict[str, str] = {}   # broadcaster_id -> login
        self._subs: dict[str, str] = {}     # broadcaster_id -> subscription id
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._seen: collections.deque[str] = collections.deque(maxlen=5000)
        self._seen_set: set[str] = set()

    # --- control (called from the watcher thread) ---
    def want(self, broadcaster_id: str, login: str) -> None:
        with self._lock:
            self._wanted[broadcaster_id] = login

    def drop(self, broadcaster_id: str) -> None:
        with self._lock:
            self._wanted.pop(broadcaster_id, None)

    def stop(self) -> None:
        self._stop.set()

    # --- internals ---
    def _dedupe(self, message_id: str) -> bool:
        if message_id in self._seen_set:
            return True
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(message_id)
        self._seen_set.add(message_id)
        return False

    def _sync_subscriptions(self, session_id: str) -> None:
        with self._lock:
            wanted = dict(self._wanted)
        for bid, login in wanted.items():
            if bid not in self._subs:
                try:
                    self._subs[bid] = self.helix.subscribe_chat(session_id, bid)
                    log.info("listening to chat of %s", login)
                except HelixError as e:
                    log.error("could not subscribe to %s chat: %s", login, e)
                    self.drop(bid)
        for bid in [b for b in self._subs if b not in wanted]:
            self.helix.unsubscribe(self._subs.pop(bid))

    def run(self) -> None:
        url, backoff, keep_subs = self.url, 1.0, False
        while not self._stop.is_set():
            try:
                url, keep_subs = self._session(url, keep_subs)
                backoff = 1.0
            except Exception as e:  # noqa: BLE001 - network errors: reconnect
                log.warning("chat connection lost (%s); reconnecting in %.0fs", e, backoff)
                url, keep_subs = self.url, False
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)

    def _session(self, url: str, keep_subs: bool) -> tuple[str, bool]:
        with self.connector(url, open_timeout=20, close_timeout=5) as ws:
            welcome = json.loads(ws.recv(timeout=15))
            if welcome.get("metadata", {}).get("message_type") != "session_welcome":
                raise RuntimeError("expected session_welcome")
            session = welcome["payload"]["session"]
            session_id = session["id"]
            keepalive = float(session.get("keepalive_timeout_seconds") or 30)
            if not keep_subs:
                self._subs.clear()  # a fresh session has no subscriptions
            last = time.monotonic()
            while not self._stop.is_set():
                self._sync_subscriptions(session_id)
                try:
                    raw = ws.recv(timeout=1.0)
                except TimeoutError:
                    if time.monotonic() - last > keepalive + 5:
                        raise RuntimeError("keepalive timeout")
                    continue
                last = time.monotonic()
                msg = json.loads(raw)
                meta = msg.get("metadata", {})
                kind = meta.get("message_type")
                if kind == "notification":
                    if self._dedupe(meta.get("message_id", "")):
                        continue
                    chat = parse_notification(msg, time.time())
                    if chat:
                        self.on_message(chat)
                elif kind == "session_reconnect":
                    return msg["payload"]["session"]["reconnect_url"], True
                elif kind == "revocation":
                    bid = msg.get("payload", {}).get("subscription", {}).get("condition", {}).get(
                        "broadcaster_user_id")
                    log.warning("Twitch revoked a chat subscription (%s)",
                                msg.get("payload", {}).get("subscription", {}).get("status"))
                    if bid:
                        self._subs.pop(bid, None)
                        self.drop(bid)
        return self.url, False

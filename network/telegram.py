"""Minimal Telegram Bot API client (long polling, no webhook).

Verified against core.telegram.org/bots/api (Bot API 10.4) on 2026-11-17:
- getUpdates(offset, timeout, allowed_updates): an update is confirmed once
  getUpdates is called with an offset above its update_id.
- sendMessage text 1-4096 chars; InlineKeyboardButton.callback_data 1-64 BYTES.
- sendVideo: bots can upload videos up to 50 MB; caption 0-1024 chars.
- Every callback query must be answered with answerCallbackQuery.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

API = "https://api.telegram.org/bot{token}/{method}"
TEXT_MAX = 4096
CAPTION_MAX = 1024
VIDEO_MAX_BYTES = 50 * 1024 * 1024


class TelegramError(RuntimeError):
    pass


def buttons(*rows: list[tuple[str, str]]) -> dict:
    for row in rows:
        for _, data in row:
            if not 1 <= len(data.encode("utf-8")) <= 64:
                raise ValueError(f"callback_data must be 1-64 bytes: {data!r}")
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class TelegramBot:
    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None):
        self.token, self.chat_id = token, str(chat_id)
        self.client = client or httpx.Client(timeout=60)

    def call(self, method: str, files: dict | None = None, **params) -> dict:
        url = API.format(token=self.token, method=method)
        if files:
            data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
                    for k, v in params.items() if v is not None}
            r = self.client.post(url, data=data, files=files, timeout=300)
        else:
            r = self.client.post(url, json={k: v for k, v in params.items() if v is not None})
        try:
            body = r.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {r.status_code}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', r.status_code)}")
        return body["result"]

    def me(self) -> dict:
        return self.call("getMe")

    def send(self, text: str, reply_markup: dict | None = None, reply_to: int | None = None) -> dict:
        return self.call("sendMessage", chat_id=self.chat_id, text=clip(text, TEXT_MAX),
                         reply_markup=reply_markup,
                         reply_parameters={"message_id": reply_to} if reply_to else None)

    def send_video(self, path: Path, caption: str, reply_markup: dict | None = None) -> dict:
        if path.stat().st_size > VIDEO_MAX_BYTES:
            return self.send(f"(video too large for Telegram: {path.stat().st_size / 1e6:.0f} MB)\n\n"
                             + caption, reply_markup)
        with open(path, "rb") as f:
            return self.call("sendVideo", files={"video": (path.name, f, "video/mp4")},
                             chat_id=self.chat_id, caption=clip(caption, CAPTION_MAX),
                             supports_streaming=True, reply_markup=reply_markup)

    def answer(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=clip(text, 200))

    def clear_buttons(self, message_id: int) -> None:
        try:
            self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=message_id,
                      reply_markup={"inline_keyboard": []})
        except TelegramError:
            pass  # message too old or already edited

    def updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        return self.call("getUpdates", offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"])

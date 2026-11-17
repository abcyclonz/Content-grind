"""Stage 7.9: approval gates over Telegram (long polling, no public webhook).

- notify(): for each job waiting at a gate whose auto_approve is off, send one
  message (ideas, script) or the video (final) with Approve / Reject buttons.
- auto_advance(): gates switched to auto_approve are passed without a message
  (this also clears items that were waiting when you flip the flag on).
- poll_once(): reads Telegram updates. Only the configured chat is obeyed.
  Button presses approve/reject; replying to a script message with edited
  lines replaces the script and approves it; "/status" lists the queue.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from network import db, gates
from network.config import AnyChannel, ChannelConfig, Settings
from network.telegram import TelegramBot, TelegramError, buttons

log = logging.getLogger("approve")

GATE_FLAG = {"idea_pending": "ideas_auto_approve", "scripted": "script_auto_approve",
             "packaged": "final_auto_approve"}
STATE_CODE = {"idea_pending": "i", "scripted": "s", "packaged": "f"}
CODE_STATE = {v: k for k, v in STATE_CODE.items()}


def gate_states(channel: AnyChannel) -> list[str]:
    return list(GATE_FLAG) if isinstance(channel, ChannelConfig) else ["packaged"]


def is_auto(channel: AnyChannel, state: str) -> bool:
    return bool(getattr(channel.gates, GATE_FLAG[state], False))


def auto_advance(conn: sqlite3.Connection, channel: AnyChannel) -> list[int]:
    moved = []
    for state in gate_states(channel):
        if is_auto(channel, state):
            for job in db.jobs_in_state(conn, state, channel.id):
                gates.approve(conn, job["id"])
                moved.append(job["id"])
    return moved


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------

def _read(settings: Settings, conn, job_id: int, kind: str) -> dict | None:
    art = db.get_artifact(conn, job_id, kind)
    if not art:
        return None
    return json.loads((settings.data_dir / Path(art["path"])).read_text(encoding="utf-8"))


def gate_message(conn, settings: Settings, channel: AnyChannel, job, state: str) -> tuple[str, Path | None]:
    jid = job["id"]
    if state == "idea_pending":
        idea = conn.execute("SELECT * FROM ideas WHERE id = ?", (job["idea_id"],)).fetchone()
        ids = json.loads(idea["signal_ids"])
        srcs = conn.execute(f"SELECT source, title FROM signals WHERE id IN ({','.join('?' * len(ids))})",
                            ids).fetchall() if ids else []
        text = (f"💡 IDEA #{jid} · {channel.name} · score {idea['score']:.0f}\n\n{idea['title']}\n"
                f"{idea['angle']}\n\nSources:\n" + "\n".join(f"• {s['title']} ({s['source']})" for s in srcs[:6]))
        return text, None
    if state == "scripted":
        doc = _read(settings, conn, jid, "script")
        lines = "\n".join(f"{i + 1}. {l['text']}" for i, l in enumerate(doc["lines"]))
        hosts = ", ".join(dict.fromkeys(urlsplit(u).netloc.removeprefix("www.") for u in doc["sources"]))
        text = (f"📝 SCRIPT #{jid} · {doc['idea']['title']}\n\n{lines}\n\n"
                f"{doc['word_count']} words · cites: {hosts}\n\n"
                f"To change it, reply to this message with all {len(doc['lines'])} lines edited.")
        return text, None
    meta = _read(settings, conn, jid, "metadata") or {}
    video = db.get_artifact(conn, jid, "video")
    caption = (f"🎬 FINAL #{jid} · {channel.name}\n\n{meta.get('title', '')}\n\n"
               f"{meta.get('description', '')[:600]}")
    return caption, settings.data_dir / Path(video["path"]) if video else None


def notify(conn: sqlite3.Connection, settings: Settings, channel: AnyChannel, bot: TelegramBot) -> int:
    sent = 0
    for state in gate_states(channel):
        if is_auto(channel, state):
            continue
        for job in db.jobs_in_state(conn, state, channel.id):
            if conn.execute("SELECT 1 FROM gate_messages WHERE job_id = ? AND state = ?",
                            (job["id"], state)).fetchone():
                continue
            code = STATE_CODE[state]
            kb = buttons([("✅ Approve", f"a:{job['id']}:{code}"), ("❌ Reject", f"r:{job['id']}:{code}")])
            text, video = gate_message(conn, settings, channel, job, state)
            msg = bot.send_video(video, text, kb) if video and video.exists() else bot.send(text, kb)
            with conn:
                conn.execute("INSERT OR IGNORE INTO gate_messages (job_id, state, chat_id, message_id, sent_at) "
                             "VALUES (?, ?, ?, ?, ?)", (job["id"], state, bot.chat_id, msg["message_id"], db.now_utc()))
            sent += 1
    return sent


# --------------------------------------------------------------------------
# Incoming updates
# --------------------------------------------------------------------------

def _status_text(conn) -> str:
    rows = conn.execute("SELECT channel_id, state, COUNT(*) FROM jobs GROUP BY channel_id, state "
                        "ORDER BY channel_id, state").fetchall()
    return "Queue:\n" + "\n".join(f"{c}: {s} × {n}" for c, s, n in rows) if rows else "No jobs yet."


def handle_update(conn, settings: Settings, channels: dict[str, AnyChannel], bot: TelegramBot,
                  update: dict) -> str | None:
    """Apply one update; returns a short description of what happened (for logs)."""
    cq = update.get("callback_query")
    if cq:
        chat = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
        if chat != bot.chat_id:
            bot.answer(cq["id"], "Not allowed.")
            return f"ignored button from chat {chat}"
        try:
            action, job_id, code = cq.get("data", "").split(":")
            job_id, state = int(job_id), CODE_STATE[code]
        except (ValueError, KeyError):
            bot.answer(cq["id"])
            return "ignored malformed button"
        job = db.get_job(conn, job_id)
        msg_id = cq["message"]["message_id"]
        if job is None or job["state"] != state:
            bot.answer(cq["id"], f"Already handled (now {job['state'] if job else 'gone'}).")
            bot.clear_buttons(msg_id)
            return f"stale button for job {job_id}"
        if action == "a":
            gates.approve(conn, job_id)
            result = "approved"
        else:
            gates.reject(conn, job_id, "rejected in Telegram")
            result = "rejected"
        with conn:
            conn.execute("UPDATE gate_messages SET resolved = ? WHERE job_id = ? AND state = ?",
                         (result, job_id, state))
        bot.answer(cq["id"], result.capitalize())
        bot.clear_buttons(msg_id)
        bot.send(f"{'✅' if result == 'approved' else '❌'} #{job_id} {result}", reply_to=msg_id)
        return f"job {job_id} {result} at {state}"

    msg = update.get("message")
    if not msg:
        return None
    if str(msg.get("chat", {}).get("id", "")) != bot.chat_id:
        return f"ignored message from chat {msg.get('chat', {}).get('id')}"
    text = (msg.get("text") or "").strip()
    if text.startswith("/status"):
        bot.send(_status_text(conn))
        return "sent status"
    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    if reply_to and text:
        row = conn.execute("SELECT job_id, state FROM gate_messages WHERE chat_id = ? AND message_id = ?",
                           (bot.chat_id, reply_to)).fetchone()
        if row and row["state"] == "scripted":
            job = db.get_job(conn, row["job_id"])
            if job is None or job["state"] != "scripted":
                bot.send(f"#{row['job_id']} is no longer waiting for script approval.", reply_to=msg["message_id"])
                return "stale edit"
            from network.stages.script import apply_edit
            try:
                apply_edit(conn, settings, channels[job["channel_id"]], job["id"], text)
            except ValueError as e:
                bot.send(f"Edit not applied: {e}", reply_to=msg["message_id"])
                return f"bad edit for job {job['id']}"
            gates.approve(conn, job["id"])
            with conn:
                conn.execute("UPDATE gate_messages SET resolved = 'edited' WHERE job_id = ? AND state = 'scripted'",
                             (job["id"],))
            bot.clear_buttons(reply_to)
            bot.send(f"✏️ #{job['id']} script replaced with your edit and approved.", reply_to=msg["message_id"])
            return f"job {job['id']} edited and approved"
    return None


def poll_once(conn, settings: Settings, channels: dict[str, AnyChannel], bot: TelegramBot,
              timeout: int = 25) -> list[str]:
    row = conn.execute("SELECT value FROM kv WHERE key = 'telegram_offset'").fetchone()
    offset = int(row[0]) if row else None
    done = []
    for update in bot.updates(offset, timeout=timeout):
        try:
            what = handle_update(conn, settings, channels, bot, update)
        except (TelegramError, Exception) as e:  # noqa: BLE001 - one bad update must not stop polling
            what = f"error handling update {update.get('update_id')}: {e}"
            log.error(what)
        if what:
            done.append(what)
        with conn:  # confirm the update even if handling failed, so it is not replayed forever
            conn.execute("INSERT INTO kv (key, value) VALUES ('telegram_offset', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(update["update_id"] + 1),))
    return done


def make_bot(settings: Settings) -> TelegramBot | None:
    import os
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return TelegramBot(token, chat) if token and chat else None

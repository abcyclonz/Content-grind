import json

import httpx
import pytest

from conftest import with_changes
from network import db
from network.stages import approve
from network.stages.script import ScriptFile, apply_edit
from network.telegram import TelegramBot, TelegramError, buttons

from test_voice_captions import LINES, _scripted_job

CHAT = "555"


class FakeBot:
    chat_id = CHAT

    def __init__(self, updates=None):
        self.sent, self.videos, self.answers, self.cleared = [], [], [], []
        self._updates = list(updates or [])
        self._next = 100

    def _msg(self):
        self._next += 1
        return {"message_id": self._next}

    def send(self, text, reply_markup=None, reply_to=None):
        self.sent.append({"text": text, "kb": reply_markup, "reply_to": reply_to})
        return self._msg()

    def send_video(self, path, caption, reply_markup=None):
        self.videos.append({"path": path, "caption": caption, "kb": reply_markup})
        return self._msg()

    def answer(self, cid, text=""):
        self.answers.append(text)

    def clear_buttons(self, mid):
        self.cleared.append(mid)

    def updates(self, offset, timeout=25):
        out, self._updates = self._updates, []
        return out


def _idea_job(conn):
    with conn:
        s = conn.execute("INSERT INTO signals (channel_id, source, url, title, fetched_at, hash) "
                         "VALUES ('gta6','IGN','u','Pre-load opens','now','h')").lastrowid
        i = conn.execute("INSERT INTO ideas (channel_id, title, angle, signal_ids, score, status, created_at) "
                         "VALUES ('gta6','Pre-load opens Monday','When to download.',?,82,'selected','now')",
                         (json.dumps([s]),)).lastrowid
    return db.create_job(conn, "gta6", idea_id=i)


def _press(job_id, code, action="a", chat=CHAT, msg_id=101, uid=1):
    return {"update_id": uid, "callback_query": {"id": "cb", "data": f"{action}:{job_id}:{code}",
            "message": {"message_id": msg_id, "chat": {"id": int(chat)}}}}


def test_idea_gate_message_and_approve(conn, settings, channel):
    job = _idea_job(conn)
    bot = FakeBot()
    assert approve.notify(conn, settings, channel, bot) == 1
    assert approve.notify(conn, settings, channel, bot) == 0          # announced once
    m = bot.sent[0]
    assert "IDEA #" in m["text"] and "Pre-load opens Monday" in m["text"] and "(IGN)" in m["text"]
    assert m["kb"]["inline_keyboard"][0][0]["callback_data"] == f"a:{job}:i"
    bot._updates = [_press(job, "i")]
    assert approve.poll_once(conn, settings, {"gta6": channel}, bot) == [f"job {job} approved at idea_pending"]
    assert db.get_job(conn, job)["state"] == "idea_approved"
    assert bot.cleared == [101] and bot.answers == ["Approved"]
    assert conn.execute("SELECT value FROM kv WHERE key='telegram_offset'").fetchone()[0] == "2"


def test_reject_and_stale_button(conn, settings, channel):
    job = _idea_job(conn)
    bot = FakeBot([_press(job, "i", "r", uid=5), _press(job, "i", "a", uid=6)])
    out = approve.poll_once(conn, settings, {"gta6": channel}, bot)
    assert out[0] == f"job {job} rejected at idea_pending" and "stale" in out[1]
    assert db.get_job(conn, job)["state"] == "rejected"


def test_other_chats_are_ignored(conn, settings, channel):
    job = _idea_job(conn)
    bot = FakeBot([_press(job, "i", chat="999"),
                   {"update_id": 2, "message": {"message_id": 1, "chat": {"id": 999}, "text": "/status"}}])
    out = approve.poll_once(conn, settings, {"gta6": channel}, bot)
    assert all(o.startswith("ignored") for o in out)
    assert db.get_job(conn, job)["state"] == "idea_pending" and bot.sent == []


def test_status_command(conn, settings, channel):
    _idea_job(conn)
    bot = FakeBot([{"update_id": 1, "message": {"message_id": 1, "chat": {"id": int(CHAT)}, "text": "/status"}}])
    approve.poll_once(conn, settings, {"gta6": channel}, bot)
    assert "gta6: idea_pending × 1" in bot.sent[0]["text"]


def _long_lines():
    return [f"Line {i} " + "word " * 20 + "end." for i in range(6)]


def test_script_edit_by_reply(conn, settings, channel):
    job = _scripted_job(conn, settings, lines=_long_lines(), state="scripted")
    bot = FakeBot()
    approve.notify(conn, settings, channel, bot)
    gate_msg = bot._next
    assert "reply to this message with all 6 lines" in bot.sent[0]["text"]
    edited = "\n".join(f"{i + 1}. Edited {i} " + "word " * 20 + "done." for i in range(6))
    bot._updates = [{"update_id": 9, "message": {"message_id": 50, "chat": {"id": int(CHAT)}, "text": edited,
                                                  "reply_to_message": {"message_id": gate_msg}}}]
    out = approve.poll_once(conn, settings, {"gta6": channel}, bot)
    assert out == [f"job {job} edited and approved"]
    assert db.get_job(conn, job)["state"] == "script_approved"
    doc = ScriptFile.model_validate_json((settings.jobs_dir / str(job) / "script.json").read_text(encoding="utf-8"))
    assert doc.edited and doc.lines[0].text.startswith("Edited 0") and doc.hook == doc.lines[0].text
    assert doc.lines[0].visual_prompt == "neon street"                    # image prompts kept


def test_bad_edit_is_explained_not_applied(conn, settings, channel):
    job = _scripted_job(conn, settings, lines=_long_lines(), state="scripted")
    bot = FakeBot()
    approve.notify(conn, settings, channel, bot)
    bot._updates = [{"update_id": 1, "message": {"message_id": 7, "chat": {"id": int(CHAT)}, "text": "just one line",
                                                  "reply_to_message": {"message_id": bot._next}}}]
    approve.poll_once(conn, settings, {"gta6": channel}, bot)
    assert "send exactly 6 lines" in bot.sent[-1]["text"]
    assert db.get_job(conn, job)["state"] == "scripted"


def test_apply_edit_word_limits(conn, settings, channel):
    job = _scripted_job(conn, settings, lines=_long_lines(), state="scripted")
    with pytest.raises(ValueError, match="must be 110 to 140"):
        apply_edit(conn, settings, channel, job, "\n".join(["short line"] * 6))


def test_final_gate_sends_video(conn, settings, channel):
    job = db.create_job(conn, "gta6", state="packaged")
    d = settings.jobs_dir / str(job)
    d.mkdir(parents=True)
    (d / "final.mp4").write_bytes(b"v")
    (d / "metadata.json").write_text(json.dumps({"title": "GTA 6 news", "description": "Desc"}), encoding="utf-8")
    with conn:
        for kind, name in (("video", "final.mp4"), ("metadata", "metadata.json")):
            conn.execute("INSERT INTO artifacts (job_id, kind, path, meta, created_at) VALUES (?,?,?,'{}','now')",
                         (job, kind, f"jobs/{job}/{name}"))
    bot = FakeBot()
    approve.notify(conn, settings, channel, bot)
    assert bot.videos[0]["path"].name == "final.mp4" and "GTA 6 news" in bot.videos[0]["caption"]
    assert bot.videos[0]["kb"]["inline_keyboard"][0][0]["callback_data"] == f"a:{job}:f"


def test_auto_approve_skips_message(conn, settings, channel):
    job = _idea_job(conn)
    ch = with_changes(channel, gates={"ideas_auto_approve": True})
    bot = FakeBot()
    assert approve.auto_advance(conn, ch) == [job]
    assert approve.notify(conn, settings, ch, bot) == 0 and bot.sent == []
    assert db.get_job(conn, job)["state"] == "idea_approved"


def test_bot_http_layer():
    seen = []

    def handler(req):
        seen.append((req.url.path, req.content))
        if req.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "goose_bot"}})
        return httpx.Response(200, json={"ok": False, "description": "Bad Request: chat not found"})

    bot = TelegramBot("TOKEN", CHAT, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert bot.me()["username"] == "goose_bot" and seen[0][0] == "/botTOKEN/getMe"
    with pytest.raises(TelegramError, match="chat not found"):
        bot.send("hi")
    with pytest.raises(ValueError, match="1-64 bytes"):
        buttons([("x", "a" * 65)])

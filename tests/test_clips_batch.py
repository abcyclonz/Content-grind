import json
import random
import subprocess

from network.clips import batch


def _cfg(**kw):
    base = dict(url="https://youtu.be/abcdefghijk", streamer_name="Speed", credit="", facecam=None)
    base.update(kw)
    return batch.BatchConfig(**base)


def test_windows_overlap_into_previous_hour():
    w = batch.windows(3 * 3600 + 100, _cfg())
    assert w == [(1, 0, 3600), (2, 3300, 7200), (3, 6900, 10800), (4, 10500, 10900)]
    assert batch.windows(10_000, _cfg(start=3600, end=7200)) == [(2, 3600, 7200)]


def _chat(seconds, bursts, seed=1):
    rnd = random.Random(seed)
    rows = []
    for t in range(seconds):
        for k in range(10):                      # steady, capped chat: 10 msg/s
            rows.append({"t": t + k / 10, "text": rnd.choice(["hi", "what game", "kfc", "speed"]),
                         "emotes": 0, "user": f"u{rnd.randrange(500)}"})
    for b, strength in bursts:                   # reaction bursts: part of chat turns into "L"
        for row in rows:
            if b <= row["t"] < b + 10 and rnd.random() < strength:
                row["text"] = "L"
    return rows


def test_pick_moments_reaches_minimum_by_relaxing():
    chat = _chat(3600, [(600, 0.9), (1500, 0.25), (2100, 0.2), (2700, 0.3), (3200, 0.25), (3400, 0.2)])
    picked = batch.pick_moments(chat, 0, 3600, want=5, max_n=8, taken=[])
    assert len(picked) >= 5
    assert abs(picked[0].start - 600) <= 20                      # strongest first
    starts = sorted(m.start for m in picked)
    assert all(b - a >= batch.MIN_GAP_SECONDS for a, b in zip(starts, starts[1:]))


def test_pick_moments_skips_moments_already_taken_in_the_overlap():
    chat = _chat(3600, [(600, 0.9)])
    picked = batch.pick_moments(chat, 0, 3600, want=1, max_n=1, taken=[600.0])
    assert all(abs(m.start - 600) >= batch.MIN_GAP_SECONDS for m in picked)


def test_write_caption_parses_claude_output(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "extract_frames", lambda *a: ["frames.jpg"])
    monkeypatch.setattr(batch, "find_claude_exe", lambda: "claude")
    seen = {}

    def runner(cmd, **kw):
        seen["cmd"], seen["prompt"] = cmd, kw["input"]
        out = {"structured_output": {"caption": "Speed failed it 😭", "facecam_visible": True,
                                     "what_happened": "x"}}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")

    cap = batch.write_caption(tmp_path / "c.mp4", {"chat_samples": ["L", "L"], "reaction": "fail"},
                              _cfg(), tmp_path, "title", runner=runner, transcript="oh no he died")
    assert cap["caption"] == "Speed failed it 😭"
    assert cap["v"] == batch.CAPTION_VERSION
    assert "Read" in seen["cmd"]
    assert "oh no he died" in seen["prompt"] and "fail" in seen["prompt"]
    assert "- L" not in seen["prompt"]                 # raw chat messages are no longer sent


def test_chat_context_keeps_topics_and_drops_spam():
    rows = []
    for i in range(20):
        rows.append({"t": 10 + i * 0.5, "text": "gg", "user": f"a{i}"})
        rows.append({"t": 10 + i * 0.5, "text": "GGG", "user": f"b{i}"})
    for i in range(6):
        rows.append({"t": 12 + i, "text": "24", "user": f"c{i}"})
    rows += [{"t": 15, "text": "AND BRO IS STILL NOT GONNA SLEEP", "user": "d"},
             {"t": 16, "text": "hand-purple-blue-peace hand-purple-blue-peace", "user": "e"},
             {"t": 16, "text": "1111111", "user": "f"},
             {"t": 17, "text": "check https://spam.example", "user": "g"},
             {"t": 99, "text": "too late to count", "user": "h"}]
    repeated, messages = batch.chat_context(rows, 0, 30)
    assert repeated == [("24", 6)]
    assert messages == ["AND BRO IS STILL NOT GONNA SLEEP"]


def test_write_video_log(tmp_path):
    video = tmp_path / "01_0-28-50_score44.mp4"
    entry = {"window_seconds": [1705.0, 1740.0], "title": "Speed hits 24 hours live",
             "caption": "Speed just hit 24 hours live 😱", "what_happened": "x", "trigger": "24h",
             "trigger_source": "mixed", "confidence": "low", "check_by_eye": True,
             "reaction": "hype", "score": 44, "chat_reacted_at": "0:28:50", "facecam": True}
    cap = {"chat_repeated": [["24", 12]], "transcript": "24 baby"}
    path = batch.write_video_log(video, entry, cap, {"title": "GTA 6 stream", "channel": "IShowSpeed"},
                                 _cfg(credit="Footage: x"), 1, (0, 3600))
    text = path.read_text(encoding="utf-8")
    assert path.name == "01_0-28-50_score44.txt"
    for want in ("GTA 6 stream", "Hour:            1", "01_0-28-50_score44.mp4",
                 "Speed hits 24 hours live", "Speed just hit 24 hours live 😱", "24 (x12)",
                 "24 baby", "CHECK BY EYE", "?t=1707"):
        assert want in text


def test_old_captions_are_not_current(tmp_path):
    caps = tmp_path / "captions"
    caps.mkdir()
    (caps / "0-01-00.json").write_text(json.dumps({"caption": "x"}), encoding="utf-8")
    assert not batch._captions_current(tmp_path)
    (caps / "0-01-00.json").write_text(json.dumps({"caption": "x", "v": batch.CAPTION_VERSION}),
                                       encoding="utf-8")
    assert batch._captions_current(tmp_path)

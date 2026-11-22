import json
import shutil

import pytest

from network import media
from network.clips import youtube_vod as yv
from network.config import Detection


def _line(offset_ms, runs, author="viewer1", kind="liveChatTextMessageRenderer"):
    return json.dumps({"replayChatItemAction": {
        "videoOffsetTimeMsec": str(offset_ms),
        "actions": [{"addChatItemAction": {"item": {kind: {
            "message": {"runs": runs}, "authorName": {"simpleText": author}}}}}],
    }})


def test_video_id_from_urls():
    assert yv.video_id("https://www.youtube.com/watch?v=abcdefghijk&t=5") == "abcdefghijk"
    assert yv.video_id("https://youtu.be/abcdefghijk") == "abcdefghijk"
    assert yv.video_id("https://www.youtube.com/live/abcdefghijk") == "abcdefghijk"
    with pytest.raises(yv.YouTubeVodError):
        yv.video_id("https://example.com/nothing")


def test_parse_time_and_format():
    assert yv.parse_time("13:21:00") == 48060
    assert yv.parse_time("90:00") == 5400
    assert yv.parse_time("75") == 75
    assert yv.fmt_time(48084) == "13:21:24"
    with pytest.raises(ValueError):
        yv.parse_time("1:2:3:4")
    with pytest.raises(ValueError):
        yv.parse_time("abc")


def test_parse_chat_replay(tmp_path):
    f = tmp_path / "x.live_chat.json"
    f.write_text("\n".join([
        _line(2500, [{"text": "LETS GO "}, {"emoji": {"emojiId": "🔥"}}], "a"),
        _line(1000, [{"text": "hello"}], "b"),
        _line(-500, [{"text": "before the stream"}]),                       # negative offset: dropped
        _line(3000, [{"text": "$5 thanks"}], kind="liveChatPaidMessageRenderer"),  # super chat: skipped
        "not json",
        "",
    ]), encoding="utf-8")
    rows = yv.parse_chat_replay(f)
    assert [r["t"] for r in rows] == [1.0, 2.5]          # sorted, only text messages
    assert rows[1]["text"] == "LETS GO 🔥"
    assert rows[1]["emotes"] == 1
    assert rows[1]["user"] == "a"


def test_parse_chat_replay_window_stops_early(tmp_path):
    f = tmp_path / "x.live_chat.json"
    lines = [_line(ms, [{"text": f"m{ms}"}]) for ms in (1000, 5000, 9000, 12000)]
    f.write_text("\n".join(lines) + '\n{"truncated', encoding="utf-8")
    assert [r["t"] for r in yv.parse_chat_replay(f, start=4, end=10)] == [5.0, 9.0]
    assert yv.last_offset(f) == 12.0


def test_download_chat_reuses_file_that_covers_the_window(tmp_path, monkeypatch):
    part = tmp_path / "abcdefghijk.live_chat.json.part"
    part.write_text(_line(4_000_000, [{"text": "x"}]) + "\n", encoding="utf-8")
    monkeypatch.setattr(yv.subprocess, "Popen", lambda *a, **k: pytest.fail("should not download"))
    assert yv.download_chat("https://youtu.be/abcdefghijk", tmp_path, until=3600) == part


def test_find_moments_spots_a_burst():
    chat = [{"t": float(t), "text": "hi", "user": f"u{t % 7}"} for t in range(0, 600, 2)]
    burst = [{"t": 400 + i / 10, "text": "LMAOOO", "emotes": 0, "user": f"v{i}"} for i in range(150)]
    chat = sorted(chat + burst, key=lambda r: r["t"])
    spikes = yv.find_moments(chat, Detection(), start=300, end=599)
    assert any(s.accepted and 380 <= s.start <= 410 for s in spikes)   # start includes the 8 s window
    assert not yv.find_moments(chat, Detection(), start=450, end=599)  # outside the window: none


def test_write_chat_log_roundtrips_with_simulate_loader(tmp_path):
    from network.clips.simulate import load_chat
    rows = [{"t": 1.0, "text": "a", "emotes": 0, "user": "u"}]
    path = yv.write_chat_log(rows, tmp_path / "c.jsonl")
    assert load_chat(path) == rows


def _have_ffmpeg() -> bool:
    try:
        media.ffmpeg()
        media.ffprobe()
        return True
    except media.MediaError:
        return False


@pytest.mark.skipif(not _have_ffmpeg(), reason="needs ffmpeg")
def test_edit_clip_makes_a_vertical_short_without_the_original_audio(tmp_path):
    from network.clips import editor
    from network.config import Settings
    clip = tmp_path / "raw.mp4"
    media.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=8",
               "-f", "lavfi", "-i", "sine=frequency=1000:duration=8",
               "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(clip)])
    settings = Settings(dry_run=True, data_dir=tmp_path / "data", channels_dir=tmp_path)
    out = tmp_path / "short.mp4"
    res = editor.edit_clip(clip, "Speed just did something wild\nchat lost it", out, settings,
                           fit="crop", hook="NO WAY", credit="Footage: test")
    info = media.probe(out)
    assert (info["width"], info["height"]) == (1080, 1920)
    assert 1 < info["duration"] <= 8.1
    assert res.voice_source == "fixture"
    shutil.rmtree(tmp_path / "data", ignore_errors=True)


@pytest.mark.skipif(not _have_ffmpeg(), reason="needs ffmpeg")
def test_caption_clip_keeps_audio_and_is_vertical(tmp_path):
    from network.clips import overlay
    clip = tmp_path / "raw.mp4"
    media.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=3",
               "-f", "lavfi", "-i", "sine=frequency=500:duration=3",
               "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(clip)])
    out = tmp_path / "cap.mp4"
    info = overlay.render_caption_clip(clip, "Chat could not believe it 😭", out, tmp_path / "w",
                                       credit="Footage: test")
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["audio_codec"] == "aac"


@pytest.mark.skipif(not _have_ffmpeg(), reason="needs ffmpeg")
def test_caption_clip_with_facecam(tmp_path):
    from network.clips import overlay
    clip = tmp_path / "raw.mp4"
    media.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=2",
               "-c:v", "libx264", "-preset", "ultrafast", str(clip)])
    out = tmp_path / "cap.mp4"
    info = overlay.render_caption_clip(clip, "Face test", out, tmp_path / "w",
                                       facecam=overlay.parse_facecam("12:462:376:238"))
    assert (info["width"], info["height"]) == (1080, 1920)
    with pytest.raises(overlay.OverlayError):
        overlay.render_caption_clip(clip, "x", out, tmp_path / "w2", facecam=(1800, 0, 400, 200))
    with pytest.raises(overlay.OverlayError):
        overlay.parse_facecam("12:462")


def test_wrap_keeps_emoji_with_last_word():
    from PIL import Image, ImageDraw
    from network.clips import overlay
    d = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    font = overlay._load_font("text", overlay.TEXT_SIZE)
    lines = overlay.wrap(d, "then Monty died 😭", font, overlay._load_font("emoji", 66), 10_000)
    assert lines == ["then Monty died 😭"]
    narrow = overlay.wrap(d, "aaaa bbbb 😭", font, None, 1)
    assert narrow == ["aaaa", "bbbb 😭"]


def test_script_lines():
    from network.clips import editor
    assert editor.script_lines(" a  b \n\n c ") == ["a b", "c"]
    with pytest.raises(editor.EditError):
        editor.script_lines("  \n ")

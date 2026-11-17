"""End-to-end clip tests with real FFmpeg on a synthetic video (no network)."""
import json
import threading

import pytest

from conftest import FIXTURES
from network import db, media
from network.clips import pipeline
from network.clips.detector import Spike
from network.clips.recorder import Segment
from network.clips.simulate import simulate
from network.clips.watcher import ClipWatcher
from network.config import load_channel
from network.twitch.eventsub import ChatMessage

try:
    media.ffmpeg()
    HAVE_FFMPEG = True
except media.MediaError:
    HAVE_FFMPEG = False
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")


@pytest.fixture
def clip_channel():
    return load_channel(FIXTURES / "channels" / "clips_valid.yaml")


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory):
    out = tmp_path_factory.mktemp("video") / "stream.mp4"
    media.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-f", "lavfi",
               "-i", "sine=frequency=440:sample_rate=48000", "-t", "70",
               "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out)])
    return out


def _chat(path, spike_at=45):
    rows = []
    for t in range(0, 70):
        n = 6 if spike_at <= t < spike_at + 6 else 1
        rows += [{"t": t + k / n, "text": "KEKW KEKW" if n > 1 else "hello", "emotes": 2 if n > 1 else 0}
                 for k in range(n)]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return path


@needs_ffmpeg
def test_simulate_creates_a_vertical_clip_job(conn, settings, clip_channel, sample_video, tmp_path):
    jobs = simulate(conn, settings, clip_channel, "test_streamer", sample_video, _chat(tmp_path / "c.jsonl"))
    assert len(jobs) == 1
    job = db.get_job(conn, jobs[0])
    assert job["state"] == "assembled" and job["idea_id"] is None
    art = db.get_artifact(conn, jobs[0], "video")
    assert art["path"] == f"jobs/{jobs[0]}/final.mp4"
    info = media.probe(settings.data_dir / art["path"])
    assert (info["width"], info["height"]) == (360, 640)
    assert info["video_codec"] == "h264" and info["audio_codec"] == "aac"
    assert 10 <= info["duration"] <= 13.5        # pre 8 s + post 4 s
    meta = json.loads(art["meta"])
    assert meta["credit"] == "twitch.tv/test_streamer"
    assert meta["permission_evidence"].startswith("https://")
    assert "KEKW KEKW" in meta["chat_samples"]


def test_simulate_refuses_unlisted_streamer(conn, settings, clip_channel, tmp_path):
    with pytest.raises(ValueError, match="not a permitted streamer"):
        simulate(conn, settings, clip_channel, "someone_else", tmp_path / "x.mp4", tmp_path / "c.jsonl")


def test_daily_cap(conn, settings, clip_channel):
    for _ in range(clip_channel.clip.max_clips_per_day):
        db.create_job(conn, clip_channel.id, state="assembled")
    called = []
    spike = Spike(start=100, peak=105, end=110, peak_rate=5, baseline=1, messages=40)
    job = pipeline.produce_clip(conn, settings, clip_channel, clip_channel.streamers[0], spike, [],
                                renderer=lambda *a, **k: called.append(1))
    assert job is None and called == []


@needs_ffmpeg
def test_render_across_segments(settings, clip_channel, sample_video, tmp_path):
    """Two TS segments stitched; the window straddles the boundary."""
    segs = []
    for i, (a, b) in enumerate([(0, 20), (20, 40)]):
        seg = tmp_path / f"seg_{i}.ts"
        media.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", str(a), "-i",
                   str(sample_video), "-t", str(b - a), "-c", "copy", "-f", "mpegts", str(seg)])
        segs.append(Segment(seg, 1000.0 + a, 1000.0 + b))
    from network.clips.cutter import render_clip
    info = render_clip(segs, 1012.0, 1027.0, tmp_path / "out.mp4", video=clip_channel.video,
                       layout="center_crop", credit_text="twitch.tv/x", workdir=tmp_path / "w")
    assert 14 <= info["duration"] <= 15.5 and (info["width"], info["height"]) == (360, 640)


# ----------------------------------------------------------------------------
# Watcher with fakes (no network, no ffmpeg)
# ----------------------------------------------------------------------------

class FakeHelix:
    def __init__(self):
        self.live = {}

    def users(self, logins):
        return {l: {"id": f"id-{l}"} for l in logins}

    def live_streams(self, logins):
        return {l: self.live[l] for l in logins if l in self.live}


class FakeChat:
    def __init__(self, helix, on_message):
        self.wanted = {}

    def start(self):
        pass

    def stop(self):
        pass

    def want(self, bid, login):
        self.wanted[bid] = login

    def drop(self, bid):
        self.wanted.pop(bid, None)


class FakeRecorder:
    instances = []

    def __init__(self, url, out_dir, quality, buffer_seconds):
        self.url, self.running = url, False
        FakeRecorder.instances.append(self)

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def alive(self):
        return self.running

    def tick(self, now=None):
        pass

    def segments(self):
        return []


def test_watcher_follows_live_status_and_clips_spikes(conn, settings, clip_channel):
    clock = {"t": 10_000.0}
    produced = []
    helix = FakeHelix()
    w = ClipWatcher(conn, clip_channel, settings, helix, chat_factory=FakeChat,
                    recorder_factory=FakeRecorder,
                    producer=lambda conn, s, ch, streamer, spike, segs, info: produced.append((streamer.login, spike, info)),
                    clock=lambda: clock["t"])
    w.start()
    w.poll_live()
    assert w.live == {}

    helix.live["test_streamer"] = {"title": "GTA RP night", "game_name": "Grand Theft Auto V"}
    w.poll_live()
    assert "test_streamer" in w.live and w.chat.wanted == {"id-test_streamer": "test_streamer"}
    assert FakeRecorder.instances[-1].url == "https://www.twitch.tv/test_streamer"

    def second(rate, text):
        for k in range(rate):
            w.messages.put(ChatMessage("test_streamer", f"viewer{k}", text, 0, clock["t"] + k / rate))
        clock["t"] += 1
        w.step()

    for _ in range(70):
        second(1, "hi")
    for _ in range(6):
        second(8, "LUL LUL")
    for _ in range(20):
        second(1, "hi")
    assert len(produced) == 1
    login, spike, info = produced[0]
    assert login == "test_streamer" and info["title"] == "GTA RP night"

    helix.live.clear()
    w.poll_live()
    assert w.live == {} and w.chat.wanted == {} and not FakeRecorder.instances[-1].running


def test_watcher_ignores_chat_from_unwatched_channels(conn, settings, clip_channel):
    w = ClipWatcher(conn, clip_channel, settings, FakeHelix(), chat_factory=FakeChat,
                    recorder_factory=FakeRecorder, producer=lambda *a: None)
    w.start()
    w.messages.put(ChatMessage("random_channel", "v", "LUL", 0, 1.0))
    w.step()  # no crash, nothing tracked
    assert w.live == {}

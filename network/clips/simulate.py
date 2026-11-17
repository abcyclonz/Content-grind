"""Run the clip pipeline on a local video plus a chat log (no Twitch needed).

Chat log: JSON lines {"t": seconds_from_video_start, "text": "...", "emotes": 0, "user": "name"}
("emotes" and "user" are optional).
The video plays the role of the recorded buffer, starting at t = 0.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from network import media
from network.clips.detector import SpikeDetector
from network.clips.pipeline import produce_clip
from network.clips.recorder import Segment
from network.config import ClipChannelConfig, Settings

log = logging.getLogger("clips.simulate")


def load_chat(path: Path) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return sorted(rows, key=lambda r: r["t"])


def replay(chat: list[dict], detector: SpikeDetector, until: float | None = None, on_spike=None) -> list:
    """Feed a chat log through the detector second by second; return every spike (accepted or not)."""
    end = until if until is not None else (chat[-1]["t"] + 60 if chat else 0)
    spikes, i = [], 0
    for second in range(0, int(end) + 1):
        while i < len(chat) and chat[i]["t"] <= second:
            row = chat[i]
            detector.add(float(row["t"]), row.get("text", ""), int(row.get("emotes", 0)),
                         str(row.get("user", "")))
            i += 1
        spike = detector.check(float(second))
        if spike:
            spikes.append(spike)
            if on_spike:
                on_spike(spike)
    return spikes


def simulate(conn: sqlite3.Connection, settings: Settings, channel: ClipChannelConfig,
             streamer_login: str, video: Path, chat_log: Path) -> list[int]:
    streamer = next((s for s in channel.streamers if s.login == streamer_login), None)
    if streamer is None:
        raise ValueError(f"{streamer_login!r} is not a permitted streamer of channel {channel.id}")
    duration = media.probe(video)["duration"]
    segments = [Segment(Path(video), 0.0, duration)]
    jobs: list[int] = []

    def on_spike(spike):
        log.info("spike at %.0fs: %s", spike.start, spike.explain())
        if not spike.accepted:
            return
        try:
            job = produce_clip(conn, settings, channel, streamer, spike, segments,
                               {"title": f"(simulated) {Path(video).name}"})
        except media.MediaError as e:
            log.error("spike at %.0fs not clipped: %s", spike.start, e)
            return
        if job:
            jobs.append(job)

    replay(load_chat(chat_log), SpikeDetector(channel.detection, ignore_users=(streamer_login,)),
           until=duration, on_spike=on_spike)
    return jobs

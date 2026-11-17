"""Turn a chat spike into a clip job (shared by the live watcher and simulate)."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from network import db
from network.clips.cutter import render_clip
from network.clips.detector import Spike
from network.clips.recorder import Segment
from network.config import ClipChannelConfig, Settings, Streamer
from network.costs import local_day_start

log = logging.getLogger("clips")


def clip_window(channel: ClipChannelConfig, spike: Spike) -> tuple[float, float]:
    """Seconds before the chat reaction (the moment itself) and a little after."""
    return spike.start - channel.clip.pre_seconds, spike.start + channel.clip.post_seconds


def clips_today(conn: sqlite3.Connection, channel: ClipChannelConfig) -> int:
    since = local_day_start(channel).astimezone(timezone.utc).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE channel_id = ? AND created_at >= ?", (channel.id, since)
    ).fetchone()[0]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def produce_clip(
    conn: sqlite3.Connection,
    settings: Settings,
    channel: ClipChannelConfig,
    streamer: Streamer,
    spike: Spike,
    segments: list[Segment],
    stream_info: dict | None = None,
    renderer=render_clip,
) -> int | None:
    """Render the clip for a spike and record it as a job in state 'assembled'.

    Returns the job id, or None if today's clip cap is reached.
    """
    if clips_today(conn, channel) >= channel.clip.max_clips_per_day:
        log.info("[%s] daily clip cap (%d) reached; skipping spike", channel.id,
                 channel.clip.max_clips_per_day)
        return None
    t0, t1 = clip_window(channel, spike)
    work = settings.data_dir / "clips_work" / streamer.login
    tmp_out = work / f"clip_{int(spike.start)}.mp4"
    info = renderer(segments, t0, t1, tmp_out, video=channel.video, layout=channel.clip.layout,
                    credit_text=streamer.credit_text, workdir=work)

    stream_info = stream_info or {}
    meta = {
        "source": streamer.platform,
        "streamer": streamer.login,
        "streamer_name": streamer.name,
        "credit": streamer.credit_text,
        "permission_evidence": streamer.permission.evidence,
        "permission_conditions": streamer.permission.conditions,
        "stream_title": stream_info.get("title", ""),
        "game": stream_info.get("game_name", ""),
        "stream_started_at": stream_info.get("started_at", ""),
        "window": [_iso(t0), _iso(t1)],
        "spike": {"start": _iso(spike.start), "peak": _iso(spike.peak),
                  "intensity": round(spike.intensity, 2), "peak_rate": round(spike.peak_rate, 2),
                  "baseline": round(spike.baseline, 2), "messages": spike.messages,
                  "score": spike.score, "category": spike.category,
                  "unique_chatters": spike.unique_chatters, "breadth": spike.breadth,
                  "clip_requests": spike.clip_requests, "top_reactions": spike.top_reactions,
                  "reasons": spike.reasons},
        "chat_samples": spike.samples,
        "probe": info,
    }
    ts = db.now_utc()
    with conn:
        job_id = conn.execute(
            "INSERT INTO jobs (channel_id, idea_id, state, created_at, updated_at) "
            "VALUES (?, NULL, 'assembled', ?, ?)", (channel.id, ts, ts),
        ).lastrowid
        rel = f"jobs/{job_id}/final.mp4"
        conn.execute(
            "INSERT INTO artifacts (job_id, kind, path, meta, created_at) VALUES (?, 'video', ?, ?, ?)",
            (job_id, rel, json.dumps(meta, ensure_ascii=False), ts),
        )
    dest = settings.data_dir / Path(rel)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_out, dest)
    except OSError as e:
        db.mark_failed(conn, job_id, f"could not move rendered clip: {e}")
        raise
    log.info("[%s] clip job %d from %s: %.0fs, chat x%.1f", channel.id, job_id, streamer.login,
             info.get("duration", 0), spike.intensity)
    return job_id

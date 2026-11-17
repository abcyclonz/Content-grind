"""Stage 7.7: render final.mp4 (1080x1920, 30 fps, H.264 + AAC) for visuals_ready jobs.

Fails the job if the result is longer than video.max_seconds. Uses
assets/<channel>/music/ (first file) under the voice if present.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from network import db
from network.config import ASSETS_DIR, PROJECT_ROOT, ChannelConfig, Settings
from network.files import job_dir, rel_to_data
from network.render import Scene, build_ass, render_short, sources_credit
from network.stages.voice import load_script

MUSIC_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac"}


@dataclass
class AssembleReport:
    channel_id: str
    done: list[tuple[int, float]] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        if not (self.done or self.failed):
            return f"[assemble] {self.channel_id}: no jobs in visuals_ready; nothing to do"
        lines = [f"[assemble] {self.channel_id}: rendered "
                 + (", ".join(f"job {j} ({s:.1f}s)" for j, s in self.done) or "none")]
        lines += [f"  job {j} failed: {e}" for j, e in self.failed]
        return "\n".join(lines)


def pick_music(channel: ChannelConfig) -> Path | None:
    folder = ASSETS_DIR / channel.id / "music"
    if not folder.is_dir():
        return None
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in MUSIC_EXTS)
    return files[0] if files else None


def run(conn: sqlite3.Connection, channel: ChannelConfig, settings: Settings) -> AssembleReport:
    report = AssembleReport(channel.id)
    for job in db.jobs_in_state(conn, "visuals_ready", channel.id):
        work = job_dir(settings, job["id"]) / "render"
        try:
            script = load_script(conn, settings, job["id"])
            captions = json.loads((settings.data_dir / Path(
                db.get_artifact(conn, job["id"], "captions")["path"])).read_text(encoding="utf-8"))
            visuals = json.loads((settings.data_dir / Path(
                db.get_artifact(conn, job["id"], "image")["path"])).read_text(encoding="utf-8"))
            voice = settings.data_dir / Path(db.get_artifact(conn, job["id"], "voice")["path"])
            scenes = [Scene(s["start"], s["end"], s["source"],
                            Path(s["path"]) if s.get("path") else None, s.get("palette", 0))
                      for s in visuals["scenes"]]
            content_end = sum(s.duration for s in scenes)
            outro, sting, outro_note = channel.outro, None, ""
            if outro and content_end + outro.seconds <= channel.video.max_seconds:
                scenes[-1].end += outro.seconds          # last background carries the outro
                sting = PROJECT_ROOT / outro.sound if outro.sound else None
            elif outro:
                outro, outro_note = None, "outro skipped: video would exceed max_seconds"
            total = sum(s.duration for s in scenes)
            ass = build_ass(captions, channel.video, script.idea.title, sources_credit(script.sources),
                            total, outro_text=outro.text if outro else "",
                            outro_start=content_end if outro else None)
            out = job_dir(settings, job["id"]) / "final.mp4"
            info = render_short(scenes, voice, ass, out, channel.video, work, music=pick_music(channel),
                                sting=sting, sting_at=content_end + 0.05)
            if outro_note:
                info["note"] = outro_note
            if info["duration"] > channel.video.max_seconds + 0.05:
                out.unlink(missing_ok=True)
                raise ValueError(f"video is {info['duration']:.1f}s, over the {channel.video.max_seconds}s limit")
            if (info["width"], info["height"]) != (channel.video.width, channel.video.height):
                raise ValueError(f"rendered {info['width']}x{info['height']}, expected "
                                 f"{channel.video.width}x{channel.video.height}")
            db.advance_with_artifact(conn, job["id"], "visuals_ready", "assembled", "video",
                                     rel_to_data(settings, out), {"probe": info})
            shutil.rmtree(work, ignore_errors=True)
            report.done.append((job["id"], info["duration"]))
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            db.mark_failed(conn, job["id"], msg)
            report.failed.append((job["id"], msg))
    return report

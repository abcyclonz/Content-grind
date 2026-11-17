"""Stage 7.6: choose a background for each script line -> visuals.json.

Order per line: an unused image from assets/<channel>/official/ (if enabled),
then the configured image provider (step 2: Cloudflare FLUX), else a local
animated neon gradient ("procedural"), which needs no API and costs nothing.
Each scene gets its time span from the caption timestamps.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from network import db
from network.config import ASSETS_DIR, ChannelConfig, Settings
from network.files import job_dir, rel_to_data, write_json_atomic
from network.render import scene_spans
from network.stages.voice import load_script

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
PROCEDURAL = {"procedural", "tbd", "none", ""}


@dataclass
class VisualsReport:
    channel_id: str
    done: list[tuple[int, dict]] = field(default_factory=list)   # job, counts per source
    failed: list[tuple[int, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        if not (self.done or self.failed):
            return f"[visuals] {self.channel_id}: no jobs in captioned; nothing to do"
        lines = [f"[visuals] {self.channel_id}: " + (", ".join(
            f"job {j} ({', '.join(f'{v} {k}' for k, v in c.items())})" for j, c in self.done) or "none")]
        lines += [f"  job {j} failed: {e}" for j, e in self.failed]
        return "\n".join(lines)


def used_official(conn: sqlite3.Connection, channel_id: str) -> set[str]:
    used: set[str] = set()
    for (meta,) in conn.execute(
        "SELECT a.meta FROM artifacts a JOIN jobs j ON j.id = a.job_id "
        "WHERE a.kind = 'image' AND j.channel_id = ?", (channel_id,)):
        used.update(json.loads(meta).get("official_used", []))
    return used


def official_images(channel: ChannelConfig) -> list[Path]:
    folder = ASSETS_DIR / channel.id / "official"
    if not channel.visuals.use_official_folder or not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTS)


def run(conn: sqlite3.Connection, channel: ChannelConfig, settings: Settings) -> VisualsReport:
    report = VisualsReport(channel.id)
    for job in db.jobs_in_state(conn, "captioned", channel.id):
        try:
            script = load_script(conn, settings, job["id"])
            cap_art = db.get_artifact(conn, job["id"], "captions")
            captions = json.loads((settings.data_dir / Path(cap_art["path"])).read_text(encoding="utf-8"))
            spans = scene_spans(captions, len(script.lines))
            pool = [p for p in official_images(channel)
                    if p.name not in used_official(conn, channel.id)]
            scenes, counts, official_used = [], {}, []
            for i, (line, (start, end)) in enumerate(zip(script.lines, spans)):
                if pool:
                    img = pool.pop(0)
                    scene = {"line": i, "start": start, "end": end, "source": "image",
                             "path": str(img), "origin": "official"}
                    official_used.append(img.name)
                elif channel.visuals.provider.lower() not in PROCEDURAL:
                    raise NotImplementedError(
                        f"image provider {channel.visuals.provider!r} arrives in step 2; "
                        "use provider: procedural for now")
                else:
                    scene = {"line": i, "start": start, "end": end, "source": "procedural",
                             "palette": (job["id"] + i) % 6}
                scene["visual_prompt"] = line.visual_prompt
                scenes.append(scene)
                counts[scene.get("origin", scene["source"])] = counts.get(
                    scene.get("origin", scene["source"]), 0) + 1
            path = job_dir(settings, job["id"]) / "visuals.json"
            write_json_atomic(path, {"job_id": job["id"], "scenes": scenes})
            db.advance_with_artifact(conn, job["id"], "captioned", "visuals_ready", "image",
                                     rel_to_data(settings, path),
                                     {"official_used": official_used, "counts": counts})
            report.done.append((job["id"], counts))
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            db.mark_failed(conn, job["id"], msg)
            report.failed.append((job["id"], msg))
    return report

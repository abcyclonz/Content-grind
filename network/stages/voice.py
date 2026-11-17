"""Stage 7.4: synthesize the approved script and normalize loudness -> voice.mp3.

Idempotent: only script_approved jobs; an existing valid voice.mp3 is reused
without a new TTS call. Fails the job if the voice is longer than the video
limit (the script is too long for a Short).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from network import costs, db, media
from network.config import ChannelConfig, Settings
from network.costs import BudgetExceeded
from network.files import job_dir, rel_to_data, write_json_atomic
from network.providers.tts import TTSProvider, make_tts
from network.stages.script import ScriptFile


@dataclass
class VoiceReport:
    channel_id: str
    voiced: list[tuple[int, float]] = field(default_factory=list)  # job, seconds
    failed: list[tuple[int, str]] = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        if not (self.voiced or self.failed or self.note):
            return f"[voice] {self.channel_id}: no jobs in script_approved; nothing to do"
        lines = [f"[voice] {self.channel_id}: voiced "
                 + (", ".join(f"{j} ({s:.1f}s)" for j, s in self.voiced) or "none")]
        lines += [f"  job {j} failed: {e}" for j, e in self.failed]
        if self.note:
            lines.append(f"  {self.note}")
        return "\n".join(lines)


class OutOfCredits(RuntimeError):
    """Provider credits for this billing period are used up: stop cleanly, do not fail the job."""


def credits_left(tts) -> int | None:
    """Remaining provider credits if the provider can tell us (ElevenLabs), else None."""
    if not hasattr(tts, "subscription"):
        return None
    try:
        sub = tts.subscription()
        return int(sub["character_limit"]) - int(sub["character_count"])
    except Exception:  # noqa: BLE001 - unknown shape or network issue: don't block on it
        return None


def load_script(conn: sqlite3.Connection, settings: Settings, job_id: int) -> ScriptFile:
    art = db.get_artifact(conn, job_id, "script")
    if art is None:
        raise ValueError("job has no script")
    return ScriptFile.model_validate_json((settings.data_dir / Path(art["path"])).read_text(encoding="utf-8"))


def voice_job(conn, channel: ChannelConfig, settings: Settings, job, tts: TTSProvider) -> float:
    d = job_dir(settings, job["id"])
    out = d / "voice.mp3"
    script = load_script(conn, settings, job["id"])
    if out.exists():
        try:
            info = media.probe(out)  # resume after a crash between writing and recording
        except media.MediaError:
            info = None
    else:
        info = None
    if info is None:
        costs.check_budget(conn, channel)
        left = credits_left(tts)
        if left is not None and left < len(script.spoken_text):
            raise OutOfCredits(f"{tts.name}: {left} credits left this period, script needs up to "
                               f"{len(script.spoken_text)}")
        result = tts.synthesize(script.spoken_text, channel.voice.voice_id, channel.voice.speed)
        db.log_cost(conn, channel.id, tts.name, "tts.synthesize", result.est_usd,
                    units=result.characters, job_id=job["id"])
        raw = d / f"voice_raw.{result.ext}"
        raw.write_bytes(result.audio)
        if result.word_timings:
            write_json_atomic(d / "voice_timings.json", [asdict(w) for w in result.word_timings])
        media.normalize_voice(raw, out)
        raw.unlink(missing_ok=True)
        info = media.probe(out)
    seconds = info["duration"]
    if seconds > channel.video.max_seconds:
        out.unlink(missing_ok=True)
        raise ValueError(f"voice is {seconds:.1f}s, over the {channel.video.max_seconds}s limit; "
                         "shorten the script or raise voice.speed")
    meta = {"duration": round(seconds, 3), "provider": tts.name, "voice_id": channel.voice.voice_id,
            "speed": channel.voice.speed, "has_tts_timings": (d / "voice_timings.json").exists()}
    db.advance_with_artifact(conn, job["id"], "script_approved", "voiced", "voice",
                             rel_to_data(settings, out), meta)
    return seconds


def run(conn: sqlite3.Connection, channel: ChannelConfig, settings: Settings,
        tts: TTSProvider | None = None) -> VoiceReport:
    report = VoiceReport(channel.id)
    jobs = db.jobs_in_state(conn, "script_approved", channel.id)
    if not jobs:
        return report
    tts = tts or make_tts(channel, settings)
    for job in jobs:
        try:
            report.voiced.append((job["id"], voice_job(conn, channel, settings, job, tts)))
        except (BudgetExceeded, OutOfCredits) as e:
            report.note = f"stopped: {e}"
            break
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            db.mark_failed(conn, job["id"], msg)
            report.failed.append((job["id"], msg))
    return report

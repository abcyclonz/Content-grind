"""Stage 7.5: word-level timestamps for every script word -> captions.json.

Uses the TTS provider's timings when it returned them (voice_timings.json),
otherwise faster-whisper on voice.mp3. Either way the result is aligned to the
script text, so captions show exactly what was written, with a start and end
time for every word.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from network import db
from network.align import NullTranscriber, WhisperTranscriber, align, script_words
from network.config import ChannelConfig, Settings
from network.files import job_dir, rel_to_data, write_json_atomic
from network.providers.tts import WordTiming
from network.stages.voice import load_script

MIN_MATCH_RATIO = 0.4   # below this the audio probably does not match the script: fail
WARN_MATCH_RATIO = 0.75


class CaptionWord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    start: float
    end: float
    line: int


class CaptionsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: int
    duration: float
    source: str
    matched_ratio: float
    words: list[CaptionWord]

    @model_validator(mode="after")
    def _ordered(self) -> "CaptionsFile":
        prev = 0.0
        for w in self.words:
            if not (prev - 1e-6 <= w.start < w.end <= self.duration + 1e-6):
                raise ValueError(f"bad timing for {w.text!r}: {w.start}-{w.end}")
            prev = w.end
        return self


@dataclass
class CaptionsReport:
    channel_id: str
    captioned: list[tuple[int, float]] = field(default_factory=list)  # job, match ratio
    failed: list[tuple[int, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        if not (self.captioned or self.failed):
            return f"[captions] {self.channel_id}: no jobs in voiced; nothing to do"
        lines = [f"[captions] {self.channel_id}: captioned "
                 + (", ".join(f"{j} ({r:.0%} words heard)" for j, r in self.captioned) or "none")]
        lines += [f"  job {j} failed: {e}" for j, e in self.failed]
        lines += [f"  WARNING: {w}" for w in self.warnings]
        return "\n".join(lines)


def make_transcriber(channel: ChannelConfig, settings: Settings):
    if settings.dry_run:
        return NullTranscriber()
    return WhisperTranscriber(channel.captions.whisper_model, channel.captions.device, channel.language)


def run(conn: sqlite3.Connection, channel: ChannelConfig, settings: Settings,
        transcriber=None) -> CaptionsReport:
    report = CaptionsReport(channel.id)
    jobs = db.jobs_in_state(conn, "voiced", channel.id)
    if not jobs:
        return report
    transcriber = transcriber or make_transcriber(channel, settings)
    for job in jobs:
        try:
            script = load_script(conn, settings, job["id"])
            voice = db.get_artifact(conn, job["id"], "voice")
            duration = json.loads(voice["meta"])["duration"]
            d = job_dir(settings, job["id"])
            pairs = script_words([l.text for l in script.lines])
            timings_file = d / "voice_timings.json"
            if timings_file.exists():
                heard = [WordTiming(**w) for w in json.loads(timings_file.read_text(encoding="utf-8"))]
                source = "tts"
            else:
                heard = transcriber.transcribe(settings.data_dir / Path(voice["path"]), script.spoken_text)
                source = f"whisper:{getattr(transcriber, 'used', '?')}"
            result = align([w for w, _ in pairs], heard, duration)
            if not settings.dry_run and result.matched_ratio < MIN_MATCH_RATIO:
                raise ValueError(f"only {result.matched_ratio:.0%} of script words were heard in the "
                                 "voice; the audio may not match the script")
            if not settings.dry_run and result.matched_ratio < WARN_MATCH_RATIO:
                report.warnings.append(f"job {job['id']}: only {result.matched_ratio:.0%} of words "
                                       "matched; check caption sync")
            doc = CaptionsFile(
                job_id=job["id"], duration=duration, source=source,
                matched_ratio=round(result.matched_ratio, 3),
                words=[CaptionWord(text=w.text, start=w.start, end=w.end, line=line)
                       for w, (_, line) in zip(result.words, pairs)],
            )
            path = d / "captions.json"
            write_json_atomic(path, doc.model_dump())
            db.advance_with_artifact(conn, job["id"], "voiced", "captioned", "captions",
                                     rel_to_data(settings, path),
                                     {"source": source, "matched_ratio": doc.matched_ratio})
            report.captioned.append((job["id"], doc.matched_ratio))
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            db.mark_failed(conn, job["id"], msg)
            report.failed.append((job["id"], msg))
    return report

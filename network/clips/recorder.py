"""Rolling recording of a live stream: streamlink -> ffmpeg segment muxer.

Segments are ~2 s MPEG-TS files, copied (not re-encoded), so recording costs
almost no CPU. Each segment is stamped with the wall-clock time it finished
arriving; segments older than the buffer are deleted.
"""
from __future__ import annotations

import csv
import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from network import media

log = logging.getLogger("clips.recorder")


@dataclass
class Segment:
    path: Path
    start: float  # wall-clock seconds
    end: float


class Recorder:
    def __init__(self, stream_url: str, out_dir: Path, quality: str, buffer_seconds: float,
                 segment_seconds: int = 2):
        self.stream_url = stream_url
        self.out_dir = Path(out_dir)
        self.quality = quality
        self.buffer_seconds = buffer_seconds
        self.segment_seconds = segment_seconds
        self._segments: list[Segment] = []
        self._seen_rows = 0
        self._streamlink: subprocess.Popen | None = None
        self._ffmpeg: subprocess.Popen | None = None
        self.started_at = 0.0

    def start(self) -> None:
        if self.out_dir.exists():
            shutil.rmtree(self.out_dir, ignore_errors=True)
        self.out_dir.mkdir(parents=True)
        self._segments, self._seen_rows = [], 0
        sl_log = open(self.out_dir / "streamlink.log", "w", encoding="utf-8")
        self._streamlink = subprocess.Popen(
            [sys.executable, "-m", "streamlink", "--stdout", "--loglevel", "warning",
             "--retry-open", "3", self.stream_url, self.quality],
            stdout=subprocess.PIPE, stderr=sl_log,
        )
        ff_log = open(self.out_dir / "ffmpeg.log", "w", encoding="utf-8")
        self._ffmpeg = subprocess.Popen(
            [media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
             "-f", "segment", "-segment_time", str(self.segment_seconds),
             "-segment_format", "mpegts", "-segment_list", "segments.csv",
             "-segment_list_type", "csv", "-segment_list_flags", "+live", "seg_%06d.ts"],
            stdin=self._streamlink.stdout, stderr=ff_log, cwd=self.out_dir,
        )
        self._streamlink.stdout.close()  # ffmpeg owns the pipe now
        self.started_at = time.time()
        log.info("recording %s (%s)", self.stream_url, self.quality)

    def alive(self) -> bool:
        return bool(self._ffmpeg and self._ffmpeg.poll() is None
                    and self._streamlink and self._streamlink.poll() is None)

    def tick(self, now: float | None = None) -> None:
        """Pick up newly finished segments and delete expired ones."""
        now = now or time.time()
        listing = self.out_dir / "segments.csv"
        if listing.exists():
            with open(listing, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
            for row in rows[self._seen_rows:]:
                if len(row) < 3:
                    break  # partially written line; read it next tick
                name, start, end = row[0], float(row[1]), float(row[2])
                self._segments.append(Segment(self.out_dir / name, now - (end - start), now))
                self._seen_rows += 1
        cutoff = now - self.buffer_seconds
        while self._segments and self._segments[0].end < cutoff:
            old = self._segments.pop(0)
            old.path.unlink(missing_ok=True)

    def segments(self) -> list[Segment]:
        return list(self._segments)

    def stop(self) -> None:
        for proc in (self._streamlink, self._ffmpeg):
            if proc and proc.poll() is None:
                proc.terminate()
        for proc in (self._streamlink, self._ffmpeg):
            if proc:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        log.info("stopped recording %s", self.stream_url)

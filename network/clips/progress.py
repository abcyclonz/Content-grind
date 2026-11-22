"""Live progress bars for `clips stream` (run in a second terminal: `clips progress`)."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from network.clips import youtube_vod as yv

BAR = 30


def _bar(done: float, total: float) -> str:
    frac = 0.0 if total <= 0 else max(0.0, min(1.0, done / total))
    full = int(frac * BAR)
    return "[" + "#" * full + "-" * (BAR - full) + f"] {frac * 100:5.1f}%"


def _moments_in_log(data_dir: Path, hour: int) -> int | None:
    log = data_dir / "stream_clips_run.log"
    if not log.is_file():
        return None
    m = None
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        hit = re.match(rf"^hour {hour} \(.*\): (\d+) moments", line)
        if hit:
            m = int(hit.group(1))
    return m


STEPS = ["download", "caption", "edit", "saved"]


def clip_steps(hour_dir: Path, planned: int | None) -> list[str]:
    """One row per clip of the hour being worked on, from the files on disk.

    raw/<t>.mp4 = downloaded, captions/<t>.json = caption written,
    <rank>_<t>_*.part.mp4 = being edited, <rank>_<t>_*.mp4 = saved.
    """
    raw_dir, cap_dir = hour_dir / "raw", hour_dir / "captions"
    stamps = set()
    if raw_dir.is_dir():
        for p in raw_dir.iterdir():
            stamps.add(p.name.split(".")[0])
    rows = []
    for stamp in sorted(stamps):
        finals = [p for p in hour_dir.glob(f"*_{stamp}_score*.mp4")]
        saved = any(not p.name.endswith(".part.mp4") for p in finals)
        editing = any(p.name.endswith(".part.mp4") for p in finals)
        captioned = (cap_dir / f"{stamp}.json").is_file()
        downloaded = (raw_dir / f"{stamp}.mp4").is_file()
        if saved:
            step, label = 4, "saved"
        elif editing:
            step, label = 3, "editing video..."
        elif captioned:
            step, label = 3, "editing video..."
        elif downloaded:
            step, label = 2, "writing caption (Claude)..."
        else:
            step, label = 1, "downloading 1080p piece..."
        marks = " ".join(("[x]" if i < step or (i == step and saved) else
                          "[>]" if i == step else "[ ]") + f" {s}" for i, s in enumerate(STEPS, 1))
        caption = ""
        if captioned:
            try:
                caption = json.loads((cap_dir / f"{stamp}.json").read_text(encoding="utf-8"))["caption"]
            except (OSError, ValueError, KeyError):
                pass
        rows.append(f"   {stamp.replace('-', ':'):>8}  {marks}   {label}")
        if caption:
            rows.append(f"             \"{caption[:90]}\"")
    total = planned if planned is not None else len(stamps)
    waiting = max(0, total - len(stamps))
    if waiting:
        rows.append(f"   ... {waiting} more clip(s) waiting")
    saved_n = sum(1 for r in rows if r.endswith("saved"))
    return [f"   Current hour: {saved_n} of {total} clips saved  {_bar(saved_n, total or 1)}"] + rows


def snapshot(data_dir: Path, vid: str, chat_file: Path | None, per_hour: int) -> list[str]:
    root = data_dir / "stream_clips" / vid
    info_file = root / "stream.json"
    duration = 0.0
    if info_file.is_file():
        duration = json.loads(info_file.read_text(encoding="utf-8")).get("duration", 0.0)
    hours = int(-(-duration // 3600)) if duration else 24

    lines = [f"Stream clipping progress  ({time.strftime('%H:%M:%S')}, refreshes every 5 s, Ctrl+C to close)", ""]

    # 1) chat download
    vod = data_dir / "youtube_vods" / vid
    if (vod / f"{vid}.live_chat.json").is_file() or (vod / f"{vid}.chat.compact.jsonl").is_file():
        lines.append(f"1. Chat download     {_bar(1, 1)}  done")
    else:
        off = yv.last_offset(chat_file) if chat_file and chat_file.is_file() else None
        total = duration or 85260.0
        lines.append(f"1. Chat download     {_bar(off or 0, total)}  "
                     f"{yv.fmt_time(off or 0)} of {yv.fmt_time(total)}")

    # 2) chat loading / hours
    compact = vod / f"{vid}.chat.compact.jsonl"
    part = vod / f"{vid}.chat.compact.part"
    if part.is_file() and not compact.is_file():
        lines.append(f"2. Reading chat      working... ({part.stat().st_size / 1e6:.0f} MB written)")
    elif compact.is_file():
        lines.append(f"2. Reading chat      {_bar(1, 1)}  done")
    else:
        lines.append("2. Reading chat      waiting")

    done_hours, videos, current = 0, 0, None
    rows = []
    for i in range(1, hours + 1):
        hd = root / f"hour_{i:02d}"
        man = hd / "manifest.json"
        made = len([p for p in hd.glob("*.mp4") if not p.name.endswith(".part.mp4")]) if hd.is_dir() else 0
        videos += made
        if man.is_file() and json.loads(man.read_text(encoding="utf-8")).get("complete"):
            done_hours += 1
            rows.append(f"   hour {i:2d}  done     {made} videos")
        elif hd.is_dir():
            current = i
            rows.append(f"   hour {i:2d}  working  {made} videos so far")
    if current is not None:
        rows.append("")
        rows += clip_steps(root / f"hour_{current:02d}", _moments_in_log(data_dir, current))
    lines.append(f"3. Hours clipped     {_bar(done_hours, hours)}  {done_hours} of {hours}")
    lines.append(f"4. Videos made       {_bar(videos, hours * per_hour)}  {videos} "
                 f"(target at least {hours * per_hour})")
    lines.append("")
    lines += rows[-20:] if rows else ["   (no hour started yet)"]

    log = data_dir / "stream_clips_run.log"
    if log.is_file():
        tail = [l for l in log.read_text(encoding="utf-8", errors="replace").splitlines()
                if re.match(r"^\s+\d+\. |^hour |FAILED|warning|videos in", l)][-4:]
        if tail:
            lines += ["", "Latest:"] + [f"   {l.strip()[:110]}" for l in tail]
        if any("videos in" in l for l in tail):
            lines += ["", "FINISHED."]
    if current is None and done_hours == hours:
        lines += ["", "All hours done."]
    return lines


def watch(data_dir: Path, vid: str, chat_file: Path | None, per_hour: int = 5, every: float = 5.0) -> None:
    try:
        while True:
            text = "\n".join(snapshot(data_dir, vid, chat_file, per_hour))
            os.system("cls" if os.name == "nt" else "clear")
            print(text, flush=True)
            if "FINISHED." in text:
                return
            time.sleep(every)
    except KeyboardInterrupt:
        print()
        sys.exit(0)

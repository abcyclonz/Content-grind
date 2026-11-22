"""Find high moments in a past YouTube live stream from its live-chat replay.

Uses the same SpikeDetector as the Twitch watcher, fed from a finished stream:

1. yt-dlp saves the chat replay. For a time window only the chat up to the end of
   the window is downloaded (yt-dlp is stopped once it gets past it).
2. parse_chat_replay() turns YouTube's chat JSON into {"t", "text", "emotes", "user"} rows.
3. find_moments() replays the chat through the detector and returns the spikes.
4. download_section() fetches only the seconds around each spike (not the whole stream).

`clips find` (cli) runs 1-4 as a LOCAL TEST: clips go to data/test_clips/ and never
become pipeline jobs, so nothing from it can be uploaded.
"""
from __future__ import annotations

import collections
import json
import logging
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from network import media
from network.clips.detector import Spike, SpikeDetector, classify
from network.config import Detection

log = logging.getLogger("clips.youtube")

_VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/live/|/shorts/)([A-Za-z0-9_-]{11})")
_OFFSET = re.compile(rb'"videoOffsetTimeMsec":\s*"(-?\d+)"')
CHAT_MARGIN_SECONDS = 30      # keep downloading chat this far past the window end
DOWNLOAD_ATTEMPTS = 3         # tries per clip download


class YouTubeVodError(RuntimeError):
    pass


def video_id(url: str) -> str:
    m = _VIDEO_ID.search(url)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    raise YouTubeVodError(f"cannot find a YouTube video id in {url!r}")


def parse_time(value: str | float | int) -> float:
    """'2:00:00', '90:00', '5400' or 5400 -> seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    parts = str(value).strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"bad time {value!r}: use h:mm:ss, mm:ss or seconds") from None
    if len(nums) > 3 or any(n < 0 for n in nums):
        raise ValueError(f"bad time {value!r}: use h:mm:ss, mm:ss or seconds")
    secs = 0.0
    for n in nums:
        secs = secs * 60 + n
    return secs


def fmt_time(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


# --------------------------------------------------------------------------
# Chat replay
# --------------------------------------------------------------------------

def _runs_to_text(runs: list[dict]) -> tuple[str, int]:
    """Join message runs into text; custom/standard emoji count as emotes."""
    parts, emotes = [], 0
    for run in runs:
        if "text" in run:
            parts.append(run["text"])
        elif "emoji" in run:
            emotes += 1
            emoji = run["emoji"]
            shortcuts = emoji.get("shortcuts") or []
            # Unicode emoji have a real character; custom ones a :shortcut:.
            parts.append(shortcuts[0].strip(":") if shortcuts and emoji.get("isCustomEmoji")
                         else emoji.get("emojiId", ""))
    return " ".join(p for p in (s.strip() for s in parts) if p), emotes


def iter_chat_replay(path: Path, start: float = 0.0, end: float | None = None) -> Iterator[dict]:
    """Stream rows from yt-dlp's live_chat JSON (one object per line), within [start, end].

    Only ordinary text messages are kept: Super Chats, memberships and system
    notices are not reactions. Reading stops at the first line past `end`, so a
    huge replay file is not read to the bottom. A truncated last line (a file
    still being written) is skipped.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            replay = obj.get("replayChatItemAction") or {}
            try:
                t = int(replay.get("videoOffsetTimeMsec")) / 1000.0
            except (TypeError, ValueError):
                continue
            if end is not None and t > end:
                return
            if t < max(0.0, start):
                continue
            for action in replay.get("actions") or []:
                item = (action.get("addChatItemAction") or {}).get("item") or {}
                msg = item.get("liveChatTextMessageRenderer")
                if not msg:
                    continue
                text, emotes = _runs_to_text((msg.get("message") or {}).get("runs") or [])
                if not text:
                    continue
                user = (msg.get("authorName") or {}).get("simpleText", "")
                yield {"t": t, "text": text, "emotes": emotes,
                       "user": user or msg.get("authorExternalChannelId", "")}


def parse_chat_replay(path: Path, start: float = 0.0, end: float | None = None) -> list[dict]:
    return sorted(iter_chat_replay(path, start, end), key=lambda r: r["t"])


def write_chat_log(rows: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path


def last_offset(path: Path, tail_bytes: int = 256_000) -> float | None:
    """Video offset (seconds) of the newest chat line in a replay file, or None."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            found = _OFFSET.findall(fh.read())
    except OSError:
        return None
    return int(found[-1]) / 1000.0 if found else None


def _ytdlp_cmd(args: list[str]) -> list[str]:
    return [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress", *args]


def _ytdlp(args: list[str], timeout: float = 3600) -> None:
    try:
        proc = subprocess.run(_ytdlp_cmd(args), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except FileNotFoundError as e:
        raise YouTubeVodError(f"cannot run yt-dlp: {e}") from e
    if proc.returncode != 0:
        if "No module named yt_dlp" in (proc.stderr or ""):
            raise YouTubeVodError("yt-dlp is not installed: pip install -r requirements.txt")
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise YouTubeVodError("yt-dlp failed: " + " | ".join(tail))


def _replay_file(workdir: Path, vid: str) -> Path | None:
    for name in (f"{vid}.live_chat.json", f"{vid}.live_chat.json.part"):
        if (workdir / name).is_file():
            return workdir / name
    return None


def download_chat(url: str, workdir: Path, until: float | None = None,
                  poll_seconds: float = 5.0) -> Path:
    """Download the chat replay into workdir and return the raw file.

    With `until`, yt-dlp is stopped as soon as the replay passes that offset, and the
    partial file is returned. An existing file that already reaches `until` is reused.
    """
    vid = video_id(url)
    workdir.mkdir(parents=True, exist_ok=True)
    existing = _replay_file(workdir, vid)
    if existing and (existing.name.endswith(".live_chat.json")
                     or (until is not None and (last_offset(existing) or 0) >= until)):
        return existing

    cmd = _ytdlp_cmd(["--skip-download", "--write-subs", "--sub-langs", "live_chat",
                      "-o", str(workdir / "%(id)s.%(ext)s"), url])
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    part = workdir / f"{vid}.live_chat.json.part"
    reached = 0.0
    try:
        while proc.poll() is None:
            time.sleep(poll_seconds)
            off = last_offset(part)
            if off is not None and off > reached + 600:
                reached = off
                log.info("chat downloaded up to %s", fmt_time(off))
            if until is not None and off is not None and off >= until:
                log.info("chat reached %s; stopping the download", fmt_time(off))
                proc.terminate()
                proc.wait(timeout=30)
                return part
    except BaseException:
        proc.kill()
        raise
    err = proc.stderr.read() if proc.stderr else ""
    raw = _replay_file(workdir, vid)
    if proc.returncode != 0 and raw is None:
        if "No module named yt_dlp" in err:
            raise YouTubeVodError("yt-dlp is not installed: pip install -r requirements.txt")
        raise YouTubeVodError("yt-dlp failed: " + " | ".join(err.strip().splitlines()[-3:]))
    if raw is None:
        raise YouTubeVodError(
            "no chat replay found. The stream must be a finished live stream with chat replay "
            "turned on (it is not available for normal uploads or for streams with replay disabled).")
    return raw


def fetch_chat(url: str, workdir: Path, start: float = 0.0, end: float | None = None) -> Path:
    """Chat for [start, end] as a JSONL chat log (the format clips.simulate reads)."""
    vid = video_id(url)
    tag = f"{int(start)}-{int(end)}" if end is not None else "all"
    log_path = workdir / f"{vid}.chat.{tag}.jsonl"
    if log_path.is_file():
        return log_path
    until = end + CHAT_MARGIN_SECONDS if end is not None else None
    raw = download_chat(url, workdir, until=until)
    rows = parse_chat_replay(raw, start, end)
    if not rows:
        raise YouTubeVodError(f"the chat replay has no text messages between "
                              f"{fmt_time(start)} and {fmt_time(end) if end else 'the end'}")
    log.info("parsed %d chat messages from %s", len(rows), raw.name)
    return write_chat_log(rows, log_path)


# --------------------------------------------------------------------------
# Moments
# --------------------------------------------------------------------------

def find_moments(chat: list[dict], detection: Detection, start: float, end: float,
                 ignore_users: tuple[str, ...] = ()) -> list[Spike]:
    """Replay chat (absolute stream seconds) and return every spike that starts in [start, end].

    The detector learns "normal" from the first minutes of the window (warm-up), so
    pass chat from a few minutes before `start` if you want spikes right at the start.
    """
    detector = SpikeDetector(detection, ignore_users=ignore_users)
    spikes, i = [], 0
    first = int(chat[0]["t"]) if chat else int(start)
    for second in range(first, int(end) + 60):
        while i < len(chat) and chat[i]["t"] <= second:
            row = chat[i]
            detector.add(float(row["t"]), row.get("text", ""), int(row.get("emotes", 0)),
                         str(row.get("user", "")))
            i += 1
        spike = detector.check(float(second))
        if spike and start <= spike.start <= end:
            spikes.append(spike)
    return spikes


def chat_is_capped(chat: list[dict], start: float, end: float, window: int = 8) -> bool:
    """True when chat speed hardly moves (busy streams: YouTube caps the replay rate).

    Then "3x faster than normal" never happens and speed cannot find moments; what
    people type still changes, so find_reaction_moments() is used instead.
    """
    counts = collections.Counter(int(r["t"] // window) for r in chat if start <= r["t"] <= end)
    rates = [counts.get(i, 0) / window for i in range(int(start // window), int(end // window))]
    if len(rates) < 20:
        return False
    med = statistics.median(rates)
    p99 = sorted(rates)[int(len(rates) * 0.99) - 1]
    return med >= 5 and p99 < 2.0 * med


@dataclass
class ChatMoment:
    """A burst of reactions found by content, with the fields Moment / the CLI read from a Spike."""
    start: float
    end: float
    peak: float
    zscore: float
    rate_ratio: float
    messages: int
    score: float
    accepted: bool
    category: str
    unique_chatters: int
    clip_requests: int
    top_reactions: list[tuple[str, int]]
    samples: list[str]
    peak_rate: float
    baseline: float

    def explain(self) -> str:
        top = ", ".join(f"{k} x{n}" for k, n in self.top_reactions[:3])
        return (f"score {self.score:.0f} ({self.category}): reactions {self.zscore:.1f} sd above normal "
                f"for {self.end - self.start:.0f}s; chat {self.rate_ratio:.1f}x speed; "
                f"{self.unique_chatters} people; top: {top}")


def find_reaction_moments(chat: list[dict], start: float, end: float, *, window: int = 10,
                          trigger_z: float = 2.5, min_score: float = 40,
                          cooldown_seconds: int = 60, baseline_windows: int = 60) -> list[ChatMoment]:
    """Find moments where a much larger share of chat than usual is reacting.

    Per `window` seconds: the share of messages that are reactions (laughing, hype,
    shock, fail, "clip it"), weighted like the speed detector does. A window
    triggers when that share is `trigger_z` robust standard deviations above the
    median of the previous `baseline_windows` windows (about 10 minutes). Windows
    next to each other join into one moment. Score 0-100 rises with the peak z-score
    (z 3 = 50, z 5 = 100) plus a little for chat also speeding up.
    """
    rows = [r for r in chat if r["t"] <= end + window]
    first = int((rows[0]["t"] if rows else start) // window)
    last = int(end // window)
    n = collections.Counter()
    weight = collections.Counter()
    cats: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    keys: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    users: dict[int, set] = collections.defaultdict(set)
    clips = collections.Counter()
    samples: dict[int, list[str]] = collections.defaultdict(list)
    for r in rows:
        i = int(r["t"] // window)
        c = classify(r.get("text", ""), int(r.get("emotes", 0)))
        n[i] += 1
        if c.category in ("funny", "hype", "shock", "fail", "clip"):
            weight[i] += c.score
            cats[i][c.category] += 1
            keys[i][c.key] += 1
            users[i].add(r.get("user", ""))
            if c.category == "clip":
                clips[i] += 1
            if len(samples[i]) < 12:
                samples[i].append(r.get("text", ""))

    share = {i: weight[i] / n[i] if n[i] else 0.0 for i in range(first, last + 1)}
    rate = {i: n[i] / window for i in range(first, last + 1)}
    hot: list[tuple[int, float, float]] = []      # (window, z, rate ratio)
    for i in range(max(first, int(start // window)), last + 1):
        past = [share[j] for j in range(max(first, i - baseline_windows), i)]
        if len(past) < 12:
            continue
        med = statistics.median(past)
        mad = statistics.median([abs(s - med) for s in past]) or 0.01
        z = (share[i] - med) / (1.4826 * mad)
        rmed = statistics.median([rate[j] for j in range(max(first, i - baseline_windows), i)]) or 1.0
        if z >= trigger_z and n[i] >= 10:
            hot.append((i, z, rate[i] / rmed))

    groups: list[list[tuple[int, float, float]]] = []
    for h in hot:
        if groups and h[0] - groups[-1][-1][0] <= 2:
            groups[-1].append(h)
        else:
            groups.append([h])

    moments: list[ChatMoment] = []
    last_accepted_end = -1e9
    for g in groups:
        idx = [h[0] for h in g]
        peak_i, peak_z, _ = max(g, key=lambda h: h[1])
        ratio = max(h[2] for h in g)
        score = max(0.0, min(100.0, 25.0 * (peak_z - 1.0) + (10.0 if ratio >= 1.3 else 0.0)))
        cat = sum((cats[i] for i in idx), collections.Counter())
        key = sum((keys[i] for i in idx), collections.Counter())
        who = set().union(*(users[i] for i in idx))
        m_start, m_end = idx[0] * window, (idx[-1] + 1) * window
        accepted = score >= min_score and m_start - last_accepted_end >= cooldown_seconds
        if accepted:
            last_accepted_end = m_end
        moments.append(ChatMoment(
            start=float(m_start), end=float(m_end), peak=float(peak_i * window), zscore=peak_z,
            rate_ratio=ratio, messages=sum(n[i] for i in idx), score=round(score, 1),
            accepted=accepted, category=cat.most_common(1)[0][0] if cat else "mixed",
            unique_chatters=len(who), clip_requests=sum(clips[i] for i in idx),
            top_reactions=[(k, c) for k, c in key.most_common(5) if k],
            samples=samples[peak_i][:8], peak_rate=rate[peak_i],
            baseline=rate[peak_i] / ratio if ratio else rate[peak_i]))
    return moments


# --------------------------------------------------------------------------
# Video
# --------------------------------------------------------------------------

def download_section(url: str, t0: float, t1: float, out: Path, max_height: int = 1080) -> Path:
    """Download only [t0, t1] of the stream to out (mp4, original audio)."""
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    stem = out.with_suffix("")
    last: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):    # YouTube / network hiccups are common
        try:
            _ytdlp(["-f", f"bv*[height<={max_height}]+ba/b[height<={max_height}]",
                    "--download-sections", f"*{t0:.1f}-{t1:.1f}", "--force-keyframes-at-cuts",
                    "--merge-output-format", "mp4",
                    "--ffmpeg-location", str(Path(media.ffmpeg()).parent),
                    "-o", f"{stem}.%(ext)s", url], timeout=1800)
            if out.is_file():
                return out
            last = YouTubeVodError(f"yt-dlp finished but {out.name} is missing")
        except YouTubeVodError as e:
            last = e
        for leftover in out.parent.glob(f"{stem.name}.*"):  # partial files from the failed try
            if leftover != out:
                leftover.unlink(missing_ok=True)
        if attempt < DOWNLOAD_ATTEMPTS:
            log.warning("download of %s failed (try %d/%d): %s", out.name, attempt,
                        DOWNLOAD_ATTEMPTS, last)
            time.sleep(10 * attempt)
    raise last  # type: ignore[misc]


def fetch_video(url: str, workdir: Path, max_height: int = 1080) -> Path:
    """Download the whole stream video as one mp4 (used by `clips youtube`)."""
    vid = video_id(url)
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / f"{vid}.mp4"
    if out.is_file():
        return out
    _ytdlp(["-f", f"bv*[height<={max_height}]+ba/b[height<={max_height}]",
            "--merge-output-format", "mp4", "--ffmpeg-location", str(Path(media.ffmpeg()).parent),
            "-o", str(workdir / "%(id)s.%(ext)s"), url], timeout=6 * 3600)
    if not out.is_file():
        raise YouTubeVodError("yt-dlp finished but the video file is missing")
    return out


@dataclass
class Moment:
    spike: "Spike | ChatMoment"
    t0: float
    t1: float
    path: Path | None = None

    def as_dict(self) -> dict:
        s = self.spike
        return {"clip": self.path.name if self.path else None,
                "window": [fmt_time(self.t0), fmt_time(self.t1)], "window_seconds": [self.t0, self.t1],
                "chat_reacted_at": fmt_time(s.start), "score": round(s.score, 1),
                "accepted": s.accepted, "reaction": s.category,
                "chat_speed_x_normal": round(s.peak_rate / s.baseline, 1) if s.baseline else None,
                "unique_chatters": s.unique_chatters, "clip_requests": s.clip_requests,
                "top_reactions": s.top_reactions, "chat_samples": s.samples[:8],
                "explain": s.explain()}

"""Queue finished clips from a `clips stream` folder as upload jobs.

`clips stream` writes caption-box Shorts plus a manifest.json per hour window
into data/stream_clips/<video id>/hour_NN/. Those are LOCAL TEST output and not
pipeline jobs. This module turns the ones you pick into jobs for a clip channel
whose config lists the streamer (with permission), so the publish stage can
upload them.

- Best first (or random order with shuffle): moments are ordered by score, then by how many people reacted,
  so the strongest clips get the earliest publish slots.
- Hour windows overlap, so the same moment can be in two hours; a moment whose
  chat reaction is within DUPLICATE_SECONDS of one already picked is skipped.
- Moments flagged check_by_eye are skipped unless include_check_by_eye is set.
- Importing twice is safe: each clip is recorded with an import key and skipped
  the second time.
"""
from __future__ import annotations

import json
import logging
import random
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from network import db
from network.config import ClipChannelConfig, Settings, Streamer
from network.files import job_dir, rel_to_data, write_json_atomic
from network.stages.package import _hashtag, clean_description, clean_tags, clean_title

DUPLICATE_SECONDS = 45
VARIETY_WINDOW = 10      # a clip is compared with the last this many in the queue (about a day)
NEAR_IN_STREAM = 20 * 60  # moments this close in the stream tend to look alike
THEMES = {
    "crash": ("crash", "crashes", "crashed", "wreck", "flip", "totals"),
    "cops": ("cop", "cops", "police", "wanted", "chase", "chased", "fleeing", "flee", "escape", "escaping",
             "star", "stars", "busted", "arrest", "arrested"),
    "death": ("wasted", "dies", "die", "died", "dead", "death", "killed", "fail", "fails", "failed"),
    "heist": ("heist", "robbery", "rob", "robs", "robbing", "bank", "vault", "jackpot", "store", "steal",
              "steals", "stealing", "stole"),
    "air": ("helicopter", "heli", "plane", "jet", "fly", "flying", "landing", "parachute", "skydive"),
    "vehicle": ("car", "supercar", "corvette", "bike", "motorcycle", "boat", "drive", "drives", "driving",
                "highway", "race", "racing", "truck"),
    "guns": ("gun", "guns", "gunfight", "shootout", "shoot", "shoots", "shooting", "shot", "sniper"),
    "music": ("music", "song", "dance", "dancing", "dancers", "nightclub", "club", "vibes", "studio",
              "rapper", "lil", "wayne", "sing", "singing"),
    "story": ("cutscene", "scene", "mission", "story", "character", "lucia", "jason"),
    "stream": ("sleep", "hours", "live", "chat", "stream", "subs", "subscribers", "viewers", "night",
               "bed", "tired", "awake"),
    "funny": ("funny", "laugh", "laughs", "laughing", "prank", "troll", "trolls", "weird", "naked", "dog"),
}
STOPWORDS = {"speed", "speeds", "gta", "6", "vi", "the", "a", "an", "in", "on", "of", "to", "and", "his",
             "he", "him", "with", "at", "for", "after", "is", "this", "that", "it", "as", "gets", "while",
             "during", "from", "by", "s", "ishowspeed", "reacts", "reaction", "goes", "into", "out", "up"}
log = logging.getLogger("clips.import")


@dataclass
class Candidate:
    key: str
    video: Path
    hour: str
    score: float
    unique_chatters: int
    reacted_at: float
    clip_start: float
    title: str
    caption: str
    moment: dict


@dataclass
class ImportReport:
    picked: list[Candidate] = field(default_factory=list)
    imported: list[tuple[int, Candidate]] = field(default_factory=list)
    already: list[Candidate] = field(default_factory=list)
    duplicates: list[Candidate] = field(default_factory=list)
    check_by_eye: list[Candidate] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def scan(folder: Path) -> tuple[list[Candidate], list[str]]:
    """Every accepted moment with a rendered video, from all hour_*/manifest.json files."""
    found, missing = [], []
    stream_id = folder.name
    for manifest in sorted(folder.glob("hour_*/manifest.json")):
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        for m in doc.get("moments", []):
            if not m.get("accepted") or not m.get("video"):
                continue
            video = manifest.parent / m["video"]
            if not video.is_file():
                missing.append(str(video))
                continue
            found.append(Candidate(
                key=f"{stream_id}/{manifest.parent.name}/{m['video']}",
                video=video, hour=manifest.parent.name,
                score=float(m.get("score") or 0), unique_chatters=int(m.get("unique_chatters") or 0),
                reacted_at=float(m.get("chat_reacted_at_seconds") or 0),
                clip_start=float((m.get("window_seconds") or [0])[0]),
                title=m.get("title") or "", caption=m.get("caption") or "", moment=m,
            ))
    found.sort(key=lambda c: (-c.score, -c.unique_chatters, c.reacted_at))
    return found, missing


def _words(c: Candidate) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", f"{c.title} {c.caption}".lower()))


def themes(c: Candidate) -> set[str]:
    w = _words(c)
    return {t for t, keys in THEMES.items() if w & set(keys)}


def _title_words(c: Candidate) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", c.title.lower())) - STOPWORDS


def similarity(a: Candidate, b: Candidate) -> float:
    """Higher = the two clips would feel more alike back to back."""
    score = 3.0 * len(themes(a) & themes(b))
    if abs(a.reacted_at - b.reacted_at) < NEAR_IN_STREAM:
        score += 2.0
    if a.hour == b.hour:
        score += 1.0
    score += 1.0 * len(_title_words(a) & _title_words(b))
    return score


def varied_order(picked: list[Candidate], history: list[Candidate] | None = None,
                 rng: random.Random | None = None) -> list[Candidate]:
    """Greedy: each next clip is the one least like the last VARIETY_WINDOW clips
    (recent ones count more); ties are broken at random so the order still feels random."""
    rng = rng or random.Random()
    pool = list(picked)
    rng.shuffle(pool)
    recent = list(history or [])[-VARIETY_WINDOW:]
    out: list[Candidate] = []
    while pool:
        def cost(c: Candidate) -> tuple[float, float]:
            total = sum(similarity(c, r) * (1.0 + i / VARIETY_WINDOW) for i, r in enumerate(recent))
            return total, rng.random()
        best = min(pool, key=cost)
        pool.remove(best)
        out.append(best)
        recent = (recent + [best])[-VARIETY_WINDOW:]
    return out


def _imported_keys(conn: sqlite3.Connection, channel_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT a.meta FROM artifacts a JOIN jobs j ON j.id = a.job_id "
        "WHERE a.kind = 'video' AND j.channel_id = ?", (channel_id,)).fetchall()
    keys = set()
    for (meta,) in rows:
        try:
            k = json.loads(meta or "{}").get("import_key")
        except json.JSONDecodeError:
            continue
        if k:
            keys.add(k)
    return keys


def _imported_keys_in_order(conn: sqlite3.Connection, channel_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT a.meta FROM artifacts a JOIN jobs j ON j.id = a.job_id "
        "WHERE a.kind = 'video' AND j.channel_id = ? AND j.state NOT IN ('failed', 'rejected') "
        "ORDER BY COALESCE(j.scheduled_publish_at, '9999'), j.id", (channel_id,)).fetchall()
    keys = []
    for (meta,) in rows:
        try:
            k = json.loads(meta or "{}").get("import_key")
        except json.JSONDecodeError:
            continue
        if k:
            keys.append(k)
    return keys


def metadata(streamer: Streamer, stream_id: str, c: Candidate, hashtags: list[str]) -> dict:
    name = streamer.name
    title = clean_title(f"{c.title.strip()} | {name}" if c.title.strip() else f"{name} stream moment")
    link = f"https://youtu.be/{stream_id}?t={int(c.clip_start)}"
    tags_line = " ".join(f"#{h}" for h in dict.fromkeys(
        ["shorts", *(_hashtag(h) for h in hashtags), _hashtag(name)]) if h)
    description = clean_description(
        f"{c.caption.strip()}\n\n"
        f"From {name}'s live stream: {link}\n"
        f"Footage: {streamer.credit_text}. Used with permission. All credit to {name}, go follow them!\n\n"
        f"{tags_line}"
    )
    tags = clean_tags([name, streamer.login, *hashtags, f"{name} clips", "live stream highlights", "shorts"])
    return {"title": title, "description": description, "tags": tags}


def import_folder(
    conn: sqlite3.Connection,
    settings: Settings,
    channel: ClipChannelConfig,
    streamer: Streamer,
    folder: Path,
    min_score: float = 90,
    limit: int | None = None,
    approve: bool = False,
    preview: bool = False,
    include_check_by_eye: bool = False,
    hashtags: list[str] | None = None,
    shuffle: bool = False,
    varied: bool = False,
) -> ImportReport:
    if streamer not in channel.streamers:
        raise ValueError(f"{streamer.login} is not a streamer of channel {channel.id}")
    report = ImportReport()
    candidates, report.missing = scan(folder)
    done = _imported_keys(conn, channel.id)
    taken_times: list[float] = []

    for c in candidates:
        if c.score < min_score:
            continue
        if limit is not None and len(report.picked) >= limit:
            break
        if c.key in done:
            report.already.append(c)
            taken_times.append(c.reacted_at)
            continue
        if any(abs(c.reacted_at - t) < DUPLICATE_SECONDS for t in taken_times):
            report.duplicates.append(c)
            continue
        if c.moment.get("check_by_eye") and not include_check_by_eye:
            report.check_by_eye.append(c)
            continue
        taken_times.append(c.reacted_at)
        report.picked.append(c)

    if varied:
        by_key = {c.key: c for c in candidates}
        history = [by_key[k] for k in _imported_keys_in_order(conn, channel.id) if k in by_key]
        report.picked = varied_order(report.picked, history)
    elif shuffle:
        random.shuffle(report.picked)
    if preview:
        return report

    state = "final_approved" if approve else "packaged"
    for c in report.picked:
        meta = metadata(streamer, folder.name, c, hashtags or [])
        job_id = db.create_job(conn, channel.id, state=state)
        d = job_dir(settings, job_id)
        d.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(c.video, d / "final.mp4")
        except OSError as e:
            db.mark_failed(conn, job_id, f"could not copy clip: {e}")
            raise
        meta_path = d / "metadata.json"
        write_json_atomic(meta_path, {"job_id": job_id, "channel_id": channel.id, **meta,
                                      "category_id": str(channel.publish.category_id),
                                      "language": channel.language, "created_at": db.now_utc()})
        video_meta = {
            "source": "stream_clips_import", "import_key": c.key, "imported_from": str(c.video),
            "streamer": streamer.login, "streamer_name": streamer.name, "credit": streamer.credit_text,
            "permission_evidence": streamer.permission.evidence,
            "permission_conditions": streamer.permission.conditions,
            "stream_id": folder.name, "score": c.score, "moment": c.moment,
        }
        ts = db.now_utc()
        with conn:
            conn.execute("INSERT INTO artifacts (job_id, kind, path, meta, created_at) VALUES (?, 'video', ?, ?, ?)",
                         (job_id, rel_to_data(settings, d / "final.mp4"),
                          json.dumps(video_meta, ensure_ascii=False), ts))
            conn.execute("INSERT INTO artifacts (job_id, kind, path, meta, created_at) VALUES (?, 'metadata', ?, ?, ?)",
                         (job_id, rel_to_data(settings, meta_path),
                          json.dumps({"title": meta["title"]}, ensure_ascii=False), ts))
        report.imported.append((job_id, c))
        log.info("[%s] imported %s as job %d (%s)", channel.id, c.key, job_id, state)
    return report

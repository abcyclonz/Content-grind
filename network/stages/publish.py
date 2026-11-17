"""Stage 7.10: upload final_approved jobs to YouTube.

- Clips go up straight away; Shorts get the next free publish slot from the
  channel config and are uploaded private with status.publishAt.
- Quota: videos.insert has its own bucket of 100 calls/day (resets midnight
  Pacific). Calls are counted in the costs table and the stage stops cleanly
  before the limit.
- Crash safety: before uploading, an upload.json marker with an internal tag is
  written. If a job is retried after a crash, our recent uploads are searched
  for that tag first, so a video is never uploaded twice.
- DRY_RUN: nothing is uploaded; the report says what would have been sent.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from network import db
from network.config import AnyChannel, ChannelConfig, ClipChannelConfig, Settings
from network.files import job_dir, write_json_atomic
from network.providers.youtube import QuotaExceeded, YouTubeClient, YouTubeError
from network.text import ignore_tokens, topic_hash

UPLOADS_PER_DAY = 100            # default videos.insert bucket (verified 2026-11-17)
MIN_LEAD = timedelta(minutes=20)  # a publishAt must be comfortably in the future
PACIFIC = ZoneInfo("America/Los_Angeles")
log = logging.getLogger("publish")


@dataclass
class PublishReport:
    channel_id: str
    uploaded: list[tuple[int, str, str | None]] = field(default_factory=list)  # job, video id, publish_at
    would_upload: list[tuple[int, str]] = field(default_factory=list)
    promoted: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        lines = [f"[publish] {self.channel_id}:"]
        for job, vid, at in self.uploaded:
            lines.append(f"  job {job} uploaded: https://youtu.be/{vid}"
                         + (f" (scheduled {at})" if at else ""))
        for job, title in self.would_upload:
            lines.append(f"  job {job} would upload (DRY_RUN): {title}")
        if self.promoted:
            lines.append(f"  scheduled -> published: {self.promoted}")
        lines += [f"  job {j} failed: {e}" for j, e in self.failed]
        lines += [f"  WARNING: {w}" for w in self.warnings]
        if self.note:
            lines.append(f"  {self.note}")
        if len(lines) == 1:
            lines[0] += " nothing to upload; nothing to do"
        return "\n".join(lines)


def uploads_today(conn: sqlite3.Connection, now: datetime) -> int:
    """videos.insert calls since midnight Pacific, across all channels (the quota is per project)."""
    start = now.astimezone(PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0)
    since = start.astimezone(timezone.utc).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT COUNT(*) FROM costs WHERE provider = 'youtube' AND operation = 'videos.insert' "
        "AND created_at >= ?", (since,),
    ).fetchone()[0]


def next_publish_slot(conn: sqlite3.Connection, channel: ChannelConfig, now: datetime) -> datetime:
    taken = {r[0] for r in conn.execute(
        "SELECT scheduled_publish_at FROM jobs WHERE channel_id = ? AND scheduled_publish_at IS NOT NULL",
        (channel.id,))}
    local_now = now.astimezone(channel.tz)
    for day in range(0, 30):
        d = (local_now + timedelta(days=day)).date()
        for hhmm in sorted(channel.publish_times_local):
            h, m = map(int, hhmm.split(":"))
            slot = datetime(d.year, d.month, d.day, h, m, tzinfo=channel.tz)
            iso = slot.astimezone(timezone.utc).isoformat(timespec="seconds")
            if slot >= now + MIN_LEAD and iso not in taken:
                return slot
    raise RuntimeError("no free publish slot in the next 30 days")


def synthetic_flag(channel: AnyChannel) -> bool:
    if channel.publish.contains_synthetic_media is not None:
        return channel.publish.contains_synthetic_media
    return isinstance(channel, ChannelConfig)  # Shorts use AI voice/images; clips are real footage


def build_body(channel: AnyChannel, meta: dict, job_id: int, publish_at: datetime | None) -> dict:
    tag = upload_tag(channel.id, job_id)
    privacy = "private" if publish_at else channel.publish.privacy
    status = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": channel.publish.made_for_kids,
        "containsSyntheticMedia": synthetic_flag(channel),
    }
    if publish_at:
        status["publishAt"] = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "snippet": {
            "title": meta["title"],
            "description": meta["description"],
            "tags": list(meta.get("tags", [])) + [tag],
            "categoryId": str(meta.get("category_id") or channel.publish.category_id),
            "defaultLanguage": channel.language,
            "defaultAudioLanguage": channel.language,
        },
        "status": status,
    }


def upload_tag(channel_id: str, job_id: int) -> str:
    return f"cn-{channel_id}-{job_id}"


def _promote_due(conn: sqlite3.Connection, channel: AnyChannel, now: datetime, report: PublishReport) -> None:
    iso_now = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    for job in db.jobs_in_state(conn, "scheduled", channel.id):
        if job["scheduled_publish_at"] is None or job["scheduled_publish_at"] <= iso_now:
            if db.transition(conn, job["id"], "scheduled", "published"):
                report.promoted.append(job["id"])


def _record_topic(conn: sqlite3.Connection, channel: AnyChannel, job: sqlite3.Row, title: str) -> None:
    if job["idea_id"]:
        row = conn.execute("SELECT title FROM ideas WHERE id = ?", (job["idea_id"],)).fetchone()
        title = row[0] if row else title
    ignore = ignore_tokens(channel.sources.keywords) if isinstance(channel, ChannelConfig) else frozenset()
    with conn:
        conn.execute("INSERT OR IGNORE INTO published_topics VALUES (?, ?, ?, ?)",
                     (channel.id, topic_hash(title, ignore), title, db.now_utc()))


def run(
    conn: sqlite3.Connection,
    channel: AnyChannel,
    settings: Settings,
    client: YouTubeClient | None = None,
    now: datetime | None = None,
    schedule: bool = True,
) -> PublishReport:
    """schedule=False uploads Shorts without publishAt (stays at the config privacy), e.g. for tests."""
    report = PublishReport(channel.id)
    now = now or datetime.now(timezone.utc)
    _promote_due(conn, channel, now, report)

    for job in db.jobs_in_state(conn, "final_approved", channel.id):
        video = db.get_artifact(conn, job["id"], "video")
        meta_art = db.get_artifact(conn, job["id"], "metadata")
        if not video or not meta_art:
            msg = "missing video or metadata artifact"
            db.mark_failed(conn, job["id"], msg)
            report.failed.append((job["id"], msg))
            continue
        meta = json.loads((settings.data_dir / Path(meta_art["path"])).read_text(encoding="utf-8"))
        video_path = settings.data_dir / Path(video["path"])

        if settings.dry_run:
            report.would_upload.append((job["id"], meta["title"]))
            continue
        if uploads_today(conn, now) >= UPLOADS_PER_DAY:
            report.note = f"stopped: {UPLOADS_PER_DAY} uploads today already (resets midnight Pacific)"
            break

        try:
            client = client or YouTubeClient(_credentials(channel))
            publish_at = (next_publish_slot(conn, channel, now)
                          if schedule and channel.publish.use_publish_times
                          and isinstance(channel, ChannelConfig) else None)
            body = build_body(channel, meta, job["id"], publish_at)
            marker = job_dir(settings, job["id"]) / "upload.json"

            response = None
            if marker.exists():  # an earlier attempt may have finished before a crash
                response = client.find_upload_with_tag(upload_tag(channel.id, job["id"]))
                if response:
                    log.info("job %d: found the earlier upload %s; not uploading again", job["id"], response["id"])
                    earlier = json.loads(marker.read_text(encoding="utf-8")).get("publish_at")
                    publish_at = datetime.fromisoformat(earlier.replace("Z", "+00:00")) if earlier else None
                    body = build_body(channel, meta, job["id"], publish_at)
            if response is None:
                write_json_atomic(marker, {"tag": upload_tag(channel.id, job["id"]), "attempted_at": db.now_utc(),
                                           "publish_at": body["status"].get("publishAt")})
                db.log_cost(conn, channel.id, "youtube", "videos.insert", 0.0, units=1, job_id=job["id"])
                response = client.upload(video_path, body)
        except QuotaExceeded as e:
            report.note = f"stopped: {e}"
            break
        except (YouTubeError, OSError, RuntimeError) as e:
            msg = f"{type(e).__name__}: {e}"
            db.mark_failed(conn, job["id"], msg)
            report.failed.append((job["id"], msg))
            continue

        video_id = response["id"]
        got = (response.get("status") or {}).get("privacyStatus")
        wanted = body["status"]["privacyStatus"]
        if got and got != wanted:
            report.warnings.append(f"job {job['id']}: asked for {wanted}, YouTube set {got} "
                                   "(check the channel's upload defaults and restrictions)")
        publish_iso = publish_at.astimezone(timezone.utc).isoformat(timespec="seconds") if publish_at else None
        with conn:
            conn.execute(
                "UPDATE jobs SET state = 'scheduled', youtube_video_id = ?, scheduled_publish_at = ?, "
                "updated_at = ? WHERE id = ? AND state = 'final_approved'",
                (video_id, publish_iso, db.now_utc(), job["id"]),
            )
        write_json_atomic(job_dir(settings, job["id"]) / "upload.json",
                          {"tag": upload_tag(channel.id, job["id"]), "video_id": video_id,
                           "privacy": got, "publish_at": publish_iso, "uploaded_at": db.now_utc()})
        if publish_at is None:
            db.transition(conn, job["id"], "scheduled", "published")
        _record_topic(conn, channel, job, meta["title"])
        report.uploaded.append((job["id"], video_id, publish_iso))
    return report


def _credentials(channel: AnyChannel):
    from network.providers.youtube import load_credentials
    return load_credentials(channel.id)

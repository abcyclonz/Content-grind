import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from conftest import FIXTURES, with_changes
from network import db
from network.config import load_channel
from network.providers.youtube import QuotaExceeded, YouTubeError
from network.stages import publish

NOW = datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc)  # 11:30 in Asia/Kolkata


@pytest.fixture
def clip_channel():
    return load_channel(FIXTURES / "channels" / "clips_valid.yaml")


@pytest.fixture
def live(settings):
    return replace(settings, dry_run=False)


def _ready_job(conn, settings, channel, idea_id=None):
    job = db.create_job(conn, channel.id, state="final_approved")
    if idea_id:
        with conn:
            conn.execute("UPDATE jobs SET idea_id = ? WHERE id = ?", (idea_id, job))
    d = settings.jobs_dir / str(job)
    d.mkdir(parents=True)
    (d / "final.mp4").write_bytes(b"video")
    (d / "metadata.json").write_text(json.dumps(
        {"title": f"Title {job}", "description": "Desc", "tags": ["a"], "category_id": "20"}), encoding="utf-8")
    with conn:
        for kind, name in (("video", "final.mp4"), ("metadata", "metadata.json")):
            conn.execute("INSERT INTO artifacts (job_id, kind, path, meta, created_at) VALUES (?, ?, ?, '{}', 'now')",
                         (job, kind, f"jobs/{job}/{name}"))
    return job


class FakeYT:
    def __init__(self, privacy=None, fail=None, existing=None):
        self.uploads, self.privacy, self.fail, self.existing = [], privacy, fail, existing
        self.searched = []

    def upload(self, path, body):
        if self.fail:
            raise self.fail
        self.uploads.append((path, body))
        return {"id": f"vid{len(self.uploads)}",
                "status": {"privacyStatus": self.privacy or body["status"]["privacyStatus"]}}

    def find_upload_with_tag(self, tag):
        self.searched.append(tag)
        return self.existing


def test_clip_uploads_immediately_and_is_published(conn, live, clip_channel):
    job = _ready_job(conn, live, clip_channel)
    yt = FakeYT()
    report = publish.run(conn, clip_channel, live, client=yt, now=NOW)
    assert report.uploaded == [(job, "vid1", None)]
    row = db.get_job(conn, job)
    assert (row["state"], row["youtube_video_id"]) == ("published", "vid1")
    body = yt.uploads[0][1]
    assert body["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                              "containsSyntheticMedia": False}     # real footage
    assert body["snippet"]["categoryId"] == "20"
    assert f"cn-{clip_channel.id}-{job}" in body["snippet"]["tags"]
    assert conn.execute("SELECT COUNT(*) FROM costs WHERE operation = 'videos.insert'").fetchone()[0] == 1
    assert publish.run(conn, clip_channel, live, client=yt, now=NOW).uploaded == []  # idempotent


def test_short_is_scheduled_into_next_free_slot(conn, live, channel):
    channel = with_changes(channel, publish={"use_publish_times": True})
    a = _ready_job(conn, live, channel)
    b = _ready_job(conn, live, channel)
    yt = FakeYT()
    publish.run(conn, channel, live, client=yt, now=NOW)
    (s1, s2) = [db.get_job(conn, j)["scheduled_publish_at"] for j in (a, b)]
    # 11:30 IST now: next slots are 14:00 and 19:00 IST today
    assert (s1, s2) == ("2026-11-17T08:30:00+00:00", "2026-11-17T13:30:00+00:00")
    st = yt.uploads[0][1]["status"]
    assert st["privacyStatus"] == "private" and st["publishAt"] == "2026-11-17T08:30:00Z"
    assert st["containsSyntheticMedia"] is True                      # AI voice/images
    assert db.get_job(conn, a)["state"] == "scheduled"
    publish.run(conn, channel, live, client=yt, now=NOW + timedelta(hours=3))
    assert db.get_job(conn, a)["state"] == "published" and db.get_job(conn, b)["state"] == "scheduled"


def test_publish_times_switch_off_keeps_private(conn, live, channel):
    _ready_job(conn, live, channel)
    ch = with_changes(channel, publish={"use_publish_times": False})
    yt = FakeYT()
    publish.run(conn, ch, live, client=yt, now=NOW)
    assert "publishAt" not in yt.uploads[0][1]["status"]


def test_short_without_schedule_stays_private(conn, live, channel):
    job = _ready_job(conn, live, channel)
    yt = FakeYT()
    publish.run(conn, channel, live, client=yt, now=NOW, schedule=False)
    st = yt.uploads[0][1]["status"]
    assert st["privacyStatus"] == "private" and "publishAt" not in st
    assert db.get_job(conn, job)["state"] == "published"


def test_dry_run_uploads_nothing(conn, settings, clip_channel):
    job = _ready_job(conn, settings, clip_channel)
    yt = FakeYT()
    report = publish.run(conn, clip_channel, settings, client=yt, now=NOW)
    assert yt.uploads == [] and report.would_upload == [(job, f"Title {job}")]
    assert db.get_job(conn, job)["state"] == "final_approved"


def test_quota_guard_stops_cleanly(conn, live, clip_channel):
    job = _ready_job(conn, live, clip_channel)
    for _ in range(publish.UPLOADS_PER_DAY):
        db.log_cost(conn, "other", "youtube", "videos.insert", 0.0, units=1)
    yt = FakeYT()
    report = publish.run(conn, clip_channel, live, client=yt, now=datetime.now(timezone.utc))
    assert yt.uploads == [] and "uploads today" in report.note
    assert db.get_job(conn, job)["state"] == "final_approved"


def test_quota_error_from_youtube_does_not_fail_job(conn, live, clip_channel):
    job = _ready_job(conn, live, clip_channel)
    report = publish.run(conn, clip_channel, live, client=FakeYT(fail=QuotaExceeded("uploadLimitExceeded")), now=NOW)
    assert "uploadLimitExceeded" in report.note
    assert db.get_job(conn, job)["state"] == "final_approved"


def test_other_error_fails_job_then_retry_finds_earlier_upload(conn, live, clip_channel):
    job = _ready_job(conn, live, clip_channel)
    publish.run(conn, clip_channel, live, client=FakeYT(fail=YouTubeError("HTTP 400 invalidTitle")), now=NOW)
    assert db.get_job(conn, job)["state"] == "failed"
    db.retry(conn, job)
    # Pretend the first attempt actually reached YouTube before the error.
    yt = FakeYT(existing={"id": "already", "status": {"privacyStatus": "private"}})
    report = publish.run(conn, clip_channel, live, client=yt, now=NOW)
    assert yt.uploads == [] and yt.searched == [f"cn-{clip_channel.id}-{job}"]
    assert report.uploaded == [(job, "already", None)]


def test_privacy_mismatch_is_reported(conn, live, clip_channel):
    _ready_job(conn, live, clip_channel)
    ch = with_changes(clip_channel, publish={"privacy": "public"})
    report = publish.run(conn, ch, live, client=FakeYT(privacy="private"), now=NOW)
    assert any("asked for public, YouTube set private" in w for w in report.warnings)


def test_published_topic_recorded_from_idea(conn, live, channel):
    with conn:
        idea = conn.execute("INSERT INTO ideas (channel_id, title, angle, created_at) "
                            "VALUES ('gta6', 'Pre-load date confirmed', 'x', 'now')").lastrowid
    _ready_job(conn, live, channel, idea_id=idea)
    publish.run(conn, channel, live, client=FakeYT(), now=NOW)
    assert conn.execute("SELECT title FROM published_topics").fetchone()[0] == "Pre-load date confirmed"

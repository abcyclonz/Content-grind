import json

import pytest

from conftest import FIXTURES
from network import db
from network.clips import importer
from network.config import load_channel


@pytest.fixture
def clip_channel():
    return load_channel(FIXTURES / "channels" / "clips_valid.yaml")


def _moment(video, score, reacted, chatters=100, **extra):
    return {"accepted": True, "video": video, "score": score, "unique_chatters": chatters,
            "chat_reacted_at_seconds": reacted, "window_seconds": [reacted - 25, reacted + 10],
            "title": f"Title {video}", "caption": f"Caption {video} 😱", **extra}


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "abc123"
    hours = {
        "hour_01": [_moment("01_a.mp4", 100, 500, chatters=50), _moment("02_b.mp4", 60, 1500)],
        # 3400 and 3410 are the same moment seen by two overlapping hour windows
        "hour_02": [_moment("01_c.mp4", 100, 3400, chatters=400), _moment("02_d.mp4", 95, 5000,
                                                                          check_by_eye=True)],
        "hour_03": [_moment("01_e.mp4", 92, 3410), _moment("02_missing.mp4", 99, 9000),
                    {"accepted": False, "video": "x.mp4", "score": 100}],
    }
    for hour, moments in hours.items():
        d = root / hour
        d.mkdir(parents=True)
        for m in moments:
            if "missing" not in m["video"]:
                (d / m["video"]).write_bytes(b"mp4 " + m["video"].encode())
        (d / "manifest.json").write_text(json.dumps({"moments": moments}), encoding="utf-8")
    return root


def test_preview_orders_best_first_and_filters(conn, settings, clip_channel, folder):
    s = clip_channel.streamers[0]
    rep = importer.import_folder(conn, settings, clip_channel, s, folder, min_score=90, preview=True)
    assert [c.video.name for c in rep.picked] == ["01_c.mp4", "01_a.mp4"]
    assert [c.video.name for c in rep.duplicates] == ["01_e.mp4"]
    assert [c.video.name for c in rep.check_by_eye] == ["02_d.mp4"]
    assert len(rep.missing) == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_import_creates_jobs_once(conn, settings, clip_channel, folder):
    s = clip_channel.streamers[0]
    rep = importer.import_folder(conn, settings, clip_channel, s, folder, min_score=90, approve=True,
                                 hashtags=["gta6"])
    assert len(rep.imported) == 2
    job_id, cand = rep.imported[0]
    job = db.get_job(conn, job_id)
    assert job["state"] == "final_approved"
    d = settings.jobs_dir / str(job_id)
    assert (d / "final.mp4").read_bytes() == b"mp4 01_c.mp4"
    meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
    assert meta["title"] == "Title 01_c.mp4 | TestStreamer"
    assert "Caption 01_c.mp4 😱" in meta["description"]
    assert "https://youtu.be/abc123?t=3375" in meta["description"]
    assert "Used with permission" in meta["description"] and "#gta6" in meta["description"]
    assert db.get_artifact(conn, job_id, "metadata") and db.get_artifact(conn, job_id, "video")

    again = importer.import_folder(conn, settings, clip_channel, s, folder, min_score=90, approve=True)
    assert again.imported == [] and len(again.already) == 2
    # the overlapping-hour duplicate of an imported moment is still skipped
    assert [c.video.name for c in again.duplicates] == ["01_e.mp4"]


def test_import_without_approve_waits_at_gate(conn, settings, clip_channel, folder):
    s = clip_channel.streamers[0]
    rep = importer.import_folder(conn, settings, clip_channel, s, folder, min_score=90, limit=1)
    assert len(rep.imported) == 1
    assert db.get_job(conn, rep.imported[0][0])["state"] == "packaged"


def test_shuffle_keeps_the_same_clips(conn, settings, clip_channel, folder):
    s = clip_channel.streamers[0]
    rep = importer.import_folder(conn, settings, clip_channel, s, folder, min_score=0, preview=True,
                                 include_check_by_eye=True, shuffle=True)
    assert sorted(c.video.name for c in rep.picked) == ["01_a.mp4", "01_c.mp4", "02_b.mp4", "02_d.mp4"]


def _cand(title, hour, t):
    return importer.Candidate(key=title, video=None, hour=hour, score=50, unique_chatters=1,
                              reacted_at=t, clip_start=t, title=title, caption="", moment={})


def test_varied_order_separates_similar_clips():
    import random
    crashes = [_cand(f"Speed crashes car number {i}", "hour_01", 100 + i * 60) for i in range(3)]
    others = [_cand("Speed robs a bank vault", "hour_05", 20000),
              _cand("Speed vibes in the nightclub", "hour_09", 40000)]
    order = importer.varied_order(crashes + others, rng=random.Random(1))
    names = [c.title for c in order]
    # no two crash clips back to back while other clips are available
    assert not any("crashes" in a and "crashes" in b for a, b in zip(names, names[1:4]))
    assert sorted(names) == sorted(c.title for c in crashes + others)


def test_themes():
    assert importer.themes(_cand("Speed crashes fleeing cops on a bike", "h", 0)) >= {"crash", "cops", "vehicle"}

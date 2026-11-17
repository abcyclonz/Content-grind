from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from network import db, media, render, trends
from network.config import ConfigError, Video, load_channel


class FakeYT:
    def __init__(self, fail=False):
        self.fail, self.search_args = fail, None
        outer = self

        class Req:
            def __init__(self, result):
                self.result = result

            def execute(self):
                return self.result

        class Search:
            def list(self, **kw):
                if outer.fail:
                    raise RuntimeError("quota")
                outer.search_args = kw
                return Req({"items": [{"id": {"videoId": "a"}}, {"id": {"videoId": "b"}}]})

        class Videos:
            def list(self, **kw):
                return Req({"items": [
                    {"id": "a", "snippet": {"title": "Low", "channelTitle": "X", "tags": ["gta 6", "rp"]},
                     "statistics": {"viewCount": "10"}},
                    {"id": "b", "snippet": {"title": "High", "channelTitle": "Y", "tags": ["GTA 6"]},
                     "statistics": {"viewCount": "999"}}]})

        self.yt = type("YT", (), {"search": lambda s: Search(), "videos": lambda s: Videos()})()


NOW = datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc)


def test_youtube_top_shorts_request_and_sorting():
    yt = FakeYT()
    vids = trends.youtube_top_shorts(yt, "GTA 6", "en", NOW)
    assert [v["title"] for v in vids] == ["High", "Low"]
    a = yt.search_args
    assert (a["type"], a["videoDuration"], a["order"], a["q"]) == ("video", "short", "viewCount", "GTA 6")
    assert a["publishedAfter"] == "2026-11-15T06:00:00Z"
    assert trends.common_tags(vids) == ["gta 6"]


def _signals(conn):
    pub = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
    with conn:
        for i, t in enumerate(["GTA 6 pre-orders hit record", "Sensor Tower: GTA 6 pre-orders",
                               "GTA 6 trailer views", "Pre-orders explode for GTA VI"]):
            conn.execute("INSERT INTO signals (channel_id, source, url, title, published_at, fetched_at, hash) "
                         "VALUES ('gta6','s',?,?,?,?,?)", (f"u{i}", t, pub, pub, f"h{i}"))


def test_news_terms_skip_channel_keywords(conn, channel):
    _signals(conn)
    terms = dict(map(tuple, trends.news_terms(conn, channel, datetime.now(timezone.utc))))
    assert terms["pre orders"] >= 2 or terms.get("orders", 0) >= 3
    assert "gta" not in terms


def test_get_trends_caches_success_only(conn, channel, settings):
    live = replace(settings, dry_run=False)
    bad = trends.get_trends(conn, channel, live, NOW, client_factory=lambda: FakeYT(fail=True))
    assert "quota" in bad["youtube_error"]
    good = trends.get_trends(conn, channel, live, NOW, client_factory=FakeYT)
    assert good["youtube"][0]["title"] == "High"
    again = trends.get_trends(conn, channel, live, NOW, client_factory=lambda: FakeYT(fail=True))
    assert again["youtube"][0]["title"] == "High"                       # served from today's cache
    assert conn.execute("SELECT COUNT(*) FROM costs WHERE operation='search.list'").fetchone()[0] == 2
    dry = trends.get_trends(conn, channel, settings, NOW + timedelta(days=1))
    assert dry["youtube_error"] == "skipped in DRY_RUN"


# ----------------------------------------------------------------------------
# Outro
# ----------------------------------------------------------------------------

def test_outro_in_config(channel):
    assert channel.outro.text == "GOOSE GETAWAY" and channel.outro.sound.endswith("goose_honk.wav")


def test_outro_missing_sound_rejected(tmp_path):
    import yaml
    from network.config import CHANNELS_DIR
    data = yaml.safe_load((CHANNELS_DIR / "gta6.yaml").read_text(encoding="utf-8"))
    data["outro"]["sound"] = "assets/gta6/brand/nope.wav"
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="outro sound not found"):
        load_channel(p)


def test_outro_event_in_ass():
    caps = {"duration": 2.0, "words": [{"text": "Hi", "start": 0.1, "end": 0.5, "line": 0}]}
    v = Video(width=1080, height=1920, fps=30, max_seconds=55, caption_style="word_highlight")
    ass = render.build_ass(caps, v, "", "", 3.6, outro_text="Goose Getaway", outro_start=2.4)
    outro = [l for l in ass.splitlines() if ",Outro," in l]
    assert len(outro) == 1 and "0:00:02.40,0:00:03.60" in outro[0] and "GOOSE GETAWAY" in outro[0]


try:
    media.ffmpeg()
    HAVE_FFMPEG = True
except media.MediaError:
    HAVE_FFMPEG = False


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")
def test_sting_is_mixed_at_the_end(tmp_path):
    from network.brand import make_honk
    honk = make_honk(tmp_path / "honk.wav")
    voice = tmp_path / "v.mp3"
    media.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
               "anullsrc=r=48000:cl=mono:d=2", "-c:a", "libmp3lame", str(voice)])
    v = Video(width=360, height=640, fps=30, max_seconds=55, caption_style="word_highlight")
    caps = {"duration": 2.0, "words": [{"text": "Hi", "start": 0.1, "end": 0.5, "line": 0}]}
    out = tmp_path / "o.mp4"
    render.render_short([render.Scene(0, 3.4, "procedural")], voice,
                        render.build_ass(caps, v, "", "", 3.4, "Goose", 2.2), out, v, tmp_path / "w",
                        sting=honk, sting_at=2.25)

    def peak(start, dur):
        err = media.run([media.ffmpeg(), "-hide_banner", "-nostats", "-ss", str(start), "-t", str(dur),
                         "-i", str(out), "-af", "volumedetect", "-f", "null", "-"]).stderr
        return float(err.split("max_volume:")[1].split("dB")[0])
    assert peak(0, 2.0) < -60          # silent voice part
    assert peak(2.2, 1.0) > -20        # the honk is there

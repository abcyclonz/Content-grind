from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from conftest import FIXTURES, with_changes
from network import http
from network.stages import signals

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
FEED_A = "https://site-a.example/feed"
FEED_B = "https://site-b.example/feed"
FEED_DOWN = "https://down.example/feed"


def _feed(name: str) -> bytes:
    text = (FIXTURES / "feeds" / name).read_text(encoding="utf-8")
    text = text.replace("{RECENT}", format_datetime(NOW - timedelta(hours=3)))
    text = text.replace("{OLD}", format_datetime(NOW - timedelta(days=10)))
    return text.encode("utf-8")


class FakeWeb:
    def __init__(self):
        self.pages = {
            FEED_A: _feed("site_a.xml"),
            FEED_B: _feed("site_b.xml"),
            "https://site-a.example/news/gta-6-preload?utm_source=rss":
                (FIXTURES / "articles" / "preload.html").read_bytes(),
        }
        self.requests: list[str] = []

    def __call__(self, url: str):
        self.requests.append(url)
        if url in self.pages:
            return self.pages[url], url
        raise http.FetchError(f"{url}: HTTP 404")


def _channel(channel):
    return with_changes(channel, sources={"rss": [FEED_A, FEED_DOWN, FEED_B]})


def test_signals_filters_extracts_and_dedupes(conn, channel, settings):
    web = FakeWeb()
    report = signals.run(conn, _channel(channel), settings, fetch=web, now=NOW)

    assert report.feeds_ok == 2
    assert len(report.feed_errors) == 1 and "down.example" in report.feed_errors[0]
    # preload (A), vice city (A), preload again via B. Old, unrelated and undated are skipped.
    assert report.matched == 3
    assert report.new == 2
    assert report.already_stored == 1   # B's copy has the same normalised URL
    assert report.summary_only == 1     # vice-city page 404s, RSS summary kept

    rows = {r["title"]: r for r in conn.execute("SELECT * FROM signals")}
    pre = rows["GTA 6 pre-load date confirmed by Rockstar"]
    assert pre["url"] == "https://site-a.example/news/gta-6-preload"
    assert pre["source"] == "Site A Games"
    assert "17 November 2026" in pre["text"]
    assert "Cookie settings" not in pre["text"]
    assert rows["Vice City map size compared"]["text"] == "A look at the map."


def test_signals_rerun_fetches_no_articles(conn, channel, settings):
    ch = _channel(channel)
    signals.run(conn, ch, settings, fetch=FakeWeb(), now=NOW)
    web = FakeWeb()
    report = signals.run(conn, ch, settings, fetch=web, now=NOW)
    assert report.new == 0 and report.already_stored == 3
    assert set(web.requests) == {FEED_A, FEED_DOWN, FEED_B}  # feeds only
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2


def test_all_feeds_down_is_not_ok(conn, channel, settings):
    ch = with_changes(channel, sources={"rss": [FEED_DOWN]})
    report = signals.run(conn, ch, settings, fetch=FakeWeb(), now=NOW)
    assert not report.ok

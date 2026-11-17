"""Stage 7.1: fetch RSS feeds, keep recent keyword matches, store article text.

Free (no paid calls), so it runs live even in DRY_RUN. Idempotent: an article
already stored (same channel + normalised URL) is skipped before its page is
fetched, so re-running costs only the feed requests.
"""
from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlsplit

import feedparser
import trafilatura

from network import db, http
from network.config import ChannelConfig, Settings
from network.text import keyword_pattern, normalize_url, sha256, strip_html

MAX_TEXT_CHARS = 20_000
MIN_ARTICLE_CHARS = 300  # shorter extractions are usually cookie walls or teasers

Fetcher = Callable[[str], tuple[bytes, str]]


@dataclass
class SignalsReport:
    channel_id: str
    feeds_ok: int = 0
    feed_errors: list[str] = field(default_factory=list)
    matched: int = 0
    new: int = 0
    already_stored: int = 0
    summary_only: int = 0  # article page could not be read; RSS summary stored instead

    @property
    def ok(self) -> bool:
        return self.feeds_ok > 0 or not self.feed_errors

    def summary(self) -> str:
        lines = [
            f"[signals] {self.channel_id}: {self.feeds_ok} feeds read, {self.matched} recent matches, "
            f"{self.new} new, {self.already_stored} already stored, "
            f"{self.summary_only} stored from RSS summary only"
        ]
        lines += [f"  feed error: {e}" for e in self.feed_errors]
        return "\n".join(lines)


def _entry_time(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime.fromtimestamp(calendar.timegm(t), timezone.utc) if t else None


def extract_article(fetch: Fetcher, url: str) -> str | None:
    try:
        body, final_url = fetch(url)
    except http.FetchError:
        return None
    # Precision mode drops boilerplate better, but on some layouts it keeps only
    # a paragraph or two; then fall back to the default mode.
    for precise in (True, False):
        text = trafilatura.extract(
            body, url=final_url, include_comments=False, include_tables=False,
            favor_precision=precise,
        )
        if text and len(text) >= MIN_ARTICLE_CHARS:
            return text[:MAX_TEXT_CHARS]
    return None


def run(
    conn: sqlite3.Connection,
    channel: ChannelConfig,
    settings: Settings,
    fetch: Fetcher = http.fetch,
    now: datetime | None = None,
) -> SignalsReport:
    report = SignalsReport(channel.id)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=channel.sources.max_age_hours)
    pattern = keyword_pattern(channel.sources.keywords)

    for feed_url in channel.sources.rss:
        try:
            body, _ = fetch(feed_url)
        except http.FetchError as e:
            report.feed_errors.append(str(e))
            continue
        parsed = feedparser.parse(body)
        if not parsed.entries:
            reason = getattr(parsed, "bozo_exception", None) or "no entries"
            report.feed_errors.append(f"{feed_url}: {reason}")
            continue
        report.feeds_ok += 1
        source = strip_html(parsed.feed.get("title", "")) or urlsplit(feed_url).netloc

        for entry in parsed.entries:
            link = (entry.get("link") or "").strip()
            title = strip_html(entry.get("title", ""))
            published = _entry_time(entry)
            if not link or not title or published is None:
                continue
            if not (cutoff <= published <= now + timedelta(hours=1)):
                continue
            summary = strip_html(entry.get("summary", ""))
            if not pattern.search(f"{title} {summary}"):
                continue
            report.matched += 1

            url = normalize_url(link)
            h = sha256(channel.id, url)
            if conn.execute("SELECT 1 FROM signals WHERE hash = ?", (h,)).fetchone():
                report.already_stored += 1
                continue

            text = extract_article(fetch, link)
            if text is None:
                text = summary
                report.summary_only += 1
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO signals "
                    "(channel_id, source, url, title, text, published_at, fetched_at, hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        channel.id, source, url, title, text,
                        published.isoformat(timespec="seconds"), db.now_utc(), h,
                    ),
                )
            if cur.rowcount:
                report.new += 1
            else:
                report.already_stored += 1
    return report

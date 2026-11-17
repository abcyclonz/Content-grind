"""What is working right now: top recent Shorts on YouTube + what the news is about.

Cached per channel per local day (data/trends/<channel>-<date>.json), so one
search serves every Short that day. search.list has its own bucket of 100
calls/day (verified 2026-11-17); each call is logged in the costs table.
"""
from __future__ import annotations

import collections
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

from network import db
from network.config import ChannelConfig, Settings
from network.files import write_json_atomic
from network.text import STOPWORDS, ignore_tokens

LOOKBACK_HOURS = 48
TOP_N = 15


def youtube_top_shorts(client, query: str, language: str, now: datetime) -> list[dict]:
    since = (now - timedelta(hours=LOOKBACK_HOURS)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    found = client.yt.search().list(
        part="snippet", q=query, type="video", videoDuration="short", order="viewCount",
        publishedAfter=since, relevanceLanguage=language, maxResults=TOP_N,
    ).execute().get("items", [])
    ids = [i["id"]["videoId"] for i in found if i.get("id", {}).get("videoId")]
    if not ids:
        return []
    videos = client.yt.videos().list(part="snippet,statistics", id=",".join(ids)).execute().get("items", [])
    out = [{
        "title": v["snippet"]["title"],
        "channel": v["snippet"].get("channelTitle", ""),
        "views": int(v.get("statistics", {}).get("viewCount", 0) or 0),
        "published_at": v["snippet"].get("publishedAt", ""),
        "tags": v["snippet"].get("tags", [])[:15],
    } for v in videos]
    return sorted(out, key=lambda x: x["views"], reverse=True)


def news_terms(conn: sqlite3.Connection, channel: ChannelConfig, now: datetime, top: int = 12) -> list[list]:
    """Most frequent topic words/phrases in recent headlines (channel keywords excluded)."""
    since = (now - timedelta(hours=LOOKBACK_HOURS)).astimezone(timezone.utc).isoformat(timespec="seconds")
    ignore = ignore_tokens(channel.sources.keywords) | {"game", "games", "new", "says", "just"}
    counts: collections.Counter = collections.Counter()
    for (title,) in conn.execute("SELECT title FROM signals WHERE channel_id = ? AND published_at >= ?",
                                 (channel.id, since)):
        words = [w for w in "".join(c.lower() if c.isalnum() else " " for c in title).split()
                 if len(w) > 2 and w not in STOPWORDS and w not in ignore]
        counts.update(set(words))
        counts.update({f"{a} {b}" for a, b in zip(words, words[1:])})
    return [[t, n] for t, n in counts.most_common(top * 2) if n > 1][:top]


def common_tags(videos: list[dict], top: int = 10) -> list[str]:
    c: collections.Counter = collections.Counter()
    for v in videos:
        c.update({t.lower() for t in v.get("tags", [])})
    return [t for t, n in c.most_common(top) if n > 1]


def get_trends(conn: sqlite3.Connection, channel: ChannelConfig, settings: Settings,
               now: datetime | None = None, client_factory: Callable | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(channel.tz).date().isoformat()
    path = settings.data_dir / "trends" / f"{channel.id}-{day}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    data: dict = {"date": day, "fetched_at": db.now_utc(), "query": channel.sources.keywords[0],
                  "news_terms": news_terms(conn, channel, now), "youtube": [], "youtube_error": None}
    if settings.dry_run:
        data["youtube_error"] = "skipped in DRY_RUN"
    else:
        try:
            if client_factory is None:
                from network.providers.youtube import YouTubeClient, load_credentials
                client = YouTubeClient(load_credentials(channel.id))
            else:
                client = client_factory()
            db.log_cost(conn, channel.id, "youtube", "search.list", 0.0, units=1)
            data["youtube"] = youtube_top_shorts(client, data["query"], channel.language, now)
        except Exception as e:  # noqa: BLE001 - trends are a bonus; never block packaging
            data["youtube_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    data["common_tags"] = common_tags(data["youtube"])
    # Cache only a successful live fetch: failures are retried, dry runs never stick.
    if not settings.dry_run and not data["youtube_error"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, data)
    return data

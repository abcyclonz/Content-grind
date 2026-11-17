"""Text helpers: keyword matching, URL normalisation, hashing, title similarity."""
from __future__ import annotations

import hashlib
import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|cmpid$|icid$)", re.I)
_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[a-z0-9]+")

STOPWORDS = frozenset(
    "a an and are as at be but by for from has have how in is it its new of on or "
    "the this that to was what when where who why will with you your".split()
)


def keyword_pattern(keywords: list[str]) -> re.Pattern:
    """Whole-word, case-insensitive match; any run of spaces in a keyword is flexible."""
    parts = [r"\s*".join(re.escape(w) for w in k.split()) for k in keywords]
    return re.compile(r"(?<![a-z0-9])(" + "|".join(parts) + r")(?![a-z0-9])", re.I)


def strip_html(text: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", text or "")).split())


def normalize_url(url: str) -> str:
    """Lower-case host, drop fragment and tracking params, so one article = one URL."""
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k)])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def sha256(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def title_tokens(title: str, ignore: frozenset[str] = frozenset()) -> frozenset[str]:
    tokens = set()
    # Join hyphenated words first, so "pre-load" and "preload" are one token.
    for w in _WORD.findall(re.sub(r"(?<=\w)-(?=\w)", "", title.lower())):
        if w in STOPWORDS or w in ignore:
            continue
        if len(w) > 3 and w.endswith("s"):
            w = w[:-1]  # crude plural folding: "trailers" ~ "trailer"
        tokens.add(w)
    return frozenset(tokens)


def ignore_tokens(keywords: list[str]) -> frozenset[str]:
    """Words every title on this channel shares (from its keywords); they say nothing about topic."""
    return frozenset(w for k in keywords for w in _WORD.findall(k.lower()))


def topic_hash(title: str, ignore: frozenset[str] = frozenset()) -> str:
    return sha256(" ".join(sorted(title_tokens(title, ignore))))


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

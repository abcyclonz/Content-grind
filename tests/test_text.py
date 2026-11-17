import pytest

from network.text import (
    ignore_tokens,
    keyword_pattern,
    normalize_url,
    similarity,
    strip_html,
    title_tokens,
    topic_hash,
)

KEYWORDS = ["GTA 6", "GTA VI", "Grand Theft Auto VI", "Vice City"]


@pytest.mark.parametrize(
    "text, hit",
    [
        ("New GTA 6 trailer", True),
        ("gta6 leak", True),             # flexible spacing
        ("GTA  VI   news", True),
        ("Grand Theft Auto VI review", True),
        ("vice city map", True),
        ("GTA 60 fps mode", False),      # whole word only
        ("GTA V is ten years old", False),
        ("pgta 6", False),
    ],
)
def test_keyword_pattern(text, hit):
    assert bool(keyword_pattern(KEYWORDS).search(text)) is hit


def test_normalize_url_strips_tracking_and_case():
    a = normalize_url("https://Site.example/News/x/?utm_source=rss&id=3#top")
    b = normalize_url("https://site.example/News/x?id=3")
    assert a == b == "https://site.example/News/x?id=3"


def test_strip_html():
    assert strip_html("<p>Hello&nbsp;<b>world</b></p>") == "Hello world"


def test_similarity_ignores_channel_words():
    ignore = ignore_tokens(KEYWORDS)
    a = title_tokens("GTA 6 pre-load date confirmed", ignore)
    b = title_tokens("GTA VI preload date is confirmed", ignore)
    c = title_tokens("GTA 6 map size compared to Vice City", ignore)
    assert "gta" not in a
    assert similarity(a, b) >= 0.5
    assert similarity(a, c) < 0.5
    assert topic_hash("Trailers dated", ignore) == topic_hash("dated trailer", ignore)

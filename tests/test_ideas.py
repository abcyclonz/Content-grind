import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import with_changes
from network import costs, db
from network.providers.llm import FixtureLLMProvider, LLMResult
from network.stages import ideas


def _now():
    return datetime.now(timezone.utc)


TOPICS = ["Pre-load opens Monday", "Map size compared", "Special edition price", "Radio station list", "Police chase mechanics"]


def _add_signals(conn, n=5, hours_ago=2):
    ids = []
    pub = (_now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    with conn:
        for i in range(n):
            cur = conn.execute(
                "INSERT INTO signals (channel_id, source, url, title, text, published_at, fetched_at, hash) "
                "VALUES ('gta6', 'Site', ?, ?, 'body text', ?, ?, ?)",
                (f"https://x.example/{i}", TOPICS[i], pub, db.now_utc(), f"h{i}"),
            )
            ids.append(cur.lastrowid)
    return ids


class FakeLLM:
    name = "fake"

    def __init__(self, ideas_list, est_usd=0.01):
        self.ideas = ideas_list
        self.est_usd = est_usd
        self.calls = []

    def complete_json(self, *, model, system, prompt, schema):
        self.calls.append({"model": model, "prompt": prompt, "schema": schema})
        return LLMResult(data={"ideas": self.ideas}, est_usd=self.est_usd, model=model)


def _idea(title, ids, score):
    return {"title": title, "angle": f"About {title}.", "signal_ids": ids, "score": score}


def test_creates_top_n_jobs_at_idea_pending(conn, channel, settings):
    s = _add_signals(conn)
    llm = FakeLLM([
        _idea("Pre-load opens Monday", [s[0]], 70),
        _idea("Map size compared", [s[1], s[2]], 90),
        _idea("Price of the special edition", [s[3]], 80),
        _idea("Soundtrack radio stations", [s[4]], 60),
    ])
    report = ideas.run(conn, channel, settings, llm=llm)

    assert len(report.jobs_created) == channel.shorts_per_day == 3
    jobs = db.list_jobs(conn)
    assert [j["state"] for j in jobs] == ["idea_pending"] * 3
    assert [j["idea_title"] for j in jobs] == [
        "Map size compared", "Price of the special edition", "Pre-load opens Monday"
    ]
    statuses = dict(conn.execute("SELECT title, status FROM ideas").fetchall())
    assert statuses["Soundtrack radio stations"] == "not_selected"
    assert json.loads(conn.execute(
        "SELECT signal_ids FROM ideas WHERE title = 'Map size compared'").fetchone()[0]) == [s[1], s[2]]
    # Model name comes from config, and the cost is logged.
    assert llm.calls[0]["model"] == channel.models.scoring
    assert tuple(conn.execute("SELECT operation, est_usd FROM costs").fetchone()) == ("ideas.score", 0.01)


def test_rerun_same_day_does_not_call_model(conn, channel, settings):
    s = _add_signals(conn)
    ideas.run(conn, channel, settings, llm=FakeLLM([_idea(f"Idea {i}", [s[i]], 50) for i in range(4)]))
    llm = FakeLLM([])
    report = ideas.run(conn, channel, settings, llm=llm)
    assert llm.calls == [] and "nothing to do" in report.note
    assert len(db.list_jobs(conn)) == 3


def test_rejected_job_frees_a_slot(conn, channel, settings):
    s = _add_signals(conn)
    ideas.run(conn, channel, settings, llm=FakeLLM([_idea(f"Idea {i}", [s[i]], 50) for i in range(3)]))
    db.transition(conn, db.list_jobs(conn)[0]["id"], "idea_pending", "rejected")
    report = ideas.run(conn, channel, settings, llm=FakeLLM([_idea("Different topic entirely", [s[4]], 50)]))
    assert report.needed == 1 and len(report.jobs_created) == 1


def test_invalid_and_duplicate_ideas_are_dropped(conn, channel, settings):
    s = _add_signals(conn)
    with conn:
        conn.execute(
            "INSERT INTO published_topics VALUES ('gta6', 'x', 'GTA 6 pre-load date confirmed', ?)",
            (db.now_utc(),),
        )
    llm = FakeLLM([
        _idea("GTA VI preload date is confirmed", [s[0]], 99),   # same topic as published
        _idea("Made-up sources", [9999], 95),                     # ids not supplied
        {"title": "", "angle": "x", "signal_ids": [s[1]], "score": 90},
        _idea("Map size compared", [s[1]], 80),
        _idea("Map size compared again", [s[2]], 79),             # near-duplicate in batch
    ])
    report = ideas.run(conn, channel, settings, llm=llm)
    assert report.invalid == 2
    assert report.duplicates == 2
    assert [j["idea_title"] for j in db.list_jobs(conn)] == ["Map size compared"]
    assert "GTA 6 pre-load date confirmed" in llm.calls[0]["prompt"]  # told to avoid it


def test_auto_approve_advances_jobs(conn, channel, settings):
    s = _add_signals(conn)
    ch = with_changes(channel, gates={"ideas_auto_approve": True})
    report = ideas.run(conn, ch, settings, llm=FakeLLM([_idea(f"Idea {i}", [s[i]], 50) for i in range(3)]))
    assert report.auto_approved
    assert {j["state"] for j in db.list_jobs(conn)} == {"idea_approved"}


def test_no_signals_means_no_model_call(conn, channel, settings):
    _add_signals(conn, hours_ago=100)  # outside the 48 h window
    llm = FakeLLM([])
    report = ideas.run(conn, channel, settings, llm=llm)
    assert llm.calls == [] and "no signals" in report.note


def test_budget_cap_stops_before_the_call(conn, channel, settings):
    _add_signals(conn)
    db.log_cost(conn, "gta6", "fake", "earlier", channel.daily_budget_usd)
    llm = FakeLLM([])
    with pytest.raises(costs.BudgetExceeded):
        ideas.run(conn, channel, settings, llm=llm)
    assert llm.calls == []


def test_dry_run_uses_fixture_provider(conn, channel, settings):
    _add_signals(conn)
    assert settings.dry_run
    report = ideas.run(conn, channel, settings)   # no llm passed: make_llm picks the fixture
    assert len(report.jobs_created) == 3
    assert all(j["idea_title"].startswith("[dry run]") for j in db.list_jobs(conn))
    assert tuple(conn.execute("SELECT provider, est_usd FROM costs").fetchone()) == ("fixture", 0.0)


def test_prompt_contains_brief_and_untrusted_marking(conn, channel, settings):
    _add_signals(conn)
    llm = FixtureLLMProvider(lambda: {"ideas": []})
    ideas.run(conn, channel, settings, llm=llm)
    call = llm.calls[0]
    assert "19 November 2026" in call["prompt"]       # from topic_brief in YAML
    assert "untrusted" in call["system"]
    assert '<item id="' in call["prompt"]

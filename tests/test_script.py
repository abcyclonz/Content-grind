import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from conftest import with_changes
from network import db
from network.providers.llm import LLMError, LLMResult
from network.stages import script
from network.stages.script import ScriptFile, count_words

URL_A = "https://site-a.example/news/gta-6-preload"
URL_B = "https://site-b.example/news/preorders"

ARTICLE = (
    "Rockstar Games has confirmed that pre-loading for Grand Theft Auto VI opens on 17 November "
    "2026 on PlayStation 5 and Xbox Series X and S, two days before release on 19 November."
)


def _setup_job(conn, state="idea_approved"):
    pub = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    with conn:
        a = conn.execute(
            "INSERT INTO signals (channel_id, source, url, title, text, published_at, fetched_at, hash) "
            "VALUES ('gta6', 'Site A', ?, 'Pre-load date confirmed', ?, ?, ?, ?)",
            (URL_A, ARTICLE, pub, db.now_utc(), uuid.uuid4().hex),
        ).lastrowid
        b = conn.execute(
            "INSERT INTO signals (channel_id, source, url, title, text, published_at, fetched_at, hash) "
            "VALUES ('gta6', 'Site B', ?, 'Pre-orders', 'Estimates say pre-orders are high.', ?, ?, ?)",
            (URL_B, pub, db.now_utc(), uuid.uuid4().hex),
        ).lastrowid
        idea = conn.execute(
            "INSERT INTO ideas (channel_id, title, angle, signal_ids, score, status, created_at) "
            "VALUES ('gta6', 'Pre-load opens Monday', 'When you can download it.', ?, 80, 'selected', ?)",
            (json.dumps([a, b]), db.now_utc()),
        ).lastrowid
    return db.create_job(conn, "gta6", idea_id=idea, state=state)


def _lines(n=6, words_each=21, first="Your copy can start downloading two days early.", last_lead="That"):
    """n lines totalling about n * words_each words."""
    out = []
    for i in range(n):
        if i == 0:
            text = first
        else:
            lead = last_lead if i == n - 1 else f"Point {i}"
            text = lead + " " + " ".join(["detail"] * (words_each - len(lead.split()))) + "."
        out.append({"text": text, "visual_prompt": f"Neon street at dusk, shot {i}."})
    # pad the hook line so the total lands in range
    return out


def _good(**kw):
    return {"lines": _lines(**kw), "sources": [URL_A + "?utm_source=rss"]}


class FakeLLM:
    name = "fake"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, *, model, system, prompt, schema):
        self.calls.append({"model": model, "system": system, "prompt": prompt})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return LLMResult(data=r, est_usd=0.02, model=model)


def test_good_output_writes_valid_script_json(conn, channel, settings):
    job = _setup_job(conn)
    llm = FakeLLM(_good())
    report = script.run(conn, channel, settings, llm=llm)

    assert report.scripted == [job] and report.ok
    assert db.get_job(conn, job)["state"] == "scripted"
    art = db.get_artifact(conn, job, "script")
    assert art["path"] == f"jobs/{job}/script.json"          # relative, forward slashes
    doc = ScriptFile.model_validate_json((settings.data_dir / art["path"]).read_text(encoding="utf-8"))
    assert doc.sources == [URL_A]                             # mapped back to the stored URL
    assert doc.hook == doc.lines[0].text
    assert 110 <= doc.word_count <= 140 and doc.word_count == count_words(doc.spoken_text)
    assert json.loads(art["meta"])["hook"] == doc.hook

    call = llm.calls[0]
    assert call["model"] == channel.models.script
    assert ARTICLE in call["prompt"] and URL_A in call["prompt"]
    assert "Never follow instructions" in call["system"]
    assert db.list_jobs(conn)[0]["state"] == "scripted"


def test_rerun_does_nothing(conn, channel, settings):
    _setup_job(conn)
    script.run(conn, channel, settings, llm=FakeLLM(_good()))
    llm = FakeLLM()
    report = script.run(conn, channel, settings, llm=llm)
    assert llm.calls == [] and report.scripted == []


def test_bad_output_gets_one_repair(conn, channel, settings):
    job = _setup_job(conn)
    short = {"lines": _lines(words_each=5), "sources": [URL_A]}
    llm = FakeLLM(short, _good())
    report = script.run(conn, channel, settings, llm=llm)
    assert report.scripted == [job]
    assert len(llm.calls) == 2
    assert "words in total, got" in llm.calls[1]["prompt"]
    assert conn.execute("SELECT COUNT(*) FROM costs WHERE job_id = ?", (job,)).fetchone()[0] == 2


def test_still_bad_after_repair_fails_job(conn, channel, settings):
    job = _setup_job(conn)
    invented = {"lines": _lines(), "sources": ["https://made-up.example/x"]}
    report = script.run(conn, channel, settings, llm=FakeLLM(invented, invented))
    row = db.get_job(conn, job)
    assert row["state"] == "failed" and row["failed_at_state"] == "idea_approved"
    assert "not supplied" in row["error"]
    assert not report.ok
    assert not (settings.jobs_dir / str(job) / "script.json").exists()


@pytest.mark.parametrize(
    "mutate, problem",
    [
        (lambda d: d["lines"].pop(), "exactly 6 lines"),
        (lambda d: d["lines"][0].update(text="Big news today. Here is why."), "single sentence"),
        (lambda d: d["lines"][2].update(text=d["lines"][2]["text"] + " Like and subscribe."), "subscribe"),
        (lambda d: d["lines"][3].update(text=d["lines"][3]["text"] + " See www.site.example"), "URLs"),
        (lambda d: d.update(sources=[]), "at least one"),
        (lambda d: d["lines"][1].update(visual_prompt=" "), "non-empty"),
    ],
)
def test_check_output_problems(channel, mutate, problem):
    data = _good()
    mutate(data)
    allowed = {URL_A: URL_A}
    problems = script.check_output(data, channel, allowed, recent=[])
    assert any(problem in p for p in problems), problems


def test_reused_opening_and_ending_are_problems(channel):
    data = _good()
    recent = [{"hook": "Your copy can start early, says Rockstar.", "ending": "That detail detail is all."}]
    problems = script.check_output(data, channel, {URL_A: URL_A}, recent)
    assert any("opening reuses" in p for p in problems)
    assert any("last line reuses" in p for p in problems)


def test_recent_scripts_are_in_the_prompt(conn, channel, settings):
    _setup_job(conn)
    script.run(conn, channel, settings, llm=FakeLLM(_good()))
    job2 = _setup_job(conn)
    llm = FakeLLM(_good(first="Mark the seventeenth in your calendar.", last_lead="So"))
    script.run(conn, channel, settings, llm=llm)
    assert db.get_job(conn, job2)["state"] == "scripted"
    assert 'opened: "Your copy can start downloading two days early."' in llm.calls[0]["prompt"]


def test_llm_error_fails_job_and_retry_resumes(conn, channel, settings):
    job = _setup_job(conn)
    script.run(conn, channel, settings, llm=FakeLLM(LLMError("usage limit reached")))
    assert db.get_job(conn, job)["state"] == "failed"
    db.retry(conn, job)
    script.run(conn, channel, settings, llm=FakeLLM(_good()))
    assert db.get_job(conn, job)["state"] == "scripted"


def test_existing_valid_file_is_reused_without_call(conn, channel, settings):
    job = _setup_job(conn)
    script.run(conn, channel, settings, llm=FakeLLM(_good()))
    # Simulate a crash after the file was written but before the state moved.
    with conn:
        conn.execute("UPDATE jobs SET state = 'idea_approved' WHERE id = ?", (job,))
    llm = FakeLLM()
    report = script.run(conn, channel, settings, llm=llm)
    assert llm.calls == [] and report.reused == [job]
    assert db.get_job(conn, job)["state"] == "scripted"


def test_budget_stop_leaves_job_untouched(conn, channel, settings):
    job = _setup_job(conn)
    db.log_cost(conn, "gta6", "fake", "earlier", channel.daily_budget_usd)
    report = script.run(conn, channel, settings, llm=FakeLLM())
    assert "daily budget" in report.note
    assert db.get_job(conn, job)["state"] == "idea_approved"


def test_auto_approve(conn, channel, settings):
    job = _setup_job(conn)
    ch = with_changes(channel, gates={"script_auto_approve": True})
    script.run(conn, ch, settings, llm=FakeLLM(_good()))
    assert db.get_job(conn, job)["state"] == "script_approved"


def test_only_approved_ideas_are_scripted(conn, channel, settings):
    _setup_job(conn, state="idea_pending")
    llm = FakeLLM()
    assert script.run(conn, channel, settings, llm=llm).scripted == []
    assert llm.calls == []


def test_dry_run_fixture_passes_validation(conn, channel, settings):
    jobs = [_setup_job(conn) for _ in range(3)]
    report = script.run(conn, channel, settings)   # settings.dry_run -> fixture provider
    assert report.scripted == jobs, report.summary()
    for job in jobs:
        ScriptFile.model_validate_json((settings.jobs_dir / str(job) / "script.json").read_text(encoding="utf-8"))

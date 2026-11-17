import sqlite3

import pytest

from network import db


def test_fresh_db_has_all_tables_and_no_jobs(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"signals", "ideas", "jobs", "artifacts", "costs", "published_topics"} <= tables
    assert db.schema_version(conn) == db.SCHEMA_VERSION
    assert db.list_jobs(conn) == []


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "x.db"
    db.connect(path).close()
    c = db.connect(path)  # second open must not re-run migration 1
    assert db.migrate(c) == db.SCHEMA_VERSION
    c.close()


def test_db_newer_than_code_is_refused(conn):
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="newer"):
        db.migrate(conn)


def test_state_check_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO jobs (channel_id, state, created_at, updated_at) "
                "VALUES ('gta6', 'bogus', 'x', 'x')"
            )


def test_signal_hash_is_unique(conn):
    row = ("gta6", "rss", "https://a", "t", "now", "h1")
    sql = "INSERT INTO signals (channel_id, source, url, title, fetched_at, hash) VALUES (?,?,?,?,?,?)"
    with conn:
        conn.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(sql, row)


def test_transition_is_compare_and_set(conn):
    job = db.create_job(conn, "gta6")
    assert db.transition(conn, job, "idea_pending", "idea_approved") is True
    # Re-running the same step on a job that already moved on does nothing.
    assert db.transition(conn, job, "idea_pending", "idea_approved") is False
    assert db.get_job(conn, job)["state"] == "idea_approved"


def test_illegal_transitions_raise(conn):
    job = db.create_job(conn, "gta6")
    with pytest.raises(db.InvalidTransition):
        db.transition(conn, job, "idea_pending", "published")   # skipping stages
    with pytest.raises(db.InvalidTransition):
        db.transition(conn, job, "voiced", "rejected")           # not a gate
    with pytest.raises(db.InvalidTransition):
        db.transition(conn, job, "idea_pending", "failed")       # must use mark_failed


def test_full_pipeline_walk(conn):
    job = db.create_job(conn, "gta6")
    for a, b in zip(db.PIPELINE_STATES, db.PIPELINE_STATES[1:]):
        assert db.transition(conn, job, a, b)
    assert db.get_job(conn, job)["state"] == "published"
    assert db.mark_failed(conn, job, "late error") is False   # published is terminal


def test_fail_then_retry_resumes_at_failed_stage(conn):
    job = db.create_job(conn, "gta6", state="script_approved")
    assert db.mark_failed(conn, job, "TTS timeout")
    row = db.get_job(conn, job)
    assert (row["state"], row["failed_at_state"], row["attempts"]) == ("failed", "script_approved", 1)
    assert db.mark_failed(conn, job, "again") is False  # already failed

    assert db.retry(conn, job) is True
    row = db.get_job(conn, job)
    assert (row["state"], row["failed_at_state"]) == ("script_approved", None)
    assert db.retry(conn, job) is False  # nothing to retry now

    db.mark_failed(conn, job, "second failure")
    assert db.get_job(conn, job)["attempts"] == 2


def test_reject_at_gate(conn):
    job = db.create_job(conn, "gta6")
    assert db.transition(conn, job, "idea_pending", "rejected")
    assert db.mark_failed(conn, job, "x") is False
    assert [r["id"] for r in db.jobs_in_state(conn, "rejected", "gta6")] == [job]


def test_artifact_kind_and_cascade(conn):
    job = db.create_job(conn, "gta6")
    with conn:
        conn.execute(
            "INSERT INTO artifacts (job_id, kind, path, created_at) VALUES (?, 'script', ?, 'now')",
            (job, f"jobs/{job}/script.json"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute(
                "INSERT INTO artifacts (job_id, kind, path, created_at) VALUES (?, 'poster', 'p', 'now')",
                (job,),
            )
    with conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job,))
    assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0

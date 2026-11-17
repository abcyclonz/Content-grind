"""SQLite schema, migrations and job-state helpers.

All timestamps are UTC ISO-8601 strings. Artifact paths are stored relative
to the data directory so the database survives a move to another machine.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Job state machine
# --------------------------------------------------------------------------

PIPELINE_STATES = (
    "idea_pending",
    "idea_approved",
    "scripted",
    "script_approved",
    "voiced",
    "captioned",
    "visuals_ready",
    "assembled",
    "packaged",
    "final_approved",
    "scheduled",
    "published",
)
FAILED = "failed"
REJECTED = "rejected"
JOB_STATES = PIPELINE_STATES + (FAILED, REJECTED)
TERMINAL_STATES = frozenset({"published", REJECTED})

# States that wait at an approval gate, and so can be rejected.
GATE_STATES = frozenset({"idea_pending", "scripted", "packaged"})


def _build_transitions() -> dict[str, frozenset[str]]:
    t: dict[str, set[str]] = {}
    for here, nxt in zip(PIPELINE_STATES, PIPELINE_STATES[1:]):
        t[here] = {nxt, FAILED}
    for gate in GATE_STATES:
        t[gate].add(REJECTED)
    # Leaving "failed" goes through retry(), which restores failed_at_state.
    return {k: frozenset(v) for k, v in t.items()}


TRANSITIONS = _build_transitions()

ARTIFACT_KINDS = ("script", "voice", "captions", "image", "video", "thumbnail", "metadata")


class InvalidTransition(ValueError):
    pass


def _sql_list(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


# --------------------------------------------------------------------------
# Migrations: append only. Never edit a migration that has shipped.
# Each entry moves the database from user_version i to i + 1.
# --------------------------------------------------------------------------

MIGRATIONS: list[str] = [
    # 1: initial schema (SPEC.md section 6, plus jobs.failed_at_state and costs.channel_id)
    f"""
    CREATE TABLE signals (
        id            INTEGER PRIMARY KEY,
        channel_id    TEXT NOT NULL,
        source        TEXT NOT NULL,
        url           TEXT NOT NULL,
        title         TEXT NOT NULL,
        text          TEXT,
        published_at  TEXT,
        fetched_at    TEXT NOT NULL,
        hash          TEXT NOT NULL UNIQUE
    );
    CREATE INDEX idx_signals_channel_published ON signals(channel_id, published_at);

    CREATE TABLE ideas (
        id          INTEGER PRIMARY KEY,
        channel_id  TEXT NOT NULL,
        title       TEXT NOT NULL,
        angle       TEXT NOT NULL,
        signal_ids  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(signal_ids)),
        score       REAL CHECK (score IS NULL OR score BETWEEN 0 AND 100),
        status      TEXT NOT NULL DEFAULT 'new',
        created_at  TEXT NOT NULL
    );
    CREATE INDEX idx_ideas_channel_created ON ideas(channel_id, created_at);

    CREATE TABLE jobs (
        id                    INTEGER PRIMARY KEY,
        channel_id            TEXT NOT NULL,
        idea_id               INTEGER REFERENCES ideas(id),
        state                 TEXT NOT NULL CHECK (state IN ({_sql_list(JOB_STATES)})),
        failed_at_state       TEXT CHECK (failed_at_state IS NULL
                                          OR failed_at_state IN ({_sql_list(PIPELINE_STATES)})),
        error                 TEXT,
        attempts              INTEGER NOT NULL DEFAULT 0,
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL,
        scheduled_publish_at  TEXT,
        youtube_video_id      TEXT,
        CHECK ((state = 'failed') = (failed_at_state IS NOT NULL))
    );
    CREATE INDEX idx_jobs_channel_state ON jobs(channel_id, state);

    CREATE TABLE artifacts (
        id          INTEGER PRIMARY KEY,
        job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        kind        TEXT NOT NULL CHECK (kind IN ({_sql_list(ARTIFACT_KINDS)})),
        path        TEXT NOT NULL,
        meta        TEXT NOT NULL DEFAULT '{{}}' CHECK (json_valid(meta)),
        created_at  TEXT NOT NULL
    );
    CREATE INDEX idx_artifacts_job ON artifacts(job_id, kind);

    CREATE TABLE costs (
        id          INTEGER PRIMARY KEY,
        channel_id  TEXT NOT NULL,
        job_id      INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
        provider    TEXT NOT NULL,
        operation   TEXT NOT NULL,
        units       REAL,
        est_usd     REAL NOT NULL,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX idx_costs_channel_created ON costs(channel_id, created_at);

    CREATE TABLE published_topics (
        channel_id    TEXT NOT NULL,
        topic_hash    TEXT NOT NULL,
        title         TEXT NOT NULL,
        published_at  TEXT NOT NULL,
        PRIMARY KEY (channel_id, topic_hash)
    );
    """,
    # 2: Telegram gate messages (one per job per gate), so each item is announced once
    """
    CREATE TABLE gate_messages (
        id          INTEGER PRIMARY KEY,
        job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        state       TEXT NOT NULL,
        chat_id     TEXT NOT NULL,
        message_id  INTEGER NOT NULL,
        sent_at     TEXT NOT NULL,
        resolved    TEXT,
        UNIQUE (job_id, state)
    );
    CREATE INDEX idx_gate_messages_msg ON gate_messages(chat_id, message_id);

    CREATE TABLE kv (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    );
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the database, creating and migrating it if needed."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # scheduler and CLI can read at once
    conn.execute("PRAGMA busy_timeout = 10000")
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Safe to call any number of times."""
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{current} is newer than this code (v{SCHEMA_VERSION})"
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        script = MIGRATIONS[version - 1]
        try:
            # user_version is transactional, so schema change and version bump
            # commit together or not at all.
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    return schema_version(conn)


# --------------------------------------------------------------------------
# Job helpers
# --------------------------------------------------------------------------

def create_job(
    conn: sqlite3.Connection,
    channel_id: str,
    idea_id: int | None = None,
    state: str = "idea_pending",
) -> int:
    if state not in PIPELINE_STATES:
        raise ValueError(f"cannot create a job in state {state!r}")
    ts = now_utc()
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (channel_id, idea_id, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel_id, idea_id, state, ts, ts),
        )
    return cur.lastrowid


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def transition(conn: sqlite3.Connection, job_id: int, from_state: str, to_state: str) -> bool:
    """Move a job from from_state to to_state.

    Compare-and-set: returns False (and changes nothing) if the job is no
    longer in from_state, e.g. because another run already advanced it. This
    is what makes re-running a stage on a finished job a no-op.
    """
    if to_state == FAILED:
        raise InvalidTransition("use mark_failed() to fail a job")
    if to_state not in TRANSITIONS.get(from_state, frozenset()):
        raise InvalidTransition(f"{from_state} -> {to_state} is not allowed")
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
            (to_state, now_utc(), job_id, from_state),
        )
    return cur.rowcount == 1


def mark_failed(conn: sqlite3.Connection, job_id: int, error: str) -> bool:
    """Fail a job, remembering the state it was in so retry() can resume it.

    attempts counts failures. Returns False if the job is already failed,
    rejected or published.
    """
    with conn:
        cur = conn.execute(
            f"UPDATE jobs SET failed_at_state = state, state = 'failed', error = ?, "
            f"attempts = attempts + 1, updated_at = ? "
            f"WHERE id = ? AND state NOT IN ({_sql_list([FAILED, *TERMINAL_STATES])})",
            (error, now_utc(), job_id),
        )
    return cur.rowcount == 1


def retry(conn: sqlite3.Connection, job_id: int) -> bool:
    """Put a failed job back in the state it failed in. Keeps the last error."""
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET state = failed_at_state, failed_at_state = NULL, updated_at = ? "
            "WHERE id = ? AND state = 'failed'",
            (now_utc(), job_id),
        )
    return cur.rowcount == 1


def jobs_in_state(
    conn: sqlite3.Connection, state: str, channel_id: str | None = None
) -> list[sqlite3.Row]:
    if state not in JOB_STATES:
        raise ValueError(f"unknown state {state!r}")
    sql = "SELECT * FROM jobs WHERE state = ?"
    args: list = [state]
    if channel_id:
        sql += " AND channel_id = ?"
        args.append(channel_id)
    return conn.execute(sql + " ORDER BY id", args).fetchall()


def list_jobs(
    conn: sqlite3.Connection, channel_id: str | None = None, state: str | None = None
) -> list[sqlite3.Row]:
    sql = (
        "SELECT jobs.*, ideas.title AS idea_title FROM jobs "
        "LEFT JOIN ideas ON ideas.id = jobs.idea_id WHERE 1 = 1"
    )
    args: list = []
    if channel_id:
        sql += " AND jobs.channel_id = ?"
        args.append(channel_id)
    if state:
        sql += " AND jobs.state = ?"
        args.append(state)
    return conn.execute(sql + " ORDER BY jobs.id", args).fetchall()


class _NotInState(Exception):
    pass


def advance_with_artifact(
    conn: sqlite3.Connection,
    job_id: int,
    from_state: str,
    to_state: str,
    kind: str,
    rel_path: str,
    meta: dict | None = None,
) -> bool:
    """Record a stage's output file and advance the job, atomically.

    Replaces any earlier artifact of the same kind for this job. Returns False
    (and records nothing) if the job is no longer in from_state.
    """
    if to_state not in TRANSITIONS.get(from_state, frozenset()) or to_state in (FAILED, REJECTED):
        raise InvalidTransition(f"{from_state} -> {to_state} is not allowed")
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"unknown artifact kind {kind!r}")
    ts = now_utc()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
                (to_state, ts, job_id, from_state),
            )
            if cur.rowcount != 1:
                raise _NotInState
            conn.execute("DELETE FROM artifacts WHERE job_id = ? AND kind = ?", (job_id, kind))
            conn.execute(
                "INSERT INTO artifacts (job_id, kind, path, meta, created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, kind, rel_path, json.dumps(meta or {}), ts),
            )
    except _NotInState:
        return False
    return True


def get_artifact(conn: sqlite3.Connection, job_id: int, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM artifacts WHERE job_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (job_id, kind),
    ).fetchone()


def set_error(conn: sqlite3.Connection, job_id: int, error: str | None) -> None:
    with conn:
        conn.execute(
            "UPDATE jobs SET error = ?, updated_at = ? WHERE id = ?", (error, now_utc(), job_id)
        )


# --------------------------------------------------------------------------
# Cost helpers
# --------------------------------------------------------------------------

def log_cost(
    conn: sqlite3.Connection,
    channel_id: str,
    provider: str,
    operation: str,
    est_usd: float,
    units: float | None = None,
    job_id: int | None = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO costs (channel_id, job_id, provider, operation, units, est_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (channel_id, job_id, provider, operation, units, est_usd, now_utc()),
        )


def spent_since(conn: sqlite3.Connection, channel_id: str, since_utc: datetime) -> float:
    since = since_utc.astimezone(timezone.utc).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT COALESCE(SUM(est_usd), 0) FROM costs WHERE channel_id = ? AND created_at >= ?",
        (channel_id, since),
    ).fetchone()
    return float(row[0])

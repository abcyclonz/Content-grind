"""Approval gates, shared by the CLI now and the Telegram bot in Milestone 6."""
from __future__ import annotations

import sqlite3

from network import db

# state waiting at a gate -> state after approval
GATE_NEXT = {
    "idea_pending": "idea_approved",
    "scripted": "script_approved",
    "packaged": "final_approved",
}
GATE_NAMES = {"idea_pending": "ideas", "scripted": "script", "packaged": "final"}
assert set(GATE_NEXT) == db.GATE_STATES


class GateError(ValueError):
    pass


def _gate_state(conn: sqlite3.Connection, job_id: int) -> str:
    job = db.get_job(conn, job_id)
    if job is None:
        raise GateError(f"no job {job_id}")
    if job["state"] not in GATE_NEXT:
        raise GateError(f"job {job_id} is in {job['state']}, which is not waiting at a gate")
    return job["state"]


def approve(conn: sqlite3.Connection, job_id: int) -> tuple[str, str]:
    state = _gate_state(conn, job_id)
    nxt = GATE_NEXT[state]
    if not db.transition(conn, job_id, state, nxt):
        raise GateError(f"job {job_id} changed state while approving; check status")
    return state, nxt


def reject(conn: sqlite3.Connection, job_id: int, reason: str | None = None) -> str:
    state = _gate_state(conn, job_id)
    if not db.transition(conn, job_id, state, db.REJECTED):
        raise GateError(f"job {job_id} changed state while rejecting; check status")
    db.set_error(conn, job_id, f"rejected at {GATE_NAMES[state]} gate" + (f": {reason}" if reason else ""))
    return state

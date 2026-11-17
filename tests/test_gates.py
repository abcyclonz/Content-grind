import pytest

from network import db, gates


@pytest.mark.parametrize(
    "state, after",
    [("idea_pending", "idea_approved"), ("scripted", "script_approved"), ("packaged", "final_approved")],
)
def test_approve_each_gate(conn, state, after):
    job = db.create_job(conn, "gta6", state=state)
    assert gates.approve(conn, job) == (state, after)
    assert db.get_job(conn, job)["state"] == after


def test_reject_records_reason(conn):
    job = db.create_job(conn, "gta6", state="scripted")
    assert gates.reject(conn, job, "too much speculation") == "scripted"
    row = db.get_job(conn, job)
    assert row["state"] == "rejected"
    assert row["error"] == "rejected at script gate: too much speculation"


def test_not_at_a_gate(conn):
    job = db.create_job(conn, "gta6", state="voiced")
    with pytest.raises(gates.GateError, match="not waiting at a gate"):
        gates.approve(conn, job)
    with pytest.raises(gates.GateError, match="no job"):
        gates.reject(conn, 999)

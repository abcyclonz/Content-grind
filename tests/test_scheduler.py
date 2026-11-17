import logging

from network import db
from network.scheduler import PIPELINE, Runner

from conftest import FIXTURES
from network.config import load_channel


class Report:
    def __init__(self, text):
        self.text = text

    def summary(self):
        return self.text


def test_pipeline_runs_every_stage_in_order_for_each_channel(settings, channel, caplog):
    clip = load_channel(FIXTURES / "channels" / "clips_valid.yaml")
    calls = []

    def fake(stage):
        def run(conn, ch, s):
            calls.append((stage, ch.id))
            if stage == "voice":
                raise RuntimeError("TTS down")
            return Report(f"[{stage}] {ch.id}: did something")
        return run

    funcs = {s: fake(s) for s in ("signals", "ideas", *PIPELINE)}
    r = Runner(settings, channels={"gta6": channel, "testclips": clip}, stage_funcs=funcs)
    with caplog.at_level(logging.INFO, logger="scheduler"):
        r.pipeline()
    shorts = [s for s, c in calls if c == "gta6"]
    assert shorts == list(PIPELINE)                                   # in order, voice error did not stop it
    assert [s for s, c in calls if c == "testclips"] == ["package", "publish"]
    assert any("TTS down" in m for m in caplog.messages)


def test_signals_and_ideas_only_for_shorts(settings, channel):
    clip = load_channel(FIXTURES / "channels" / "clips_valid.yaml")
    calls = []
    funcs = {s: (lambda s: lambda conn, ch, st: calls.append((s, ch.id)) or Report(""))(s)
             for s in ("signals", "ideas", *PIPELINE)}
    r = Runner(settings, channels={"gta6": channel, "testclips": clip}, stage_funcs=funcs)
    r.signals()
    r.ideas("gta6")
    assert calls == [("signals", "gta6"), ("ideas", "gta6")]


def test_gates_auto_advance_runs_without_telegram(settings, channel):
    from conftest import with_changes
    ch = with_changes(channel, gates={"script_auto_approve": True})
    conn = db.connect(settings.db_path)
    job = db.create_job(conn, "gta6", state="scripted")
    conn.close()
    Runner(settings, channels={"gta6": ch}, stage_funcs={}).gates()
    conn = db.connect(settings.db_path)
    assert db.get_job(conn, job)["state"] == "script_approved"
    conn.close()

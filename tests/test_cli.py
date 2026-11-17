from network import cli, db

from conftest import FIXTURES


def test_status_on_empty_db_prints_headers_only(data_dir, capsys):
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "id" in out and "state" in out and "attempts" in out
    assert "(0 jobs)" in out
    assert (data_dir / "network.db").exists()


def test_status_lists_jobs_and_failures(data_dir, capsys):
    conn = db.connect(data_dir / "network.db")
    db.create_job(conn, "gta6")
    failed = db.create_job(conn, "gta6", state="voiced")
    db.mark_failed(conn, failed, "ffmpeg exited 1\nwith a long multi-line message")
    conn.close()

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "idea_pending" in out
    assert "failed (at voiced)" in out
    assert "ffmpeg exited 1 with a long" in out
    assert "(2 jobs)" in out

    assert cli.main(["status", "--state", "failed"]) == 0
    assert "(1 job)" in capsys.readouterr().out


def test_check_config_accepts_real_channel(capsys):
    assert cli.main(["check-config"]) == 0
    assert "OK" in capsys.readouterr().out


def test_check_config_rejects_broken_files(capsys):
    rc = cli.main([
        "check-config",
        str(FIXTURES / "channels" / "broken_syntax.yaml"),
        str(FIXTURES / "channels" / "bad_values.yaml"),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.count("FAIL") == 2


def test_telegram_test_needs_config(data_dir, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    assert cli.main(["telegram-test"]) == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err

import copy

import pytest
import yaml

from network.config import (
    CHANNELS_DIR,
    ConfigError,
    Settings,
    load_channel,
    load_channels,
    load_settings,
)

from conftest import FIXTURES

GTA6 = CHANNELS_DIR / "gta6.yaml"


def test_real_channel_config_loads():
    cfg = load_channel(GTA6)
    assert cfg.id == "gta6"
    assert cfg.shorts_per_day == len(cfg.publish_times_local) == 3
    assert cfg.models.script  # name comes from config, not code
    assert cfg.gates.ideas_auto_approve is False
    assert str(cfg.tz) == "Asia/Kolkata"


def test_load_channels_indexes_by_id():
    assert "gta6" in load_channels()


def test_broken_yaml_syntax_is_rejected_with_location():
    with pytest.raises(ConfigError, match=r"invalid YAML at line \d+"):
        load_channel(FIXTURES / "channels" / "broken_syntax.yaml")


def test_bad_values_fixture_reports_every_problem():
    with pytest.raises(ConfigError) as exc:
        load_channel(FIXTURES / "channels" / "bad_values.yaml")
    msg = str(exc.value)
    assert "publish_times_local" in msg
    assert "timezone" in msg
    assert "visuals.use_official_folder" in msg
    assert "gates.final_auto_aprove" in msg       # unknown key
    assert "gates.final_auto_approve" in msg      # and the real one is missing


def _base() -> dict:
    return yaml.safe_load(GTA6.read_text(encoding="utf-8"))


def _set(d: dict, dotted: str, value):
    *parents, last = dotted.split(".")
    for p in parents:
        d = d[p]
    if value is _DELETE:
        del d[last]
    else:
        d[last] = value


_DELETE = object()


@pytest.mark.parametrize(
    "key, value, expect",
    [
        ("format", "long", "format"),
        ("shorts_per_day", "3", "shorts_per_day"),               # string, not int
        ("shorts_per_day", 2, "shorts_per_day is 2"),             # count mismatch
        ("publish_times_local", ["9:00", "14:00", "19:00"], "HH:MM"),
        ("publish_times_local", ["09:00", "09:00", "19:00"], "duplicate"),
        ("timezone", "Mars/Olympus", "unknown IANA timezone"),
        ("daily_budget_usd", 0, "daily_budget_usd"),
        ("sources.keywords", [], "sources.keywords"),
        ("sources.rss", ["not a url"], "http"),
        ("models.script", "", "models.script"),
        ("video.max_seconds", 90, "video.max_seconds"),
        ("gates.script_auto_approve", "no", "gates.script_auto_approve"),
        ("publish.privacy", "secret", "publish.privacy"),
        ("publish", _DELETE, "publish"),
        ("surprise_key", 1, "surprise_key"),
    ],
)
def test_invalid_values_are_rejected(tmp_path, key, value, expect):
    data = copy.deepcopy(_base())
    _set(data, key, value)
    path = tmp_path / "ch.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match=expect):
        load_channel(path)


def test_non_mapping_and_empty_files_are_rejected(tmp_path):
    for text in ("", "- a\n- b\n"):
        p = tmp_path / "x.yaml"
        p.write_text(text, encoding="utf-8")
        with pytest.raises(ConfigError, match="top level must be a mapping"):
            load_channel(p)


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_channel(tmp_path / "nope.yaml")


def test_settings_defaults_to_dry_run(data_dir, monkeypatch):
    monkeypatch.delenv("DRY_RUN")
    s = load_settings(env_file=data_dir / "missing.env")
    assert s.dry_run is True
    assert s.db_path == data_dir / "network.db"


def test_settings_rejects_bad_dry_run(data_dir, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "maybe")
    with pytest.raises(ConfigError, match="DRY_RUN"):
        load_settings()


def test_missing_secret_raises_without_leaking(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY is not set"):
        Settings.secret("ANTHROPIC_API_KEY")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert Settings.secret("ANTHROPIC_API_KEY") == "sk-test"

from pathlib import Path

import pytest

from network import db
from network.config import CHANNELS_DIR, Settings, load_channel

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the app at an empty temporary data directory."""
    d = tmp_path / "data"
    monkeypatch.setenv("NETWORK_DATA_DIR", str(d))
    monkeypatch.setenv("DRY_RUN", "1")
    return d


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def channel():
    return load_channel(CHANNELS_DIR / "gta6.yaml")


@pytest.fixture
def settings(tmp_path):
    return Settings(dry_run=True, data_dir=tmp_path / "data", channels_dir=CHANNELS_DIR)


def with_changes(cfg, **sections):
    """Copy a frozen ChannelConfig, e.g. with_changes(cfg, gates={"ideas_auto_approve": True})."""
    update = {}
    for key, value in sections.items():
        current = getattr(cfg, key)
        update[key] = current.model_copy(update=value) if isinstance(value, dict) else value
    return cfg.model_copy(update=update)

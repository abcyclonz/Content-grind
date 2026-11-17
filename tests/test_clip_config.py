import copy
from datetime import date, timedelta

import pytest
import yaml

from conftest import FIXTURES
from network.config import ClipChannelConfig, ConfigError, load_channel

VALID = FIXTURES / "channels" / "clips_valid.yaml"


def _write(tmp_path, data):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def _base():
    return yaml.safe_load(VALID.read_text(encoding="utf-8"))


def test_valid_clip_channel():
    cfg = load_channel(VALID)
    assert isinstance(cfg, ClipChannelConfig)
    s = cfg.streamers[0]
    assert s.credit_text == "twitch.tv/test_streamer" and s.name == "TestStreamer"


@pytest.mark.parametrize(
    "mutate, expect",
    [
        (lambda d: d["streamers"][0].pop("permission"), "permission"),
        (lambda d: d["streamers"][0]["permission"].update(evidence="assets/permissions/nope.png"),
         "existing file"),
        (lambda d: d["streamers"][0]["permission"].update(granted_on=date.today() + timedelta(days=3)),
         "future"),
        (lambda d: d["streamers"].append(copy.deepcopy(d["streamers"][0])), "listed twice"),
        (lambda d: d["streamers"][0].update(login="Has Spaces"), "login"),
        (lambda d: d["streamers"][0].update(platform="tiktok"), "platform"),
        (lambda d: d.update(streamers=[]), "streamers"),
        (lambda d: d["clip"].update(pre_seconds=50, post_seconds=20), "exceeds video.max_seconds"),
    ],
)
def test_invalid_clip_channels(tmp_path, mutate, expect):
    data = _base()
    mutate(data)
    with pytest.raises(ConfigError, match=expect):
        load_channel(_write(tmp_path, data))


def test_unknown_format(tmp_path):
    data = _base()
    data["format"] = "podcast"
    with pytest.raises(ConfigError, match="format: must be one of"):
        load_channel(_write(tmp_path, data))


def test_evidence_file_in_project_is_accepted(tmp_path):
    data = _base()
    data["streamers"][0]["permission"]["evidence"] = "assets/fonts/Anton-OFL.txt"  # any existing file
    assert load_channel(_write(tmp_path, data)).streamers[0].permission.evidence.endswith("OFL.txt")

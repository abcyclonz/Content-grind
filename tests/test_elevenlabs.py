import base64
import json

import httpx
import pytest

from network import db
from network.providers.tts import ElevenLabsTTS, TTSError, words_from_alignment
from network.stages import voice

from test_voice_captions import CountingTTS, _scripted_job, needs_ffmpeg


def _alignment(text, step=0.1):
    chars = list(text)
    return {"characters": chars,
            "character_start_times_seconds": [i * step for i in range(len(chars))],
            "character_end_times_seconds": [(i + 1) * step for i in range(len(chars))]}


def test_words_from_alignment():
    words = words_from_alignment(_alignment("Hi  GTA 6!"))
    assert [(w.text, round(w.start, 2), round(w.end, 2)) for w in words] == [
        ("Hi", 0.0, 0.2), ("GTA", 0.4, 0.7), ("6!", 0.8, 1.0)]
    assert words_from_alignment(None) is None


def test_request_and_response():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        seen["key"] = req.headers["xi-api-key"]
        return httpx.Response(200, json={"audio_base64": base64.b64encode(b"ID3mp3").decode(),
                                         "alignment": _alignment("Hello there"),
                                         "normalized_alignment": None})

    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"xi-api-key": "k"})
    r = ElevenLabsTTS("k", "eleven_flash_v2_5", client=client).synthesize("Hello there", "VOICE1", 1.05)
    assert seen["url"] == ("https://api.elevenlabs.io/v1/text-to-speech/VOICE1/with-timestamps"
                           "?output_format=mp3_44100_128")
    assert seen["body"] == {"text": "Hello there", "model_id": "eleven_flash_v2_5",
                            "voice_settings": {"speed": 1.05}, "apply_text_normalization": "auto"}
    assert seen["key"] == "k"
    assert r.audio == b"ID3mp3" and r.ext == "mp3"
    assert [w.text for w in r.word_timings] == ["Hello", "there"]
    assert r.est_usd == pytest.approx(11 * 0.0001)


@pytest.mark.parametrize("status, body, match", [
    (401, "unauthorized", "rejected the API key"),
    (400, '{"detail": {"status": "quota_exceeded"}}', "credits used up"),
    (500, "boom", "HTTP 500"),
])
def test_errors(status, body, match):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, text=body)))
    with pytest.raises(TTSError, match=match):
        ElevenLabsTTS("k", "eleven_flash_v2_5", client=client).synthesize("x", "V", 1.0)


@needs_ffmpeg
def test_voice_stage_stops_cleanly_when_out_of_credits(conn, settings, channel, tmp_path):
    job = _scripted_job(conn, settings)

    class Broke(CountingTTS):
        def subscription(self):
            return {"character_count": 29_990, "character_limit": 30_000}

    tts = Broke(tmp_path)
    report = voice.run(conn, channel, settings, tts=tts)
    assert "credits left" in report.note and tts.calls == []
    assert db.get_job(conn, job)["state"] == "script_approved"   # not failed: retried next period

import base64
import json

import pytest

from conftest import with_changes
from network import db, media
from network.providers.tts import FixtureTTS, GoogleChirp3TTS, TTSError, TTSResult, WordTiming
from network.stages import captions, voice
from network.stages.captions import CaptionsFile
from network.stages.script import IdeaRef, ScriptFile, ScriptLine, count_words

try:
    media.ffmpeg()
    HAVE_FFMPEG = True
except media.MediaError:
    HAVE_FFMPEG = False
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")

LINES = ["Rockstar just confirmed the date.", "Pre-loading opens two days early.",
         "Your download starts on the seventeenth."]


def _scripted_job(conn, settings, lines=LINES, state="script_approved"):
    job = db.create_job(conn, "gta6", state=state)
    d = settings.jobs_dir / str(job)
    d.mkdir(parents=True)
    sl = [ScriptLine(text=t, visual_prompt="neon street") for t in lines]
    doc = ScriptFile(job_id=job, channel_id="gta6", idea=IdeaRef(id=1, title="t", angle="a"), model="m",
                     hook=sl[0].text, lines=sl, sources=["https://x.example/a"],
                     word_count=count_words(" ".join(lines)), created_at="now")
    (d / "script.json").write_text(doc.model_dump_json(), encoding="utf-8")
    with conn:
        conn.execute("INSERT INTO artifacts (job_id, kind, path, meta, created_at) VALUES (?, 'script', ?, '{}', 'now')",
                     (job, f"jobs/{job}/script.json"))
    return job


class CountingTTS(FixtureTTS):
    name = "counting"

    def __init__(self, workdir, timings=False):
        super().__init__(workdir)
        self.calls, self.timings = [], timings

    def synthesize(self, text, voice_id, speed):
        self.calls.append((text, voice_id, speed))
        r = super().synthesize(text, voice_id, speed)
        r.est_usd = len(text) * 30 / 1_000_000
        if self.timings:
            r.word_timings = [WordTiming(w, i * 0.4, i * 0.4 + 0.35) for i, w in enumerate(text.split())]
        return r


@needs_ffmpeg
def test_voice_stage_writes_normalized_mp3(conn, settings, channel, tmp_path):
    job = _scripted_job(conn, settings)
    tts = CountingTTS(tmp_path)
    report = voice.run(conn, channel, settings, tts=tts)
    assert report.ok and report.voiced[0][0] == job
    assert tts.calls == [(" ".join(LINES), channel.voice.voice_id, channel.voice.speed)]
    row = db.get_job(conn, job)
    assert row["state"] == "voiced"
    art = db.get_artifact(conn, job, "voice")
    info = media.probe(settings.data_dir / art["path"])
    assert art["path"].endswith("voice.mp3") and info["audio_codec"] == "mp3"
    assert abs(json.loads(art["meta"])["duration"] - info["duration"]) < 0.05
    cost = conn.execute("SELECT provider, operation, units FROM costs").fetchone()
    assert tuple(cost) == ("counting", "tts.synthesize", len(" ".join(LINES)))
    assert voice.run(conn, channel, settings, tts=tts).voiced == [] and len(tts.calls) == 1


@needs_ffmpeg
def test_voice_too_long_fails_job(conn, settings, channel, tmp_path):
    job = _scripted_job(conn, settings, lines=["word " * 200])
    ch = with_changes(channel, video={"max_seconds": 10})
    report = voice.run(conn, ch, settings, tts=CountingTTS(tmp_path))
    assert "over the 10s limit" in report.failed[0][1]
    assert db.get_job(conn, job)["failed_at_state"] == "script_approved"


@needs_ffmpeg
def test_captions_from_transcriber_cover_every_word(conn, settings, channel, tmp_path):
    job = _scripted_job(conn, settings)
    voice.run(conn, channel, settings, tts=CountingTTS(tmp_path))

    class FakeWhisper:
        used = "fake"

        def transcribe(self, audio, prompt=""):
            assert prompt == " ".join(LINES)
            words = " ".join(LINES).split()
            return [WordTiming(w.strip(".,").lower(), i * 0.3, i * 0.3 + 0.25) for i, w in enumerate(words)
                    if w != "Pre-loading"]  # one word misheard/missing

    live = settings.__class__(dry_run=False, data_dir=settings.data_dir, channels_dir=settings.channels_dir)
    report = captions.run(conn, channel, live, transcriber=FakeWhisper())
    assert report.ok and report.captioned[0][0] == job
    doc = CaptionsFile.model_validate_json((settings.jobs_dir / str(job) / "captions.json").read_text(encoding="utf-8"))
    assert [w.text for w in doc.words] == " ".join(LINES).split()   # script spelling, every word
    assert doc.words[5].text == "Pre-loading" and doc.words[5].end > doc.words[5].start
    assert [w.line for w in doc.words][:6] == [0, 0, 0, 0, 0, 1]
    assert doc.source == "whisper:fake" and 0.9 < doc.matched_ratio < 1
    assert db.get_job(conn, job)["state"] == "captioned"


@needs_ffmpeg
def test_captions_prefer_tts_timings(conn, settings, channel, tmp_path):
    job = _scripted_job(conn, settings)
    voice.run(conn, channel, settings, tts=CountingTTS(tmp_path, timings=True))

    class Boom:
        def transcribe(self, *a, **k):
            raise AssertionError("whisper should not run when the TTS gave timings")

    captions.run(conn, channel, settings, transcriber=Boom())
    doc = json.loads((settings.jobs_dir / str(job) / "captions.json").read_text(encoding="utf-8"))
    assert doc["source"] == "tts" and doc["matched_ratio"] == 1.0


@needs_ffmpeg
def test_mismatched_audio_fails_in_live_mode(conn, settings, channel, tmp_path):
    job = _scripted_job(conn, settings)
    voice.run(conn, channel, settings, tts=CountingTTS(tmp_path))

    class Wrong:
        used = "fake"

        def transcribe(self, *a, **k):
            return [WordTiming("completely", 0, 1), WordTiming("different", 1, 2)]

    live = settings.__class__(dry_run=False, data_dir=settings.data_dir, channels_dir=settings.channels_dir)
    report = captions.run(conn, channel, live, transcriber=Wrong())
    assert "may not match the script" in report.failed[0][1]


@needs_ffmpeg
def test_dry_run_end_to_end(conn, settings, channel):
    job = _scripted_job(conn, settings)
    assert voice.run(conn, channel, settings).ok          # fixture TTS
    assert captions.run(conn, channel, settings).ok       # no whisper: even spread
    assert db.get_job(conn, job)["state"] == "captioned"


class FakeSession:
    def __init__(self, status=200):
        self.status, self.sent = status, None

    def post(self, url, json, timeout):
        self.sent = (url, json)
        outer = self

        class R:
            status_code = outer.status
            text = "denied"

            def json(self):
                return {"audioContent": base64.b64encode(b"RIFFdata").decode()}
        return R()


def test_google_request_shape():
    s = FakeSession()
    r = GoogleChirp3TTS("unused", session=s).synthesize("Hello there.", "en-US-Chirp3-HD-Charon", 1.05)
    url, body = s.sent
    assert url == "https://texttospeech.googleapis.com/v1/text:synthesize"
    assert body == {"input": {"text": "Hello there."},
                    "voice": {"languageCode": "en-US", "name": "en-US-Chirp3-HD-Charon"},
                    "audioConfig": {"audioEncoding": "LINEAR16", "speakingRate": 1.05}}
    assert r.audio == b"RIFFdata" and r.ext == "wav" and r.word_timings is None
    assert r.est_usd == pytest.approx(12 * 30 / 1_000_000)


def test_google_errors():
    with pytest.raises(TTSError, match="not a Chirp 3 HD voice"):
        GoogleChirp3TTS("unused", session=FakeSession()).synthesize("x", "en-US-Neural2-A", 1.0)
    with pytest.raises(TTSError, match="HTTP 403"):
        GoogleChirp3TTS("unused", session=FakeSession(403)).synthesize("x", "en-US-Chirp3-HD-Charon", 1.0)

"""Text-to-speech providers behind one interface.

google_chirp3 (verified against docs.cloud.google.com/text-to-speech, 2026-11-17):
- POST https://texttospeech.googleapis.com/v1/text:synthesize
  {input: {text}, voice: {languageCode, name}, audioConfig: {audioEncoding, speakingRate}}
  -> {audioContent: base64}. LINEAR16 responses include a WAV header.
- Voice names: <locale>-Chirp3-HD-<Name>, e.g. en-US-Chirp3-HD-Charon.
- speakingRate 0.25..2.0 is supported. 5,000 bytes per request. 200 requests/min/project.
- Auth: OAuth scope cloud-platform (API keys are not documented), so a
  service-account key file is used.
- No word timepoints are returned: captions come from faster-whisper alignment.
- Price $30 per 1M characters after 1M free per month (Google pricing page).
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from network import media
from network.config import ChannelConfig, Settings

SYNTH_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
CHIRP3_USD_PER_CHAR = 30 / 1_000_000
MAX_REQUEST_BYTES = 5000


class TTSError(RuntimeError):
    pass


@dataclass
class WordTiming:
    text: str
    start: float
    end: float


@dataclass
class TTSResult:
    audio: bytes
    ext: str                                  # file extension of `audio`
    characters: int
    est_usd: float
    word_timings: list[WordTiming] | None = None  # None: provider gives no timings


class TTSProvider(Protocol):
    name: str

    def synthesize(self, text: str, voice_id: str, speed: float) -> TTSResult: ...


class GoogleChirp3TTS:
    name = "google_chirp3"

    def __init__(self, credentials_file: str, session=None):
        if session is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_file(
                credentials_file, scopes=["https://www.googleapis.com/auth/cloud-platform"])
            session = AuthorizedSession(creds)
        self.session = session

    def synthesize(self, text: str, voice_id: str, speed: float) -> TTSResult:
        if "-Chirp3-HD-" not in voice_id:
            raise TTSError(f"voice_id {voice_id!r} is not a Chirp 3 HD voice (e.g. en-US-Chirp3-HD-Charon)")
        if len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise TTSError(f"script is longer than {MAX_REQUEST_BYTES} bytes")
        language = "-".join(voice_id.split("-")[:2])
        body = {
            "input": {"text": text},
            "voice": {"languageCode": language, "name": voice_id},
            "audioConfig": {"audioEncoding": "LINEAR16", "speakingRate": speed},
        }
        r = self.session.post(SYNTH_URL, json=body, timeout=120)
        if r.status_code != 200:
            raise TTSError(f"Google TTS HTTP {r.status_code}: {r.text[:400]}")
        audio = base64.b64decode(r.json()["audioContent"])
        return TTSResult(audio=audio, ext="wav", characters=len(text),
                         est_usd=len(text) * CHIRP3_USD_PER_CHAR)


class ElevenLabsTTS:
    """ElevenLabs text-to-speech with character timestamps.

    Verified against elevenlabs.io/docs on 2026-11-17:
    - POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps
      header xi-api-key; body {text, model_id, voice_settings{speed,...}, apply_text_normalization}
      query output_format (mp3_44100_128 is allowed on Starter; 192 kbps needs Creator).
    - Response: audio_base64, alignment{characters, character_start_times_seconds,
      character_end_times_seconds} for the ORIGINAL text (normalized_alignment for the
      normalized text).
    - Starter: 30k credits/month; Multilingual v2 = 1 credit/char, Flash = 0.5-1 credit/char.
    """

    name = "elevenlabs"
    BASE = "https://api.elevenlabs.io"
    # Rough USD per character on Starter ($6 / 30k credits), for budget logging only.
    USD_PER_CHAR = {"eleven_flash_v2_5": 0.0001, "eleven_flash_v2": 0.0001}
    DEFAULT_USD_PER_CHAR = 0.0002

    def __init__(self, api_key: str, model_id: str, client=None):
        import httpx
        self.model_id = model_id
        self.client = client or httpx.Client(timeout=120, headers={"xi-api-key": api_key})

    def synthesize(self, text: str, voice_id: str, speed: float) -> TTSResult:
        r = self.client.post(
            f"{self.BASE}/v1/text-to-speech/{voice_id}/with-timestamps",
            params={"output_format": "mp3_44100_128"},
            json={"text": text, "model_id": self.model_id,
                  "voice_settings": {"speed": speed}, "apply_text_normalization": "auto"},
        )
        if r.status_code == 401:
            raise TTSError(f"ElevenLabs rejected the API key (TTS_API_KEY in .env): {r.text[:300]}")
        if r.status_code != 200:
            detail = r.text[:400]
            if "quota" in detail.lower() or "credits" in detail.lower():
                raise TTSError(f"ElevenLabs credits used up for this month: {detail}")
            raise TTSError(f"ElevenLabs HTTP {r.status_code}: {detail}")
        data = r.json()
        rate = self.USD_PER_CHAR.get(self.model_id, self.DEFAULT_USD_PER_CHAR)
        return TTSResult(audio=base64.b64decode(data["audio_base64"]), ext="mp3",
                         characters=len(text), est_usd=len(text) * rate,
                         word_timings=words_from_alignment(data.get("alignment")))

    def subscription(self) -> dict:
        """Credits used/left this period (GET /v1/user/subscription)."""
        r = self.client.get(f"{self.BASE}/v1/user/subscription")
        if r.status_code != 200:
            raise TTSError(f"ElevenLabs HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def voices(self, search: str | None = None) -> list[dict]:
        params = {"page_size": 100, "voice_type": "default"}
        if search:
            params["search"] = search
        r = self.client.get(f"{self.BASE}/v2/voices", params=params)
        if r.status_code != 200:
            raise TTSError(f"ElevenLabs HTTP {r.status_code}: {r.text[:200]}")
        return r.json().get("voices", [])


def words_from_alignment(alignment: dict | None) -> list[WordTiming] | None:
    """Group character timestamps into words (split on whitespace)."""
    if not alignment or not alignment.get("characters"):
        return None
    chars = alignment["characters"]
    starts = alignment["character_start_times_seconds"]
    ends = alignment["character_end_times_seconds"]
    words, buf, s, e = [], "", 0.0, 0.0
    for ch, cs, ce in zip(chars, starts, ends):
        if ch.isspace():
            if buf:
                words.append(WordTiming(buf, s, e))
                buf = ""
            continue
        if not buf:
            s = cs
        buf += ch
        e = ce
    if buf:
        words.append(WordTiming(buf, s, e))
    return words


class FixtureTTS:
    """DRY_RUN: a quiet tone as long as the text would take to say (about 2.6 words/s)."""

    name = "fixture"

    def __init__(self, workdir: Path):
        self.workdir = workdir

    def synthesize(self, text: str, voice_id: str, speed: float) -> TTSResult:
        words = len(re.findall(r"\S+", text))
        seconds = max(1.0, words / (2.6 * speed))
        self.workdir.mkdir(parents=True, exist_ok=True)
        out = self.workdir / "fixture_voice.wav"
        media.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                   "-i", f"sine=frequency=220:sample_rate=24000:duration={seconds:.2f}",
                   "-af", "volume=0.2", "-ac", "1", str(out)])
        return TTSResult(audio=out.read_bytes(), ext="wav", characters=len(text), est_usd=0.0)


def make_tts(channel: ChannelConfig, settings: Settings) -> TTSProvider:
    if settings.dry_run:
        return FixtureTTS(settings.data_dir / "tts_fixture")
    if channel.voice.provider == "elevenlabs":
        if not channel.voice.model:
            raise TTSError("voice.model is required for elevenlabs (e.g. eleven_flash_v2_5)")
        return ElevenLabsTTS(settings.secret("TTS_API_KEY"), channel.voice.model)
    if channel.voice.provider == "google_chirp3":
        return GoogleChirp3TTS(settings.secret("GOOGLE_TTS_CREDENTIALS_FILE"))
    raise TTSError(f"unknown voice provider {channel.voice.provider!r} (supported: elevenlabs, google_chirp3)")

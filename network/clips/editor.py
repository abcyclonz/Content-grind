"""Turn a raw stream clip into our own vertical Short.

    raw clip (landscape, streamer audio)
      -> 9:16 frame ("crop" fills the screen, "pad" shows the full frame over a blur)
      -> streamer audio removed
      -> our narration (ElevenLabs, or a recording we made) + word-highlight captions
      -> optional hook title at the top and a footage credit at the bottom

The rendering is the same code as the news Shorts (network.render), with the clip as
the only background scene. Without TTS_API_KEY / with DRY_RUN=1 the narration is a
placeholder tone, which is enough to check timing, framing and captions.
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from network import media
from network.align import NullTranscriber, WhisperTranscriber, align, script_words
from network.config import ClipSettings, Settings, Video
from network.providers.tts import ElevenLabsTTS, FixtureTTS, TTSProvider, WordTiming
from network.render import TAIL, Scene, build_ass, render_short

log = logging.getLogger("clips.editor")

SHORT = Video(width=1080, height=1920, fps=30, max_seconds=59, caption_style="word_highlight")
DEFAULT_VOICE = "TX3LPaxmHKxFdv7VOQHJ"      # Liam, same as the news channel
DEFAULT_TTS_MODEL = "eleven_flash_v2_5"
AFTER_REACTION = 3.0    # seconds kept after chat reacted, by default


class EditError(RuntimeError):
    pass


@dataclass
class EditResult:
    out: Path
    duration: float
    voice_source: str
    timing_source: str
    warnings: list[str]


def script_lines(text: str) -> list[str]:
    """One caption line per non-empty line of the script."""
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    if not lines:
        raise EditError("the script is empty")
    return lines


def make_tts(settings: Settings) -> TTSProvider:
    if settings.dry_run:
        return FixtureTTS(settings.data_dir / "tts_fixture")
    return ElevenLabsTTS(settings.secret("TTS_API_KEY"), DEFAULT_TTS_MODEL)


def edit_clip(
    clip: Path,
    script: str,
    out: Path,
    settings: Settings,
    *,
    voice_file: Path | None = None,
    fit: str = "crop",
    hook: str = "",
    credit: str = "",
    start: float | None = None,
    tts: TTSProvider | None = None,
    transcriber=None,
    voice_id: str = DEFAULT_VOICE,
    speed: float = 1.05,
) -> EditResult:
    if fit not in ("crop", "pad"):
        raise EditError("fit must be 'crop' or 'pad'")
    clip = Path(clip)
    if not clip.is_file():
        raise EditError(f"clip not found: {clip}")
    lines = script_lines(script)
    spoken = " ".join(lines)
    work = settings.data_dir / "edit_work" / out.stem
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    warnings: list[str] = []

    full_len = media.probe(clip)["duration"]
    if start is not None and full_len - start <= 1:
        raise EditError(f"the clip is only {full_len - start:.1f}s long after --start")
    clip_len = full_len - (start or 0.0)

    # 1) narration
    heard: list[WordTiming] | None = None
    if voice_file:
        raw = Path(voice_file)
        if not raw.is_file():
            raise EditError(f"voice file not found: {raw}")
        voice_source = f"recording ({raw.name})"
    else:
        tts = tts or make_tts(settings)
        res = tts.synthesize(spoken, voice_id, speed)
        raw = work / f"voice_raw.{res.ext}"
        raw.write_bytes(res.audio)
        heard = res.word_timings
        voice_source = getattr(tts, "name", "tts")
    voice = work / "narration.mp3"   # render_short copies it to voice.mp3 in the same folder
    media.normalize_voice(raw, voice)
    voice_len = media.probe(voice)["duration"]

    # 2) word timings for the captions
    if heard is not None:
        timing_source = "tts"
    else:
        if transcriber is None:
            # A real recording is worth transcribing even in dry run (Whisper is local and free);
            # the placeholder tone has no words in it.
            transcriber = (WhisperTranscriber("small", "cpu", "en")
                           if voice_file or not settings.dry_run else NullTranscriber())
        heard = transcriber.transcribe(voice, spoken)
        timing_source = f"whisper:{getattr(transcriber, 'used', '?')}" if heard else "spread evenly"
    pairs = script_words(lines)
    result = align([w for w, _ in pairs], heard, voice_len)
    if heard and result.matched_ratio < 0.6:
        warnings.append(f"only {result.matched_ratio:.0%} of script words were heard; check caption sync")
    captions = {"duration": voice_len,
                "words": [{"text": w.text, "start": w.start, "end": w.end, "line": line}
                          for w, (_, line) in zip(result.words, pairs)]}

    # 3) length: the narration decides, capped by the clip and the Shorts limit
    total = min(voice_len + TAIL, clip_len, float(SHORT.max_seconds))
    if voice_len + TAIL > clip_len:
        warnings.append(f"narration is {voice_len:.1f}s but the clip is {clip_len:.1f}s; "
                        "the end of the narration is cut. Shorten the script or use a longer clip.")
    if voice_len + TAIL > SHORT.max_seconds:
        warnings.append(f"narration is longer than {SHORT.max_seconds}s; it is cut at the Shorts limit")

    # 4) render. Clips from `clips find` end post_seconds after chat reacted, and the moment is
    # just before the reaction. By default the edit ends a few seconds after the reaction, so a
    # short narration still shows the build-up and the moment, not the aftermath.
    end_at = min(full_len, max(total, full_len - ClipSettings().post_seconds + AFTER_REACTION))
    offset = start if start is not None else max(0.0, end_at - total)
    scenes = [Scene(start=0.0, end=total, source="video", path=clip.resolve(), offset=offset, fit=fit)]
    ass = build_ass(captions, SHORT, hook=hook, credit=credit, total=total)
    info = render_short(scenes, voice, ass, out, SHORT, work)
    log.info("edited %s -> %s (%.1fs)", clip.name, out, info.get("duration", 0))
    return EditResult(out=out, duration=info.get("duration", total), voice_source=voice_source,
                      timing_source=timing_source, warnings=warnings)

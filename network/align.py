"""Word timing: transcribe the voice with faster-whisper and align it to the script.

The captions must show the script's own words (the TTS read the script), so
whisper is only used for timing. Script words that whisper heard differently
("nine point seven" vs "9.7") get times interpolated between their matched
neighbours, weighted by word length, so every script word gets a timestamp.
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from network.providers.tts import WordTiming

log = logging.getLogger("captions")
MIN_WORD = 0.05


def script_words(lines: list[str]) -> list[tuple[str, int]]:
    """(word as written, line index) for every spoken word; punctuation-only tokens are dropped."""
    return [(tok, i) for i, line in enumerate(lines) for tok in line.split() if re.search(r"\w", tok)]


def norm(word: str) -> str:
    return re.sub(r"[^0-9a-z]", "", word.lower())


@dataclass
class Alignment:
    words: list[WordTiming]
    matched: int

    @property
    def matched_ratio(self) -> float:
        return self.matched / len(self.words) if self.words else 0.0


def align(words: list[str], heard: list[WordTiming], duration: float) -> Alignment:
    a = [norm(w) for w in words]
    b = [norm(h.text) for h in heard]
    times: list[tuple[float, float] | None] = [None] * len(words)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    matched = 0
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            h = heard[block.b + k]
            times[block.a + k] = (h.start, h.end)
            matched += 1

    # Interpolate runs of unmatched words between known neighbours.
    i = 0
    while i < len(words):
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(words) and times[j] is None:
            j += 1
        left = times[i - 1][1] if i > 0 else 0.0
        right = times[j][0] if j < len(words) else duration
        if right <= left:
            right = min(duration, left + MIN_WORD * (j - i))
        weights = [max(1, len(a[k])) for k in range(i, j)]
        total, t = sum(weights), left
        for k, w in zip(range(i, j), weights):
            span = (right - left) * w / total
            times[k] = (t, t + span)
            t += span
        i = j

    # Forward pass: monotonic and non-overlapping.
    spans, prev_end = [], 0.0
    for s, e in times:
        s = max(s, prev_end)
        e = max(e, s + MIN_WORD)
        spans.append([s, e])
        prev_end = e
    # Backward pass: pull anything past the end of the audio back inside,
    # keeping every word at least MIN_WORD long.
    limit = duration
    for span in reversed(spans):
        span[1] = min(span[1], limit)
        span[0] = max(0.0, min(span[0], span[1] - MIN_WORD))
        limit = span[0]
    out = [WordTiming(w, round(s, 3), round(e, 3)) for w, (s, e) in zip(words, spans)]
    return Alignment(out, matched)


class WhisperTranscriber:
    """faster-whisper with word timestamps; GPU float16 if it works, else CPU int8."""

    def __init__(self, model: str = "small", device: str = "auto", language: str = "en"):
        self.model_name, self.device, self.language = model, device, language
        self._model = None
        self.used = ""

    def _load(self, device: str):
        from faster_whisper import WhisperModel
        compute = "float16" if device == "cuda" else "int8"
        self._model = WhisperModel(self.model_name, device=device, compute_type=compute)
        self.used = f"{self.model_name}/{device}/{compute}"

    def _run(self, audio: Path, prompt: str) -> list[WordTiming]:
        segments, _ = self._model.transcribe(
            str(audio), language=self.language, word_timestamps=True,
            initial_prompt=prompt[:800], vad_filter=False, beam_size=5,
        )
        return [WordTiming(w.word.strip(), w.start, w.end) for seg in segments for w in (seg.words or [])]

    def transcribe(self, audio: Path, prompt: str = "") -> list[WordTiming]:
        order = ["cuda", "cpu"] if self.device == "auto" else [self.device]
        last: Exception | None = None
        for dev in order:
            try:
                if self._model is None or not self.used.split("/")[1:2] == [dev]:
                    self._load(dev)
                return self._run(audio, prompt)
            except Exception as e:  # noqa: BLE001 - e.g. missing CUDA libraries: try the CPU
                last = e
                self._model = None
                log.warning("whisper on %s failed (%s); %s", dev, str(e)[:200],
                            "trying CPU" if dev == "cuda" and len(order) > 1 else "giving up")
        raise RuntimeError(f"whisper transcription failed: {last}")


class NullTranscriber:
    """DRY_RUN: hears nothing, so timings are spread evenly over the audio."""
    used = "none (dry run)"

    def transcribe(self, audio: Path, prompt: str = "") -> list[WordTiming]:
        return []

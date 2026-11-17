"""Channel signature sounds, synthesized locally (original audio, no licensing).

make_honk(): a cartoon double goose honk. Each honk is a sawtooth whose pitch
falls about 28%, pushed through a nasal resonance (band-pass ~1.15 kHz plus a
presence boost), soft-clipped for the reedy rasp, with a fast attack.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from network import media


def _honk(out: Path, f0: float, dur: float, rate: int = 48000) -> None:
    k = f0 * 0.28 / (2 * dur)          # phase = f0*t - k*t^2  -> frequency falls linearly
    phase = f"({f0}*t-{k:.4f}*t*t)"
    expr = f"0.9*(2*({phase}-floor({phase}+0.5)))"
    media.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
               "-i", f"aevalsrc='{expr}':s={rate}:d={dur}",
               "-af", (f"bandpass=f=1150:width_type=q:w=1.4,equalizer=f=2600:t=q:w=2:g=6,"
                       f"highpass=f=280,asoftclip=type=tanh,volume=4,"
                       f"afade=t=in:d=0.015,afade=t=out:st={dur - 0.07:.3f}:d=0.07"),
               str(out)])


def make_honk(out: Path, base_hz: float = 410.0) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        _honk(t / "h1.wav", base_hz, 0.22)
        _honk(t / "h2.wav", base_hz * 1.15, 0.26)
        media.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                   "-i", str(t / "h1.wav"), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=0.09",
                   "-i", str(t / "h2.wav"), "-filter_complex",
                   "[0:a][1:a][2:a]concat=n=3:v=0:a=1,aecho=0.6:0.3:35:0.12,loudnorm=I=-16:TP=-2:LRA=7",
                   "-ar", "48000", "-ac", "1", str(out)])
    return out

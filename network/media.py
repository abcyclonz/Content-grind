"""FFmpeg helpers: locate the binaries, pick an encoder (NVENC, else libx264)."""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path

from network.config import PROJECT_ROOT

_EXE = ".exe" if os.name == "nt" else ""


class MediaError(RuntimeError):
    pass


def _find(name: str, env_var: str) -> str:
    override = os.environ.get(env_var, "").strip()
    if override:
        return override
    local = PROJECT_ROOT / "tools" / "ffmpeg" / "bin" / f"{name}{_EXE}"
    if local.exists():
        return str(local)
    found = shutil.which(name)
    if not found:
        raise MediaError(f"{name} not found: put it on PATH, in tools/ffmpeg/bin, or set {env_var}")
    return found


def ffmpeg() -> str:
    return _find("ffmpeg", "FFMPEG_PATH")


def ffprobe() -> str:
    return _find("ffprobe", "FFPROBE_PATH")


@functools.lru_cache(maxsize=1)
def h264_encoder() -> tuple[str, ...]:
    """Encoder arguments. Tries h264_nvenc with a one-second test encode, else libx264."""
    test = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "testsrc2=size=256x256:rate=30", "-t", "0.2", "-c:v", "h264_nvenc", "-f", "null", "-"]
    try:
        if subprocess.run(test, capture_output=True, timeout=30).returncode == 0:
            return ("-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ("-c:v", "libx264", "-preset", "veryfast", "-crf", "20")


def run(args: list[str], timeout: float = 600, cwd: Path | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, cwd=cwd)
    if proc.returncode != 0:
        raise MediaError(f"{Path(args[0]).name} exit {proc.returncode}: {proc.stderr.strip()[-800:]}")
    return proc


LOUDNORM = "I=-14:TP=-1.5:LRA=11"  # typical loudness target for mobile/short-form


def normalize_voice(src: Path, dst: Path) -> dict:
    """Two-pass EBU R128 loudnorm, written as 48 kHz MP3. Returns the first-pass measurements."""
    import json
    import re
    first = subprocess.run(
        [ffmpeg(), "-hide_banner", "-nostats", "-i", str(src), "-af",
         f"loudnorm={LOUDNORM}:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    found = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", first.stderr)
    if first.returncode != 0 or not found:
        raise MediaError(f"loudnorm measurement failed: {first.stderr.strip()[-400:]}")
    m = json.loads(found[-1])
    second = (f"loudnorm={LOUDNORM}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
              f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
              f"offset={m['target_offset']}:linear=true")
    tmp = dst.with_name(dst.stem + ".part" + dst.suffix)
    run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-af", second,
         "-ar", "48000", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "192k", str(tmp)], timeout=300)
    import os
    os.replace(tmp, dst)
    return m


def probe(path: Path) -> dict:
    """Duration and first video stream size via ffprobe."""
    import json
    out = run([ffprobe(), "-v", "error", "-print_format", "json", "-show_format",
               "-show_streams", str(path)], timeout=60).stdout
    data = json.loads(out)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    return {
        "duration": float(data.get("format", {}).get("duration") or 0),
        "width": video.get("width"),
        "height": video.get("height"),
        "video_codec": video.get("codec_name"),
        "fps": video.get("r_frame_rate"),
        "audio_codec": audio.get("codec_name") if audio else None,
    }

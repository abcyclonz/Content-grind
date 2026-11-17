"""Cut a window out of recorded segments and render a 9:16 clip with a credit line."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from network import media
from network.clips.recorder import Segment
from network.config import PROJECT_ROOT, ClipVideo

FONT = PROJECT_ROOT / "assets" / "fonts" / "Anton-Regular.ttf"
MIN_CLIP_SECONDS = 5


def _filter(layout: str, v: ClipVideo) -> str:
    W, H, fps = v.width, v.height, v.fps
    size = int(H * 0.034)
    credit = (f"drawtext=fontfile=font.ttf:textfile=credit.txt:fontsize={size}:fontcolor=white:"
              f"borderw=5:bordercolor=black@0.85:x=(w-text_w)/2:y=h*0.16")
    if layout == "center_crop":
        return f"[0:v]fps={fps},scale=-2:{H},crop={W}:{H},setsar=1,{credit}[v]"
    # blur_pad: full frame in the middle, blurred zoomed copy behind it.
    # The background is blurred at quarter size, which is much faster.
    return (
        f"[0:v]fps={fps},split=2[a][b];"
        f"[a]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
        f"boxblur=luma_radius=12:luma_power=2,scale={W}:{H},eq=brightness=-0.12[bg];"
        f"[b]scale={W}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,{credit}[v]"
    )


def render_clip(
    segments: list[Segment],
    t0: float,
    t1: float,
    out: Path,
    *,
    video: ClipVideo,
    layout: str,
    credit_text: str,
    workdir: Path,
) -> dict:
    """Render [t0, t1] (wall-clock seconds) to out. Returns ffprobe info of the result."""
    segs = sorted((s for s in segments if s.end > t0 and s.start < t1), key=lambda s: s.start)
    if not segs:
        raise media.MediaError("no recorded video covers the clip window")
    t0, t1 = max(t0, segs[0].start), min(t1, segs[-1].end)
    duration = min(t1 - t0, float(video.max_seconds))
    if duration < MIN_CLIP_SECONDS:
        raise media.MediaError(f"only {duration:.1f}s of video available for this clip")

    workdir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FONT, workdir / "font.ttf")  # relative names avoid Windows path escaping
    (workdir / "credit.txt").write_text(credit_text, encoding="utf-8")
    listing = workdir / "inputs.txt"
    listing.write_text(
        "ffconcat version 1.0\n" + "".join(
            "file '{}'\n".format(str(s.path.resolve()).replace("\\", "/").replace("'", r"'\''"))
            for s in segs),
        encoding="utf-8",
    )

    tmp = out.with_name(out.stem + ".part.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    base = [media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{t0 - segs[0].start:.3f}", "-f", "concat", "-safe", "0", "-i", "inputs.txt",
            "-t", f"{duration:.3f}", "-filter_complex", _filter(layout, video),
            "-map", "[v]", "-map", "0:a:0?", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-pix_fmt", "yuv420p", "-r", str(video.fps), "-movflags", "+faststart"]
    try:
        media.run(base + list(media.h264_encoder()) + [str(tmp.resolve())], cwd=workdir)
    except media.MediaError:
        if "h264_nvenc" not in media.h264_encoder():
            raise
        media.run(base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(tmp.resolve())],
                  cwd=workdir)
    os.replace(tmp, out)
    return media.probe(out)

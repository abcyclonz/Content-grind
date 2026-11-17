"""Short video rendering: scene backgrounds, crossfades, ASS captions, audio.

Layers, bottom to top:
  1. one background clip per script line (image with slow zoom, or a local
     animated neon gradient), joined with crossfades
  2. a dark tint so text stays readable
  3. hook title (top, first seconds), word-highlight captions (middle),
     source credit (bottom), all drawn by libass from one .ass file
  4. the voice track (optional music ducked under it)

FFmpeg runs with cwd = the job's render folder and only relative file names,
which sidesteps Windows path escaping inside filter strings.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from network import media
from network.config import PROJECT_ROOT, Video

FONT_FILE = PROJECT_ROOT / "assets" / "fonts" / "Anton-Regular.ttf"
FONT_NAME = "Anton"
FADE = 0.35            # crossfade between scenes, seconds
TAIL = 0.4             # silence kept after the last word
HOOK_SECONDS = 2.8
CHUNK_WORDS = 3
CHUNK_CHARS = 18
YELLOW = "&H00FFFF&"   # ASS colours are BGR

# Neon palettes for procedural backgrounds (cycled per scene).
PALETTES = [
    ("0xff2bd6", "0x2b0a5e"), ("0x00e5ff", "0x1a0b3d"), ("0xff7a18", "0x3d0b2b"),
    ("0x7c4dff", "0x00172e"), ("0xff3d71", "0x0b1f3d"), ("0x18ffb2", "0x240b3d"),
]


@dataclass
class Scene:
    start: float
    end: float
    source: str                 # "procedural" | "image" | "video"
    path: Path | None = None    # image or video file
    palette: int = 0
    offset: float = 0.0         # where to start reading a video file, seconds

    @property
    def duration(self) -> float:
        return self.end - self.start


# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------

def scene_spans(captions: dict, n_lines: int) -> list[tuple[float, float]]:
    """Line i runs from its first word to the next line's first word; the last ends after a tail."""
    firsts: dict[int, float] = {}
    for w in captions["words"]:
        firsts.setdefault(w["line"], w["start"])
    starts = [0.0] + [firsts.get(i, 0.0) for i in range(1, n_lines)]
    for i in range(1, n_lines):  # a line with no words: share the gap evenly
        if starts[i] <= starts[i - 1]:
            starts[i] = starts[i - 1] + 0.5
    end = captions["duration"] + TAIL
    return [(starts[i], starts[i + 1] if i + 1 < n_lines else end) for i in range(n_lines)]


# --------------------------------------------------------------------------
# Captions (.ass)
# --------------------------------------------------------------------------

def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ass_text(text: str) -> str:
    return re.sub(r"[{}\\]", "", text).replace("\n", " ")


def chunk_words(words: list[dict]) -> list[list[dict]]:
    """2-3 words per caption, breaking at script lines, sentence ends and long chunks."""
    chunks, cur = [], []
    for w in words:
        if cur and (w["line"] != cur[-1]["line"] or len(cur) >= CHUNK_WORDS
                    or len(" ".join(x["text"] for x in cur + [w])) > CHUNK_CHARS):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?]$", w["text"]):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(captions: dict, video: Video, hook: str, credit: str, total: float,
              outro_text: str = "", outro_start: float | None = None) -> str:
    W, H = video.width, video.height
    big = int(H * 0.052)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{FONT_NAME},{big},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,1,0,1,{int(big * 0.09)},{int(big * 0.05)},5,60,60,0,1
Style: Hook,{FONT_NAME},{int(H * 0.042)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,1,0,1,{int(H * 0.004)},0,8,90,90,{int(H * 0.11)},1
Style: Outro,{FONT_NAME},{int(H * 0.07)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,2,0,1,{int(H * 0.006)},{int(H * 0.003)},5,40,40,0,1
Style: Credit,{FONT_NAME},{int(H * 0.017)},&H00E6E6E6,&H00E6E6E6,&H00000000,&H96000000,0,0,0,0,100,100,1,0,1,{int(H * 0.002)},0,2,60,60,{int(H * 0.06)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    cx, cy = W // 2, int(H * 0.66)
    chunks = chunk_words(captions["words"])
    for ci, chunk in enumerate(chunks):
        next_start = chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else total
        chunk_end = next_start if next_start - chunk[-1]["end"] < 0.4 else chunk[-1]["end"] + 0.15
        for wi, w in enumerate(chunk):
            start = w["start"]
            end = chunk[wi + 1]["start"] if wi + 1 < len(chunk) else chunk_end
            parts = []
            for k, x in enumerate(chunk):
                t = _ass_text(x["text"]).upper()
                parts.append(f"{{\\c{YELLOW}\\fscx112\\fscy112}}{t}{{\\r}}" if k == wi else t)
            events.append(f"Dialogue: 1,{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,"
                          f"{{\\pos({cx},{cy})}}{' '.join(parts)}")
    if hook:
        events.append(f"Dialogue: 2,{_ass_time(0)},{_ass_time(min(HOOK_SECONDS, total))},Hook,,0,0,0,,"
                      f"{{\\fad(150,250)}}{_ass_text(hook).upper()}")
    if credit:
        events.append(f"Dialogue: 0,{_ass_time(0)},{_ass_time(total)},Credit,,0,0,0,,{_ass_text(credit)}")
    if outro_text and outro_start is not None:
        # pop in: grow past full size, settle, fade at the very end
        pop = "\\fad(60,120)\\fscx40\\fscy40\\t(0,160,\\fscx118\\fscy118)\\t(160,300,\\fscx100\\fscy100)"
        events.append(f"Dialogue: 3,{_ass_time(outro_start)},{_ass_time(total)},Outro,,0,0,0,,"
                      f"{{{pop}\\pos({W // 2},{int(H * 0.45)})}}{_ass_text(outro_text).upper()}")
    return head + "\n".join(events) + "\n"


def sources_credit(urls: list[str]) -> str:
    hosts = []
    for u in urls:
        h = urlsplit(u).netloc.lower().removeprefix("www.")
        if h and h not in hosts:
            hosts.append(h)
    return ("Sources: " + ", ".join(hosts[:3])) if hosts else ""


# --------------------------------------------------------------------------
# Scenes
# --------------------------------------------------------------------------

def _scene_args(scene: Scene, length: float, video: Video, index: int) -> list[str]:
    W, H, fps = video.width, video.height, video.fps
    if scene.source == "video" and scene.path:
        # Full frame in the middle over a blurred, zoomed copy (quarter-size blur is much faster).
        # Live captures often have timestamps that do not start at 0 (and may jump), so
        # reset them before anything else and let the output -t do the cutting.
        return ["-ss", f"{scene.offset:.3f}", "-i", str(scene.path),
                "-filter_complex",
                f"[0:v]setpts=PTS-STARTPTS,fps={fps},split=2[a][b];"
                f"[a]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
                f"boxblur=luma_radius=12:luma_power=2,scale={W}:{H},eq=brightness=-0.15[bg];"
                f"[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]",
                "-map", "[v]"]
    if scene.source == "image" and scene.path:
        frames = max(1, int(round(length * fps)))
        zoom = "min(zoom+0.0009,1.12)" if index % 2 == 0 else "if(eq(on,0),1.12,max(zoom-0.0009,1.0))"
        return ["-loop", "1", "-t", f"{length:.3f}", "-i", str(scene.path), "-vf",
                f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
                f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={fps},"
                f"setsar=1"]
    c0, c1 = PALETTES[scene.palette % len(PALETTES)]
    return ["-f", "lavfi", "-i",
            f"gradients=s={W}x{H}:r={fps}:c0={c0}:c1={c1}:nb_colors=2:type={index % 2}:speed=0.015:"
            f"seed={index + 7}:d={length:.3f}",
            "-vf", "vignette=PI/4,noise=alls=6:allf=t,setsar=1"]


def render_short(
    scenes: list[Scene],
    voice: Path,
    ass_text: str,
    out: Path,
    video: Video,
    workdir: Path,
    music: Path | None = None,
    sting: Path | None = None,
    sting_at: float = 0.0,
) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    if sting:
        shutil.copyfile(sting, workdir / f"sting{sting.suffix}")
    fonts = workdir / "fonts"
    fonts.mkdir(exist_ok=True)
    shutil.copyfile(FONT_FILE, fonts / FONT_FILE.name)
    (workdir / "captions.ass").write_text(ass_text, encoding="utf-8")
    shutil.copyfile(voice, workdir / "voice.mp3")
    if music:
        shutil.copyfile(music, workdir / f"music{music.suffix}")

    # 1) one short intermediate clip per scene (fast, high quality)
    names = []
    for i, sc in enumerate(scenes):
        length = sc.duration + (FADE if i < len(scenes) - 1 else 0.0)
        name = f"scene_{i}.mp4"
        media.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                   *_scene_args(sc, length, video, i), "-t", f"{length:.3f}", "-an",
                   "-avoid_negative_ts", "make_zero",
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-pix_fmt", "yuv420p", name],
                  cwd=workdir, timeout=600)
        names.append(name)

    # 2) crossfades + tint + captions + audio in one final encode
    inputs = []
    for n in names:
        inputs += ["-i", n]
    inputs += ["-i", "voice.mp3"]
    voice_idx = len(names)
    # xfade needs every input to start at t=0.
    graph = [f"[{i}:v]setpts=PTS-STARTPTS[s{i}]" for i in range(len(names))]
    last, offset = "[s0]", 0.0
    for i in range(1, len(names)):
        offset += scenes[i - 1].duration
        graph.append(f"{last}[s{i}]xfade=transition=fade:duration={FADE}:offset={offset:.3f}[x{i}]")
        last = f"[x{i}]"
    graph.append(f"{last}drawbox=x=0:y=0:w=iw:h=ih:color=black@0.28:t=fill,"
                 f"ass=captions.ass:fontsdir=fonts,format=yuv420p[v]")
    # Audio: everything mixed in mono, then copied to both channels at full level
    # (plain -ac 2 drops 3 dB; some players are silent on mono AAC).
    mono_fmt = "aformat=sample_rates=48000:channel_layouts=mono"
    nxt = voice_idx + 1
    if music:
        inputs += ["-stream_loop", "-1", "-i", f"music{music.suffix}"]
        graph.append(f"[{nxt}:a]{mono_fmt},volume=0.35[mu];[{voice_idx}:a]{mono_fmt},asplit=2[vo][sc];"
                     f"[mu][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[duck];"
                     f"[vo][duck]amix=inputs=2:duration=first:normalize=0[mix0]")
        nxt += 1
    else:
        graph.append(f"[{voice_idx}:a]{mono_fmt}[mix0]")
    mix = "[mix0]"
    if sting:
        inputs += ["-i", f"sting{sting.suffix}"]
        graph.append(f"[{nxt}:a]{mono_fmt},adelay={int(sting_at * 1000)}:all=1[st];"
                     f"{mix}[st]amix=inputs=2:duration=longest:normalize=0[mix1]")
        mix = "[mix1]"
    graph.append(f"{mix}pan=stereo|c0=c0|c1=c0[a]")
    audio_map = "[a]"
    total = sum(s.duration for s in scenes)
    tmp = "final.part.mp4"
    base = [media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *inputs,
            "-filter_complex", ";".join(graph), "-map", "[v]", "-map", audio_map,
            "-t", f"{total:.3f}", "-r", str(video.fps),
            # stereo (built in the graph): some players and phones stay silent on mono AAC
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart"]
    try:
        media.run(base + list(media.h264_encoder()) + [tmp], cwd=workdir, timeout=1800)
    except media.MediaError:
        if "h264_nvenc" not in media.h264_encoder():
            raise
        media.run(base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", tmp],
                  cwd=workdir, timeout=1800)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(workdir / tmp, out)
    except OSError:  # e.g. work folder on another drive
        shutil.move(str(workdir / tmp), str(out))
    return media.probe(out)

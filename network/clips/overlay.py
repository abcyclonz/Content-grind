"""Caption-box Shorts: a white rounded box with bold text (and emoji) over the clip.

The common "story" style on Shorts and Reels:

    +-------------------------------+
    |   blurred copy of the clip    |
    |  +-------------------------+  |
    |  | Speed made Adin Ross    |  |   <- caption box (PNG drawn with Pillow)
    |  | tap out in front of ... |  |
    |  +-------------------------+  |
    |  [ the clip, large, middle ]  |
    |                               |
    |  footage credit               |
    +-------------------------------+

Unlike `clips edit`, the original audio is kept by default (pass mute=True to drop it).
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from network import media
from network.config import PROJECT_ROOT

W, H, FPS = 1080, 1920, 30
BOX_MARGIN = 60          # box distance from the frame edges
BOX_PAD_X, BOX_PAD_Y = 44, 30
TEXT_SIZE = 66
LINE_GAP = 10
VIDEO_TOP = 0.33         # clip starts at this share of the frame height
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+")


class OverlayError(RuntimeError):
    pass


def _font_candidates(kind: str) -> list[Path]:
    win = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    if kind == "emoji":
        return [win / "seguiemj.ttf", Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
                Path("/System/Library/Fonts/Apple Color Emoji.ttc")]
    return [PROJECT_ROOT / "assets" / "fonts" / "CaptionBold.ttf",   # drop a licensed font here to override
            win / "arialbd.ttf", win / "segoeuib.ttf",
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")]


def _load_font(kind: str, size: int):
    from PIL import ImageFont
    for p in _font_candidates(kind):
        if p.is_file():
            try:
                # Color emoji fonts only come in fixed sizes (109 for Noto); Segoe scales freely.
                return ImageFont.truetype(str(p), 109 if "Noto" in p.name else size)
            except OSError:
                continue
    if kind == "emoji":
        return None
    raise OverlayError("no bold font found; put a .ttf at assets/fonts/CaptionBold.ttf")


def _runs(text: str) -> list[tuple[str, bool]]:
    """Split text into (piece, is_emoji) runs."""
    out, pos = [], 0
    for m in _EMOJI.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out


EMOJI_HEIGHT = int(TEXT_SIZE * 0.95)   # drawn emoji height, about the height of a capital letter + a bit
EMOJI_GAP = 4                           # extra space after an emoji
_emoji_cache: dict[tuple[str, int], object] = {}


def emoji_tile(piece: str, emoji_font):
    """The emoji as a tightly cropped RGBA image, EMOJI_HEIGHT tall.

    Drawn on a padded canvas and cropped to its real ink box, so no part of the glyph
    is cut off (color emoji fonts draw above and below the normal text box).
    """
    from PIL import Image, ImageDraw
    key = (piece, id(emoji_font))
    if key in _emoji_cache:
        return _emoji_cache[key]
    size = emoji_font.size
    canvas = Image.new("RGBA", (size * 2 * max(1, len(piece)), size * 2), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((size // 2, size // 2), piece, font=emoji_font, embedded_color=True)
    box = canvas.getbbox()
    if box is None:
        tile = Image.new("RGBA", (1, EMOJI_HEIGHT), (0, 0, 0, 0))
    else:
        tile = canvas.crop(box)
        scale = EMOJI_HEIGHT / tile.height
        tile = tile.resize((max(1, round(tile.width * scale)), EMOJI_HEIGHT), Image.LANCZOS)
    _emoji_cache[key] = tile
    return tile


def _width(draw, text: str, font, emoji_font) -> float:
    total = 0.0
    for piece, is_emoji in _runs(text):
        if is_emoji:
            if emoji_font is not None:
                total += emoji_tile(piece, emoji_font).width + EMOJI_GAP
            continue
        total += draw.textlength(piece, font=font)
    return total


def wrap(draw, text: str, font, emoji_font, max_width: float) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            trial = f"{cur} {word}".strip()
            too_wide = cur and _width(draw, trial, font, emoji_font) > max_width
            if too_wide and _EMOJI.fullmatch(word):
                # never start a line with a lone emoji: carry the previous word down with it
                head, _, last = cur.rpartition(" ")
                if head:
                    lines.append(head)
                    cur = f"{last} {word}"
                else:
                    cur = trial
            elif too_wide:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
    return lines


def caption_box_png(text: str, out: Path, width: int = W - 2 * BOX_MARGIN) -> tuple[int, int]:
    """Draw the white rounded caption box (transparent around it). Returns its size."""
    from PIL import Image, ImageDraw
    font = _load_font("text", TEXT_SIZE)
    emoji_font = _load_font("emoji", TEXT_SIZE)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    lines = wrap(probe, text, font, emoji_font, width - 2 * BOX_PAD_X)
    if not lines:
        raise OverlayError("caption text is empty")
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + LINE_GAP
    box_w = int(min(width, max(_width(probe, l, font, emoji_font) for l in lines) + 2 * BOX_PAD_X))
    box_h = int(len(lines) * line_h - LINE_GAP + 2 * BOX_PAD_Y)
    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, box_w - 1, box_h - 1], radius=28, fill=(255, 255, 255, 255))
    # Vertical middle of the letters (cap top to baseline-ish), to centre emoji on the text.
    _, ink_top, _, ink_bottom = font.getbbox("HXgy")
    _, cap_top, _, base = font.getbbox("HX")
    text_mid = (cap_top + base) / 2
    y = BOX_PAD_Y
    for line in lines:
        x = (box_w - _width(d, line, font, emoji_font)) / 2
        for piece, is_emoji in _runs(line):
            if is_emoji:
                if emoji_font is None:
                    continue
                tile = emoji_tile(piece, emoji_font)
                ey = int(round(y + text_mid - tile.height / 2))
                ey = max(4, min(ey, box_h - tile.height - 4))          # always fully inside the box
                img.alpha_composite(tile, (int(round(x)), ey))
                x += tile.width + EMOJI_GAP
            else:
                d.text((x, y), piece, font=font, fill=(0, 0, 0, 255))
                x += d.textlength(piece, font=font)
        y += line_h
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return box_w, box_h


def parse_facecam(value: str) -> tuple[int, int, int, int]:
    """'x:y:w:h' in source-video pixels (e.g. '12:462:376:238')."""
    try:
        x, y, w, h = (int(v) for v in value.split(":"))
    except ValueError:
        raise OverlayError("facecam must be x:y:w:h in pixels of the original video, e.g. 12:462:376:238") from None
    if w < 20 or h < 20 or x < 0 or y < 0:
        raise OverlayError("facecam box is too small or outside the frame")
    return x, y, w, h


FACE_WIDTH = 620         # facecam width in the Short when stacked
FACE_BORDER = 6
GAP = 24


def _stack_layout(box_h: int, face: tuple[int, int, int, int], game_h: int) -> tuple[int, int, int, int]:
    """Positions for caption box / facecam / gameplay stacked top to bottom.

    Returns (box_y, face_y, face_w, game_y). The facecam shrinks if everything would
    not fit above the credit line.
    """
    box_y = 70
    bottom = int(H * 0.93) - 20
    _, _, fw, fh = face
    face_w = FACE_WIDTH
    while True:
        face_h = int(face_w * fh / fw) + 2 * FACE_BORDER
        face_y = box_y + box_h + GAP
        game_y = face_y + face_h + GAP
        if game_y + game_h <= bottom or face_w <= 300:
            return box_y, face_y, face_w, game_y
        face_w -= 20


def render_caption_clip(clip: Path, text: str, out: Path, workdir: Path, *, fit: str = "zoom",
                        credit: str = "", mute: bool = False, start: float = 0.0,
                        duration: float | None = None,
                        facecam: tuple[int, int, int, int] | None = None) -> dict:
    """Render the caption-box Short. fit: "zoom" (4:3 middle of the frame, bigger) or "full".

    facecam (x, y, w, h in source pixels): cut the streamer's camera out of the frame and
    stack it between the caption box and the gameplay, so it is not lost to the crop.
    """
    if fit not in ("zoom", "full"):
        raise OverlayError("fit must be 'zoom' or 'full'")
    clip = Path(clip)
    if not clip.is_file():
        raise OverlayError(f"clip not found: {clip}")
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    box_w, box_h = caption_box_png(text, workdir / "box.png")
    length = media.probe(clip)["duration"] - start
    if duration:
        length = min(length, duration)
    length = min(length, 59.0)
    if length < 1:
        raise OverlayError("the clip is too short")

    src = media.probe(clip)
    fg = ("crop=ih*4/3:ih," if fit == "zoom" else "") + f"scale={W}:-2"
    game_h = int(W * 3 / 4) if fit == "zoom" else int(W * src["height"] / src["width"])
    face_part = ""
    if facecam:
        x, y, w, h = facecam
        if x + w > src["width"] or y + h > src["height"]:
            raise OverlayError(f"facecam box is outside the {src['width']}x{src['height']} video")
        box_y, face_y, face_w, video_top = _stack_layout(box_h, facecam, game_h)
        if fit == "zoom":
            # Slide the 4:3 gameplay window off the camera so it does not show twice.
            crop_w = int(src["height"] * 4 / 3) // 2 * 2
            cx = (src["width"] - crop_w) // 2
            if x < cx + crop_w and x + w > cx:
                if x + w / 2 < src["width"] / 2:
                    cx = min(src["width"] - crop_w, x + w)
                else:
                    cx = max(0, x - crop_w)
            fg = f"crop={crop_w}:ih:{cx}:0,scale={W}:-2"
        b = FACE_BORDER
        face_part = (f"[c]crop={w}:{h}:{x}:{y},scale={face_w - 2 * b}:-2,"
                     f"pad=iw+{2 * b}:ih+{2 * b}:{b}:{b}:white[face];")
    else:
        video_top = int(H * VIDEO_TOP)
        box_y = max(40, video_top - box_h + 50)      # box overlaps the top of the clip a little
    credit_part = ""
    if credit:
        shutil.copyfile(_load_font("text", 10).path, workdir / "font.ttf")
        (workdir / "credit.txt").write_text(credit, encoding="utf-8")
        credit_part = (",drawtext=fontfile=font.ttf:textfile=credit.txt:fontsize=34:fontcolor=white:"
                       "borderw=3:bordercolor=black@0.8:x=(w-text_w)/2:y=h*0.90")
    graph = (
        f"[0:v]setpts=PTS-STARTPTS,fps={FPS},split={3 if facecam else 2}[a][b]{'[c]' if facecam else ''};"
        f"[a]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
        f"boxblur=luma_radius=12:luma_power=2,scale={W}:{H},eq=brightness=-0.25[bg];"
        f"[b]{fg}[fg];{face_part}"
        f"[bg][fg]overlay=(W-w)/2:{video_top}[v0];"
        + (f"[v0][face]overlay=(W-w)/2:{face_y}[v1];" if facecam else "[v0]null[v1];")
        + f"[v1][1:v]overlay=(W-w)/2:{box_y}{credit_part},format=yuv420p,setsar=1[v]"
    )
    tmp = out.with_name(out.stem + ".part.mp4")
    args = [media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-i", str(clip.resolve()), "-loop", "1", "-i", "box.png",
            "-t", f"{length:.3f}", "-filter_complex", graph, "-map", "[v]"]
    args += ["-an"] if mute else ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    args += ["-r", str(FPS), "-movflags", "+faststart"]
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        media.run(args + list(media.h264_encoder()) + [str(tmp.resolve())], cwd=workdir, timeout=1800)
    except media.MediaError:
        if "h264_nvenc" not in media.h264_encoder():
            raise
        media.run(args + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(tmp.resolve())],
                  cwd=workdir, timeout=1800)
    os.replace(tmp, out)
    return media.probe(out)

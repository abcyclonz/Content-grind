import re

import pytest

from network import media, render
from network.config import Video
from network.render import Scene, build_ass, chunk_words, scene_spans, sources_credit

try:
    media.ffmpeg()
    HAVE_FFMPEG = True
except media.MediaError:
    HAVE_FFMPEG = False


def _caps():
    words, t = [], 0.0
    for line, text in enumerate(["GTA six is coming.", "Pre-orders broke records at Sony and Microsoft stores."]):
        for tok in text.split():
            words.append({"text": tok, "start": round(t, 2), "end": round(t + 0.3, 2), "line": line})
            t += 0.35
    return {"duration": round(t, 2), "words": words}


def test_scene_spans_follow_line_starts():
    caps = _caps()
    spans = scene_spans(caps, 2)
    second_line_start = next(w["start"] for w in caps["words"] if w["line"] == 1)
    assert spans[0] == (0.0, second_line_start)
    assert spans[1] == (second_line_start, caps["duration"] + render.TAIL)


def test_chunks_break_at_lines_sentences_and_length():
    chunks = chunk_words(_caps()["words"])
    texts = [" ".join(w["text"] for w in c) for c in chunks]
    assert texts[0] == "GTA six is" and texts[1] == "coming."          # sentence end, then new line
    assert all(len(c) <= render.CHUNK_WORDS for c in chunks)
    assert all(len({w["line"] for w in c}) == 1 for c in chunks)
    assert sum(len(c) for c in chunks) == len(_caps()["words"])


def test_ass_has_one_event_per_word_with_highlight():
    caps = _caps()
    ass = build_ass(caps, Video(width=1080, height=1920, fps=30, max_seconds=55, caption_style="word_highlight"),
                    "Big {news}", "Sources: a.com", caps["duration"])
    captions = [l for l in ass.splitlines() if l.startswith("Dialogue: 1,")]
    assert len(captions) == len(caps["words"])
    assert all(render.YELLOW in l for l in captions)
    assert "BIG NEWS" in ass and "{news}" not in ass                    # braces stripped from text
    assert "Sources: a.com" in ass and "PlayResY: 1920" in ass
    # highlighted word in the first caption event is the first word
    assert re.search(r"\\c&H00FFFF&\\fscx112\\fscy112}GTA\{\\r\} SIX IS", captions[0])


def test_sources_credit():
    assert sources_credit(["https://www.ign.com/a", "https://kotaku.com/b", "https://www.ign.com/c"]) \
        == "Sources: ign.com, kotaku.com"
    assert sources_credit([]) == ""


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")
def test_video_scenes_from_capture_with_offset_timestamps(tmp_path):
    """Live captures start at non-zero timestamps; every scene must still fill its slot."""
    cap = tmp_path / "live.ts"
    media.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
               "testsrc2=size=640x360:rate=60", "-t", "12", "-c:v", "libx264", "-preset", "ultrafast",
               "-output_ts_offset", "7.3", "-f", "mpegts", str(cap)])
    voice = tmp_path / "v.mp3"
    media.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
               "sine=frequency=300:duration=6", "-c:a", "libmp3lame", str(voice)])
    video = Video(width=360, height=640, fps=30, max_seconds=55, caption_style="word_highlight")
    scenes = [Scene(0.0, 2.0, "video", cap, offset=0), Scene(2.0, 4.0, "video", cap, offset=4),
              Scene(4.0, 6.0, "video", cap, offset=8)]
    caps = {"duration": 6.0, "words": [{"text": "Hi", "start": 0.1, "end": 0.5, "line": 0}]}
    out = tmp_path / "out.mp4"
    render.render_short(scenes, voice, build_ass(caps, video, "", "", 6.0), out, video, tmp_path / "w")
    frames = int(media.run([media.ffprobe(), "-v", "error", "-select_streams", "v", "-count_packets",
                            "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(out)]).stdout.strip())
    assert frames >= 6.0 * 30 - 3      # the picture lasts the whole video, not just the first scene


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not available")
def test_small_render(tmp_path):
    voice = tmp_path / "v.mp3"
    media.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
               "sine=frequency=300:duration=4", "-c:a", "libmp3lame", str(voice)])
    img = tmp_path / "img.png"
    media.run([media.ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
               "testsrc2=size=400x300", "-frames:v", "1", str(img)])
    video = Video(width=360, height=640, fps=30, max_seconds=55, caption_style="word_highlight")
    caps = {"duration": 4.0, "words": [{"text": "Hello", "start": 0.2, "end": 0.8, "line": 0},
                                        {"text": "world.", "start": 2.2, "end": 2.9, "line": 1}]}
    scenes = [Scene(0.0, 2.2, "procedural", palette=1), Scene(2.2, 4.4, "image", img)]
    info = render.render_short(scenes, voice, build_ass(caps, video, "Hook", "Sources: x.com", 4.4),
                               tmp_path / "out.mp4", video, tmp_path / "work")
    assert (info["width"], info["height"]) == (360, 640)
    assert abs(info["duration"] - 4.4) < 0.2 and info["audio_codec"] == "aac"

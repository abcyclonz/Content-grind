import pytest

from network.clips.detector import SpikeDetector, classify, hype_score
from network.config import Detection

CFG = Detection(window_seconds=5, baseline_minutes=1, warmup_minutes=1, spike_ratio=3.0,
                min_messages_per_second=1.0, cooldown_seconds=60, hype_weight=1.5)


@pytest.mark.parametrize(
    "text, emotes, high",
    [
        ("KEKW KEKW KEKW", 3, True),
        ("CLIP IT", 0, True),
        ("NOOOOOO", 0, True),
        ("WHAT DID HE JUST DO???", 0, True),
        ("what did he just do???", 0, True),
        ("what game is this?", 0, False),       # "what" in a normal question is not a reaction
        ("lmaooooo", 0, True),
        ("hahahaha", 0, True),
        ("💀💀💀", 0, True),
        ("no way he actually did that", 0, True),
        ("thanks for the gifted subs!", 0, False),
        ("!enter", 0, False),
        ("hi chat how is everyone", 0, False),
        ("", 1, True),
    ],
)
def test_hype_score(text, emotes, high):
    assert (hype_score(text, emotes) >= 0.5) is high


def _feed(det, start, end, per_second, text="ok", emotes=0):
    """Add messages for each second in [start, end) and run check(); return spikes."""
    spikes = []
    for sec in range(start, end):
        for k in range(per_second):
            det.add(sec + k / max(per_second, 1), text, emotes)
        s = det.check(float(sec) + 0.99)
        if s:
            spikes.append(s)
    return spikes


def test_spike_detected_and_ends():
    det = SpikeDetector(CFG)
    assert _feed(det, 0, 90, 1) == []                    # steady chat: nothing
    assert _feed(det, 90, 100, 8, "LUL LUL", 2) == []   # spike running, not ended yet
    spikes = _feed(det, 100, 130, 1)                     # calms down -> spike reported
    assert len(spikes) == 1
    s = spikes[0]
    assert 84 <= s.start <= 96
    assert s.intensity > 3
    assert s.messages >= 70
    assert "LUL LUL" in s.samples


def test_no_spike_during_warmup():
    det = SpikeDetector(CFG)
    assert _feed(det, 0, 20, 10, "LUL") + _feed(det, 20, 40, 0) == []


def test_cooldown_blocks_back_to_back_spikes():
    det = SpikeDetector(CFG)
    _feed(det, 0, 90, 1)
    first = _feed(det, 90, 100, 8, "LUL") + _feed(det, 100, 115, 1)
    second = _feed(det, 115, 125, 8, "LUL") + _feed(det, 125, 140, 1)
    assert len(first) == 1 and second == []


def test_absolute_floor_ignores_tiny_chats():
    det = SpikeDetector(CFG)
    _feed(det, 0, 90, 0)
    # 0 -> one message every 2 s is "infinitely" more, but below the 1 msg/s floor
    spikes = []
    for sec in range(90, 130, 2):
        det.add(float(sec), "lol")
        spikes += [s for s in [det.check(sec + 0.5), det.check(sec + 1.5)] if s]
    assert spikes == []


def test_spike_capped_at_max_length():
    det = SpikeDetector(CFG)
    _feed(det, 0, 90, 1)
    spikes = _feed(det, 90, 160, 10, "W")
    assert len(spikes) == 1 and spikes[0].end - spikes[0].start <= 46


@pytest.mark.parametrize(
    "text, emotes, category",
    [
        ("KEKW KEKW", (), "funny"),
        ("POGGERS", (), "hype"),
        ("WWWWW", (), "hype"),
        ("NO WAY", (), "shock"),
        ("someone clip that", (), "clip"),
        ("RIP", (), "fail"),
        ("kaiLaugh kaiLaugh", ("kaiLaugh", "kaiLaugh"), "funny"),
        ("raid incoming", (), "noise"),
        ("anyone know the song", (), None),
    ],
)
def test_classify_categories(text, emotes, category):
    assert classify(text, len(emotes), emotes).category == category


def _feed_users(det, start, end, per_second, texts, users=None, emotes=0):
    """Like _feed but with named chatters; texts/users are cycled."""
    spikes, n = [], 0
    for sec in range(start, end):
        for k in range(per_second):
            who = users[n % len(users)] if users else f"u{n % 400}"
            det.add(sec + k / max(per_second, 1), texts[n % len(texts)], emotes, who)
            n += 1
        s = det.check(float(sec) + 0.99)
        if s:
            spikes.append(s)
    return spikes


def _calm(det, start, end):
    return _feed_users(det, start, end, 1, ["hi", "how is everyone", "nice", "what map is this"],
                       users=[f"reg{i}" for i in range(40)])


def test_one_spammer_cannot_make_a_spike():
    det = SpikeDetector(CFG)
    _calm(det, 0, 90)
    spikes = _feed_users(det, 90, 100, 10, ["LUL LUL"], users=["spammer"], emotes=2) + _calm(det, 100, 130)
    assert spikes == []


def test_bots_and_commands_are_ignored():
    det = SpikeDetector(CFG, ignore_users=("thestreamer",))
    _calm(det, 0, 90)
    spikes = (_feed_users(det, 90, 95, 10, ["!enter"]) + _feed_users(det, 95, 100, 10, ["Thanks for following!"],
                                                                       users=["Nightbot", "StreamElements"])
              + _feed_users(det, 100, 105, 10, ["LUL"], users=["thestreamer"]) + _calm(det, 105, 140))
    assert spikes == []


def test_real_reaction_scores_high_and_explains_itself():
    det = SpikeDetector(CFG)
    _calm(det, 0, 90)
    spikes = _feed_users(det, 90, 98, 9, ["KEKW KEKW", "LMAOOOO", "KEKW", "clip it", "ICANT 💀"],
                         emotes=1) + _calm(det, 98, 130)
    assert len(spikes) == 1
    s = spikes[0]
    assert s.accepted and s.score >= 70 and s.category == "funny"
    assert s.clip_requests >= 3 and s.unique_chatters >= 50
    assert s.top_reactions[0][0] == "kekw"
    assert "asked to clip it" in s.explain()
    assert 89 <= s.start <= 91            # onset found precisely, not just "window start"
    assert len(set(s.samples)) == len(s.samples)


def test_busy_conversation_is_detected_but_not_clipped():
    det = SpikeDetector(CFG)
    _calm(det, 0, 90)
    talk = ["what game is he going to play next week do you think",
            "i think he said something about a new map update coming soon",
            "has anyone tried the new patch yet it seems kind of buggy to me",
            "where is he streaming from today is that his new apartment"]
    spikes = _feed_users(det, 90, 98, 6, talk) + _calm(det, 98, 130)
    assert len(spikes) == 1 and not spikes[0].accepted
    # A rejected burst does not block a real reaction shortly after.
    later = _feed_users(det, 160, 168, 9, ["W", "POGGERS", "LETS GOOO", "clip it"]) + _calm(det, 168, 200)
    assert len(later) == 1 and later[0].accepted and later[0].category == "hype"


def test_raid_is_penalised():
    det = SpikeDetector(CFG)
    _calm(det, 0, 90)
    spikes = _feed_users(det, 90, 98, 9, ["raid incoming", "RAID HYPE", "kaiRaid kaiRaid", "raiders are here"])         + _calm(det, 98, 130)
    assert all(not s.accepted for s in spikes)

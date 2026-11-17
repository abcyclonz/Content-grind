from network.align import align, norm, script_words
from network.providers.tts import WordTiming as W


def test_script_words_drop_punctuation_tokens():
    assert script_words(["Hello there — friend.", "Bye!"]) == [
        ("Hello", 0), ("there", 0), ("friend.", 0), ("Bye!", 1)]


def test_exact_match_keeps_whisper_times_and_script_spelling():
    words = ["GTA", "6's", "map,", "huge."]
    heard = [W("GTA", 0.0, 0.3), W("6's", 0.3, 0.6), W("map", 0.6, 0.9), W("huge", 0.9, 1.4)]
    a = align(words, heard, 2.0)
    assert a.matched == 4 and [w.text for w in a.words] == words
    assert (a.words[2].start, a.words[2].end) == (0.6, 0.9)


def test_unheard_words_are_interpolated_between_neighbours():
    words = ["about", "nine", "point", "seven", "million", "copies"]
    heard = [W("about", 0.0, 0.4), W("9.7", 0.5, 1.4), W("million", 1.5, 1.9), W("copies", 2.0, 2.5)]
    a = align(words, heard, 3.0)
    assert a.matched == 3
    mid = a.words[1:4]
    assert mid[0].start >= 0.4 and mid[-1].end <= 1.5
    assert all(w.end > w.start for w in a.words)
    assert all(a.words[i].start >= a.words[i - 1].end - 1e-9 for i in range(1, len(a.words)))


def test_nothing_heard_spreads_evenly():
    a = align(["one", "two", "three"], [], 3.0)
    assert a.matched == 0
    assert a.words[0].start == 0.0 and abs(a.words[-1].end - 3.0) < 1e-6


def test_overlaps_and_overruns_are_clamped():
    words = ["a", "b", "c"]
    heard = [W("a", 0.0, 1.0), W("b", 0.8, 1.2), W("c", 1.1, 9.0)]
    a = align(words, heard, 2.0)
    assert a.words[1].start == 1.0 and a.words[2].end == 2.0


def test_words_past_the_end_are_pulled_back_with_length():
    heard = [W(w, i * 0.5, i * 0.5 + 0.4) for i, w in enumerate("a b c d e".split())]
    a = align("a b c d e".split(), heard, 1.6)          # audio shorter than the timings
    assert all(w.end - w.start >= 0.049 for w in a.words)
    assert a.words[-1].end <= 1.6
    assert all(a.words[i].start >= a.words[i - 1].end - 1e-9 for i in range(1, 5))


def test_norm():
    assert norm("Leonida's,") == "leonidas" and norm("—") == ""

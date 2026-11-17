"""Chat spike detection: find the moments chat genuinely explodes.

Two steps:

1. Trigger (cheap, every second). Chat is counted in one-second bins. Each
   message is weighted by how much it looks like a reaction (see classify),
   each chatter counts at most max_messages_per_chatter times per window (so
   one spammer cannot fake a spike), and bots, commands ("!enter") and the
   broadcaster are ignored. A spike starts when the window rate is above all
   of: min_messages_per_second, spike_ratio x the normal rate, and
   min_zscore robust standard deviations above it, with at least
   min_unique_chatters different people talking. "Normal" is the median of
   this streamer's own recent windows (robust to earlier spikes).

2. Judgement (once, when the spike ends). The reaction is scored 0-100 from:
     intensity    how much faster than normal chat got
     breadth      what share of the active audience joined in
     reaction     what share of messages were reactions (laughing, hype, shock)
     convergence  whether chat converged on the same emote/word (an emote wall)
     clip asks    how many different people typed "clip it"
   Raids, sub gifts and giveaways are penalised. Spikes below min_score are
   returned with accepted=False (logged, not clipped) and do not start the
   cooldown. The dominant reaction (funny/hype/shock/fail) is kept for titles.
"""
from __future__ import annotations

import collections
import math
import re
import statistics
from dataclasses import dataclass, field

from network.config import Detection

MAX_SPIKE_SECONDS = 45
REJECTED_COOLDOWN_SECONDS = 20
BOTS = frozenset(
    "nightbot streamelements streamlabs moobot fossabot wizebot soundalerts sery_bot "
    "botrixoficial kofistreambot commanderroot pokemoncommunitygame streamstickers "
    "own3d blerp buttsbot".split()
)

# Strong words count anywhere; weak ones only in 1-2 word or shouted messages
# ("what" in "what game is this" is not a reaction, "WHAT" is).
_STRONG = {
    "clip": "clip clipit clipped clipthat clipthis",
    "funny": "lul lol lmao lmfao kekw kek kekl lulw omegalul icant pepelaugh xd xdd "
             "dead dying ded rofl",
    "hype": "pog pogchamp poggers pogu letsgo goated insane cracked nuts hype",
    "shock": "wtf omg omfg noway holy monkas monkaw",
    "fail": "rip oof sadge pepehands unlucky",
    "noise": "raid raiders hypetrain gifted giveaway",
}
_WEAK = {
    "hype": "w ww gg ez clean goat fire sheesh",
    "shock": "what wait huh yo bro bruh damn wow jesus no",
    "fail": "l f ff wasted",
}
_PHRASES = [
    (re.compile(r"\b(clip (it|that|this)|someone clip|clip pls|clip please)\b"), "clip"),
    (re.compile(r"\b(no way|holy (shit|moly|crap)|what the|did (that|he|she) just|is this real)\b"), "shock"),
    (re.compile(r"\b(lets go|let s go|lfg|so good|goated)\b"), "hype"),
    (re.compile(r"\b(im (dead|crying|weak)|i m (dead|crying)|i cant breathe)\b"), "funny"),
    (re.compile(r"\b(gifted|gift(ed)? subs?|thanks for the (sub|raid)|raid(ers)?|hype train|!enter)\b"), "noise"),
]
_EMOJI = {"😂": "funny", "🤣": "funny", "💀": "funny", "😭": "funny",
          "🔥": "hype", "🐐": "hype", "💯": "hype",
          "😱": "shock", "😳": "shock", "🤯": "shock", "😮": "shock", "😨": "shock",
          "🪦": "fail", "💔": "fail"}
# Channel emotes have custom names (kaiLaugh, xqcPog): guess from the name.
_EMOTE_HINTS = [("clip", "clip"), ("laugh", "funny"), ("lul", "funny"), ("kek", "funny"),
                ("lol", "funny"), ("lmao", "funny"), ("icant", "funny"), ("pog", "hype"),
                ("hype", "hype"), ("fire", "hype"), ("goat", "hype"), ("wow", "shock"),
                ("omg", "shock"), ("wtf", "shock"), ("monka", "shock"), ("shock", "shock"),
                ("scared", "shock"), ("sad", "fail"), ("cry", "fail"), ("rip", "fail"),
                ("raid", "noise")]
_PRIORITY = ["noise", "clip", "funny", "hype", "shock", "fail"]
REACTIONS = ("funny", "hype", "shock", "fail")

_WORD_SETS = {cat: frozenset(words.split()) for cat, words in _STRONG.items()}
_WEAK_SETS = {cat: frozenset(words.split()) for cat, words in _WEAK.items()}
_WORDS = re.compile(r"[a-z0-9]+")
_RUNS = re.compile(r"(.)\1+")
_REPEAT = re.compile(r"(.)\1{3,}")
_LAUGH = re.compile(r"^(ha|he|ah|ja|xa){2,}h?$|^a?(sk|ks|dj|jd){3,}")


def _collapse(word: str) -> str:
    return _RUNS.sub(r"\1", word)   # lmaooo -> lmao, wwww -> w, pogggg -> pog


@dataclass(frozen=True)
class Reaction:
    score: float          # 0..1, how much the message looks like a reaction
    category: str | None  # funny / hype / shock / fail / clip / noise / emote / None
    key: str              # what it "says", for spotting chat converging (emote walls)


def classify(text: str, emote_count: int = 0, emotes: tuple[str, ...] = ()) -> Reaction:
    t = text.strip()
    low = t.lower()
    words = _WORDS.findall(low)
    collapsed = [_collapse(w) for w in words]
    letters = [c for c in t if c.isalpha()]
    shouting = len(letters) >= 3 and sum(c.isupper() for c in letters) / len(letters) > 0.7
    short = len(words) <= 4
    emote_heavy = emote_count >= 2 or (emote_count > 0 and len(words) <= emote_count)

    found: set[str] = set()
    for raw, w in zip(words, collapsed):
        for cat, ws in _WORD_SETS.items():
            if raw in ws or w in ws:
                found.add(cat)
        if len(words) <= 2 or shouting:
            for cat, ws in _WEAK_SETS.items():
                if raw in ws or w in ws:
                    found.add(cat)
        if _LAUGH.match(raw):
            found.add("funny")
    spaced = " ".join(collapsed)
    for pattern, cat in _PHRASES:
        if pattern.search(spaced) or pattern.search(low):
            found.add(cat)
    for ch, cat in _EMOJI.items():
        if ch in t:
            found.add(cat)
    for name in emotes:
        n = name.lower()
        for hint, cat in _EMOTE_HINTS:
            if hint in n:
                found.add(cat)
                break

    category = next((c for c in _PRIORITY if c in found), None)
    if category is None and (emote_heavy or (not words and emote_count)):
        category = "emote"
    key = (emotes[0].lower() if emotes
           else next((w for w in collapsed if any(w in s for s in _WORD_SETS.values())), None)
           or " ".join(collapsed)[:40])

    if category == "noise":
        return Reaction(0.0, "noise", key)
    if category == "clip":
        return Reaction(1.0, "clip", key)
    if not t:
        return Reaction(1.0 if emote_count else 0.0, category, key)
    score = 0.5 if category else 0.0
    if emote_heavy:
        score += 0.4
    if shouting:
        score += 0.2
    if _REPEAT.search(t) or t.count("!") >= 3 or t.count("?") >= 3:
        score += 0.2
    if short and score > 0:
        score += 0.1
    if len(words) > 12 and not emote_heavy:
        score *= 0.5   # people typing paragraphs are talking, not reacting
    return Reaction(min(score, 1.0), category, key)


def hype_score(text: str, emote_count: int = 0) -> float:
    """0..1: how much a single message looks like a reaction to something."""
    return classify(text, emote_count).score


@dataclass
class Spike:
    start: float          # when chat started reacting (the moment is just before this)
    peak: float
    end: float
    peak_rate: float
    baseline: float
    messages: int
    samples: list[str] = field(default_factory=list)
    score: float = 100.0
    accepted: bool = True
    category: str = "mixed"                 # dominant reaction: funny / hype / shock / fail / mixed
    unique_chatters: int = 0
    breadth: float = 0.0                    # share of the recently active audience that joined in
    reaction_share: float = 0.0
    convergence: float = 0.0
    clip_requests: int = 0
    top_reactions: list[tuple[str, int]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def intensity(self) -> float:
        return self.peak_rate / max(self.baseline, 0.1)

    def explain(self) -> str:
        verdict = "clip" if self.accepted else "skip"
        return f"score {self.score:.0f} ({verdict}, {self.category}): " + "; ".join(self.reasons)


@dataclass
class _Msg:
    ts: float
    chatter: str
    text: str
    reaction: Reaction


class SpikeDetector:
    def __init__(self, cfg: Detection, ignore_users: tuple[str, ...] = ()):
        self.cfg = cfg
        self.ignore = BOTS | {u.lower() for u in cfg.ignore_users} | {u.lower() for u in ignore_users}
        # second -> chatter -> [messages, summed weight]
        self.bins: collections.OrderedDict[int, dict[str, list[float]]] = collections.OrderedDict()
        self.first_ts: float | None = None
        self.next_allowed = float("-inf")
        self._active: dict | None = None
        self._msgs: collections.deque[_Msg] = collections.deque(maxlen=20_000)
        self._window_cache: dict[int, tuple[float, int]] = {}
        self._anon = 0

    # ------------------------------------------------------------------
    def add(self, ts: float, text: str, emote_count: int = 0, chatter: str = "",
            emotes: tuple[str, ...] = ()) -> None:
        if self.first_ts is None:
            self.first_ts = ts
        who = chatter.lower()
        if who in self.ignore or (self.cfg.ignore_commands and text.lstrip().startswith("!")):
            return
        if not who:   # chat logs without names: treat every message as its own chatter
            self._anon += 1
            who = f"~{self._anon}"
        r = classify(text, emote_count, emotes)
        weight = 1.0 + (self.cfg.hype_weight - 1.0) * r.score
        if r.category == "noise":
            weight = 0.3
        cell = self.bins.setdefault(int(ts), {}).setdefault(who, [0, 0.0])
        cell[0] += 1
        cell[1] += weight
        self._msgs.append(_Msg(ts, who, text, r))

    def _window(self, start: int, end: int) -> tuple[float, int]:
        """(capped weighted messages per second, unique chatters) for seconds [start, end)."""
        agg: dict[str, list[float]] = {}
        for s in range(start, end):
            for who, (n, w) in self.bins.get(s, {}).items():
                a = agg.setdefault(who, [0, 0.0])
                a[0] += n
                a[1] += w
        cap = self.cfg.max_messages_per_chatter
        total = sum(w * min(1.0, cap / n) for n, w in agg.values())
        return total / max(1, end - start), len(agg)

    def _baseline_windows(self, now: float) -> list[tuple[float, int]]:
        w = self.cfg.window_seconds
        lo = int(now) - self.cfg.baseline_minutes * 60
        hi = int(now) - w
        out = []
        for s in range(lo, hi - w + 1, w):
            if s not in self._window_cache:
                self._window_cache[s] = self._window(s, s + w)
            out.append(self._window_cache[s])
        for s in [k for k in self._window_cache if k < lo]:
            del self._window_cache[s]
        return out

    def baseline(self, now: float) -> float:
        """The normal chat rate for this streamer (median of recent windows)."""
        return self._stats(now)[0]

    def _stats(self, now: float) -> tuple[float, float, float]:
        """(median rate, robust sigma, median unique chatters) over the baseline period."""
        windows = self._baseline_windows(now)
        if not windows:
            return 0.0, 0.0, 0.0
        rates = [r for r, _ in windows]
        med = statistics.median(rates)
        sigma = 1.4826 * statistics.median(abs(r - med) for r in rates)
        return med, sigma, statistics.median(u for _, u in windows)

    def _audience(self, now: float) -> int:
        lo = int(now) - self.cfg.baseline_minutes * 60
        who: set[str] = set()
        for s, cells in self.bins.items():
            if s >= lo:
                who.update(cells)
        return len(who)

    def _bin_rate(self, s: int) -> float:
        return sum(w for _, w in self.bins.get(s, {}).values())

    # ------------------------------------------------------------------
    def check(self, now: float) -> Spike | None:
        """Call about once a second. Returns a Spike (accepted or not) when one has finished."""
        horizon = int(now) - self.cfg.baseline_minutes * 60 - self.cfg.window_seconds
        while self.bins and next(iter(self.bins)) < horizon:
            self.bins.popitem(last=False)

        w = self.cfg.window_seconds
        rate, unique = self._window(int(now) - w, int(now) + 1)

        if self._active is None:
            warmed = self.first_ts is not None and now - self.first_ts >= self.cfg.warmup_minutes * 60
            if not warmed or now < self.next_allowed:
                return None
            med, sigma, med_unique = self._stats(now)
            threshold = max(self.cfg.min_messages_per_second, self.cfg.spike_ratio * med,
                            med + self.cfg.min_zscore * sigma)
            if rate >= threshold and unique >= self.cfg.min_unique_chatters:
                self._active = {"start": self._onset(now, med), "peak": now, "peak_rate": rate,
                                "baseline": med, "audience": self._audience(now)}
            return None

        a = self._active
        if rate > a["peak_rate"]:
            a["peak_rate"], a["peak"] = rate, now
        calm = rate < max(a["baseline"] * 1.5, self.cfg.min_messages_per_second * 0.5)
        if calm or now - a["start"] >= MAX_SPIKE_SECONDS:
            self._active = None
            spike = self._judge(a, now)
            self.next_allowed = now + (self.cfg.cooldown_seconds if spike.accepted
                                       else min(self.cfg.cooldown_seconds, REJECTED_COOLDOWN_SECONDS))
            return spike
        return None

    def _onset(self, now: float, med: float) -> float:
        """The first second of the current surge (chat reacts a few seconds after the moment)."""
        w = self.cfg.window_seconds
        level = max(med * 1.5, self.cfg.min_messages_per_second * 0.5)
        onset = int(now)
        for s in range(int(now), int(now) - 2 * w, -1):
            if self._bin_rate(s) >= level or self._bin_rate(s - 1) >= level:
                onset = s
            else:
                break
        return float(max(onset, int(now) - 2 * w))

    def _judge(self, a: dict, now: float) -> Spike:
        msgs = [m for m in self._msgs if a["start"] <= m.ts <= now]
        chatters = {m.chatter for m in msgs}
        n = max(1, len(msgs))
        cats = collections.Counter(m.reaction.category for m in msgs if m.reaction.category)
        reacting = sum(cats[c] for c in (*REACTIONS, "emote", "clip"))
        reaction_share = reacting / n
        noise_share = cats["noise"] / n
        clip_requests = len({m.chatter for m in msgs if m.reaction.category == "clip"})
        # Count each chatter once per key so one spammer cannot fake an emote wall.
        # Only reactions count: ten people asking the same question is not an emote wall.
        keys = collections.Counter(k for k in {(m.chatter, m.reaction.key) for m in msgs
                                               if m.reaction.key and m.reaction.category not in (None, "noise")}
                                   )
        by_key = collections.Counter(k for _, k in keys)
        top = by_key.most_common(3)
        convergence = sum(c for _, c in top) / max(1, len(keys))
        breadth = len(chatters) / max(1, a["audience"])
        intensity = a["peak_rate"] / max(a["baseline"], 0.1)

        parts = {
            "intensity": min(1.0, math.log(max(intensity, 1.0)) / math.log(8)) * 25,
            "breadth": min(1.0, breadth / 0.4) * 15,
            "reaction": min(1.0, reaction_share / 0.6) * 30,
            "convergence": min(1.0, convergence / 0.5) * 15,
            "clip": min(1.0, clip_requests / 3) * 15,
        }
        score = sum(parts.values())
        reasons = [f"chat {intensity:.1f}x normal",
                   f"{len(chatters)} people ({breadth:.0%} of active chat)",
                   f"{reaction_share:.0%} reactions"]
        if top:
            reasons.append("top: " + ", ".join(f"{k} x{c}" for k, c in top))
        if clip_requests:
            reasons.append(f"{clip_requests} asked to clip it")
        if reaction_share < 0.2:
            score *= 0.6
            reasons.append("mostly conversation, not reactions")
        if noise_share > 0.25:
            score *= 0.3
            reasons.append(f"{noise_share:.0%} raid/sub/giveaway messages")

        reaction_counts = {c: cats[c] for c in REACTIONS}
        best = max(reaction_counts, key=reaction_counts.get)
        category = best if reaction_counts[best] >= 0.4 * max(1, sum(reaction_counts.values())) \
            and reaction_counts[best] > 0 else "mixed"

        # Samples: the most reaction-like distinct messages around the peak.
        seen, samples = set(), []
        for m in sorted((m for m in msgs if m.ts <= a["peak"] + 3), key=lambda m: -m.reaction.score):
            norm = " ".join(_collapse(x) for x in _WORDS.findall(m.text.lower())) or m.text
            if norm not in seen:
                seen.add(norm)
                samples.append(m.text)
            if len(samples) == 15:
                break

        return Spike(start=a["start"], peak=a["peak"], end=now, peak_rate=a["peak_rate"],
                     baseline=a["baseline"], messages=len(msgs), samples=samples,
                     score=round(score, 1), accepted=score >= self.cfg.min_score, category=category,
                     unique_chatters=len(chatters), breadth=round(breadth, 3),
                     reaction_share=round(reaction_share, 3), convergence=round(convergence, 3),
                     clip_requests=clip_requests, top_reactions=top, reasons=reasons)

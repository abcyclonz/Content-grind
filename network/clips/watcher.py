"""Live watcher: one process, all permitted streamers of one clip channel.

Every POLL_LIVE_SECONDS it asks Helix which streamers are live. For each live
streamer it keeps a rolling recording and listens to chat; when the chat spikes
it cuts a clip and records a job. Only streamers listed (with permission) in
the channel YAML are ever watched.
"""
from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, field

from network.clips.detector import Spike, SpikeDetector
from network.clips.pipeline import clip_window, produce_clip
from network.clips.recorder import Recorder
from network.config import ClipChannelConfig, Settings, Streamer
from network.media import MediaError
from network.twitch.api import Helix, HelixError
from network.twitch.eventsub import ChatListener, ChatMessage

POLL_LIVE_SECONDS = 60
RENDER_MARGIN_SECONDS = 6  # wait for the last segment of the window to land
log = logging.getLogger("clips.watcher")


@dataclass
class LiveStream:
    streamer: Streamer
    user_id: str
    info: dict
    recorder: Recorder
    detector: SpikeDetector
    pending: list[Spike] = field(default_factory=list)
    restarts: int = 0


class ClipWatcher:
    def __init__(self, conn: sqlite3.Connection, channel: ClipChannelConfig, settings: Settings,
                 helix: Helix, chat_factory=ChatListener, recorder_factory=Recorder,
                 producer=produce_clip, clock=time.time):
        self.conn, self.channel, self.settings, self.helix = conn, channel, settings, helix
        self.chat_factory, self.recorder_factory = chat_factory, recorder_factory
        self.producer, self.clock = producer, clock
        self.streamers = {s.login: s for s in channel.enabled_streamers if s.platform == "twitch"}
        self.user_ids: dict[str, str] = {}
        self.live: dict[str, LiveStream] = {}
        self.messages: queue.Queue[ChatMessage] = queue.Queue()
        self.chat: ChatListener | None = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        users = self.helix.users(list(self.streamers))
        for login in self.streamers:
            if login in users:
                self.user_ids[login] = users[login]["id"]
            else:
                log.error("Twitch user %r not found; check the login in the channel YAML", login)
        self.chat = self.chat_factory(self.helix, self.messages.put)
        self.chat.start()

    def poll_live(self) -> None:
        try:
            streams = self.helix.live_streams([l for l in self.streamers if l in self.user_ids])
        except HelixError as e:
            log.warning("could not check who is live: %s", e)
            return
        for login, info in streams.items():
            if login not in self.live:
                self._go_live(login, info)
            else:
                self.live[login].info = info
        for login in [l for l in self.live if l not in streams]:
            self._go_offline(login)

    def _go_live(self, login: str, info: dict) -> None:
        s = self.streamers[login]
        rec = self.recorder_factory(
            f"https://www.twitch.tv/{login}",
            self.settings.data_dir / "buffers" / login,
            self.channel.clip.quality,
            self.channel.clip.buffer_minutes * 60,
        )
        rec.start()
        self.live[login] = LiveStream(s, self.user_ids[login], info, rec, SpikeDetector(self.channel.detection, ignore_users=(login,)))
        self.chat.want(self.user_ids[login], login)
        log.info("%s is live: %s [%s]", login, info.get("title", ""), info.get("game_name", ""))

    def _go_offline(self, login: str) -> None:
        st = self.live.pop(login)
        self._render_pending(st, force=True)
        self.chat.drop(st.user_id)
        st.recorder.stop()
        log.info("%s went offline", login)

    # ------------------------------------------------------------------
    def _render_pending(self, st: LiveStream, now: float | None = None, force: bool = False) -> None:
        now = now if now is not None else self.clock()
        st.recorder.tick(now)
        keep = []
        for spike in st.pending:
            _, t1 = clip_window(self.channel, spike)
            if not force and now < t1 + RENDER_MARGIN_SECONDS:
                keep.append(spike)
                continue
            try:
                self.producer(self.conn, self.settings, self.channel, st.streamer, spike,
                              st.recorder.segments(), st.info)
            except MediaError as e:
                log.error("clip from %s failed: %s", st.streamer.login, e)
        st.pending = keep

    def step(self) -> None:
        now = self.clock()
        while True:
            try:
                m = self.messages.get_nowait()
            except queue.Empty:
                break
            st = self.live.get(m.broadcaster_login)
            if st:
                st.detector.add(m.received_at, m.text, m.emote_count, m.chatter_login, m.emotes)
        for st in list(self.live.values()):
            if not st.recorder.alive():
                st.restarts += 1
                log.warning("recorder for %s stopped; restarting (#%d)", st.streamer.login, st.restarts)
                st.recorder.stop()
                st.recorder.start()
            st.recorder.tick(now)
            spike = st.detector.check(now)
            if spike:
                log.info("chat spike on %s: %s", st.streamer.login, spike.explain())
                if spike.accepted:
                    st.pending.append(spike)
            self._render_pending(st, now)

    def run(self, stop: threading.Event) -> None:
        self.start()
        next_poll = 0.0
        try:
            while not stop.is_set():
                if self.clock() >= next_poll:
                    self.poll_live()
                    next_poll = self.clock() + POLL_LIVE_SECONDS
                self.step()
                stop.wait(1.0)
        finally:
            for login in list(self.live):
                self._go_offline(login)
            if self.chat:
                self.chat.stop()

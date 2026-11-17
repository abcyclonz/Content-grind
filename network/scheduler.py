"""The long-running process: `python -m network.cli run`.

Jobs (APScheduler, one at a time per job, missed runs coalesced):
- signals: every 2 hours (Shorts channels)
- ideas: every morning at IDEAS_TIME in each channel's timezone
- pipeline: every few minutes, every stage after ideas, in order, for every
  channel (each stage only touches jobs in its own state, so this is safe to
  repeat), then the Telegram gate messages
- telegram: long-polls Telegram for button presses and script edits

Every stage gets its own SQLite connection, and errors in one stage or channel
are logged without stopping the others. DRY_RUN is respected by every stage.
Clip channels run their live watcher separately: `python -m network.cli clips watch`.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from network import db
from network.config import AnyChannel, ChannelConfig, Settings, load_channels

SIGNALS_EVERY_HOURS = 2
PIPELINE_EVERY_MINUTES = 3
IDEAS_TIME = "07:00"
log = logging.getLogger("scheduler")

PIPELINE = ("script", "voice", "captions", "visuals", "assemble", "package", "publish")


def _stage_funcs():
    from network.stages import (assemble, captions, ideas, package, publish, script, signals,
                                visuals, voice)
    return {"signals": signals.run, "ideas": ideas.run, "script": script.run, "voice": voice.run,
            "captions": captions.run, "visuals": visuals.run, "assemble": assemble.run,
            "package": package.run, "publish": publish.run}


def applies(stage: str, channel: AnyChannel) -> bool:
    if stage in ("package", "publish"):
        return True
    return isinstance(channel, ChannelConfig)


class Runner:
    def __init__(self, settings: Settings, channels: dict[str, AnyChannel] | None = None,
                 stage_funcs: dict | None = None, bot=None):
        self.settings = settings
        self.channels = channels if channels is not None else load_channels(settings.channels_dir)
        self.funcs = stage_funcs or _stage_funcs()
        self.bot = bot
        self._lock = threading.Lock()   # pipeline and ideas never overlap

    def _connect(self):
        return db.connect(self.settings.db_path)

    def run_stage(self, stage: str, channels: list[AnyChannel] | None = None) -> None:
        conn = self._connect()
        try:
            for ch in channels or list(self.channels.values()):
                if not applies(stage, ch):
                    continue
                try:
                    report = self.funcs[stage](conn, ch, self.settings)
                    text = report.summary() if hasattr(report, "summary") else str(report)
                    if "nothing to do" not in text:
                        log.info(text)
                except Exception as e:  # noqa: BLE001
                    log.error("[%s] %s: %s: %s", stage, ch.id, type(e).__name__, e)
        finally:
            conn.close()

    def signals(self) -> None:
        self.run_stage("signals")

    def ideas(self, channel_id: str | None = None) -> None:
        with self._lock:
            chans = [self.channels[channel_id]] if channel_id else None
            self.run_stage("ideas", chans)
            self.gates()

    def pipeline(self) -> None:
        if not self._lock.acquire(blocking=False):
            log.info("pipeline still running from the last tick; skipping")
            return
        try:
            self.gates()
            for stage in PIPELINE:
                self.run_stage(stage)
            self.gates()
        finally:
            self._lock.release()

    def gates(self) -> None:
        from network.stages import approve
        conn = self._connect()
        try:
            for ch in self.channels.values():
                try:
                    moved = approve.auto_advance(conn, ch)
                    if moved:
                        log.info("[gates] %s: auto-approved %s", ch.id, moved)
                    if self.bot:
                        n = approve.notify(conn, self.settings, ch, self.bot)
                        if n:
                            log.info("[gates] %s: sent %d approval message(s)", ch.id, n)
                except Exception as e:  # noqa: BLE001
                    log.error("[gates] %s: %s: %s", ch.id, type(e).__name__, e)
        finally:
            conn.close()

    def telegram_loop(self, stop: threading.Event) -> None:
        from network.stages import approve
        conn = self._connect()
        try:
            while not stop.is_set():
                try:
                    for what in approve.poll_once(conn, self.settings, self.channels, self.bot, timeout=25):
                        log.info("[telegram] %s", what)
                except Exception as e:  # noqa: BLE001 - network blips: wait and retry
                    log.warning("[telegram] %s", e)
                    stop.wait(10)
        finally:
            conn.close()


def setup_logging(settings: Settings) -> None:
    logs = settings.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), RotatingFileHandler(logs / "scheduler.log", maxBytes=5_000_000,
                                                           backupCount=5, encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def run_forever(settings: Settings, run_now: bool = True) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    from network.stages.approve import make_bot

    setup_logging(settings)
    runner = Runner(settings, bot=make_bot(settings))
    shorts = [c for c in runner.channels.values() if isinstance(c, ChannelConfig)]
    log.info("starting: %d channel(s) %s, DRY_RUN=%s, Telegram %s", len(runner.channels),
             sorted(runner.channels), "on" if settings.dry_run else "OFF",
             "connected" if runner.bot else "not configured (approve with the CLI)")

    sched = BlockingScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600})
    first = datetime.now(timezone.utc) if run_now else None
    sched.add_job(runner.signals, IntervalTrigger(hours=SIGNALS_EVERY_HOURS), id="signals", next_run_time=first)
    hh, mm = map(int, IDEAS_TIME.split(":"))
    for ch in shorts:
        sched.add_job(runner.ideas, CronTrigger(hour=hh, minute=mm, timezone=ch.timezone),
                      args=[ch.id], id=f"ideas-{ch.id}")
        if run_now:  # started mid-day: fill today's slots once, after the first signals run
            from datetime import timedelta
            sched.add_job(runner.ideas, "date", run_date=first + timedelta(seconds=90),
                          args=[ch.id], id=f"ideas-now-{ch.id}")
    sched.add_job(runner.pipeline, IntervalTrigger(minutes=PIPELINE_EVERY_MINUTES), id="pipeline",
                  next_run_time=first)

    stop = threading.Event()
    if runner.bot:
        threading.Thread(target=runner.telegram_loop, args=(stop,), daemon=True, name="telegram").start()
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("stopping")
    finally:
        stop.set()

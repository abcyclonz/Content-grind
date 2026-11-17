"""Command-line entry point: python -m network.cli <command>."""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

from network import db
from network.config import ConfigError, channel_files, load_channel, load_channels, load_settings

STATUS_COLUMNS = (
    ("id", "id"),
    ("channel", "channel_id"),
    ("state", "state"),
    ("idea", "idea_title"),
    ("attempts", "attempts"),
    ("updated (UTC)", "updated_at"),
    ("publish at", "scheduled_publish_at"),
    ("video id", "youtube_video_id"),
    ("error", "error"),
)
CELL_WIDTH = 50
# stage -> channel formats it applies to (grows with each milestone)
STAGE_FORMATS = {
    "signals": {"short"},
    "ideas": {"short"},
    "script": {"short"},
    "voice": {"short"},
    "captions": {"short"},
    "visuals": {"short"},
    "assemble": {"short"},
    "package": {"short", "clip"},
    "publish": {"short", "clip"},
}
STAGE_NAMES = tuple(STAGE_FORMATS)


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, row)]
    line = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()
    out = [line(headers), line(["-" * w for w in widths])]
    out += [line(r) for r in rows]
    return "\n".join(out)


def _cell(row, key: str) -> str:
    value = row[key]
    if value is None:
        return ""
    text = " ".join(str(value).split())  # one line per job
    if key in ("error", "idea_title") and len(text) > CELL_WIDTH:
        text = text[: CELL_WIDTH - 3] + "..."
    if key == "state" and row["failed_at_state"]:
        text += f" (at {row['failed_at_state']})"
    return text


def _open_db():
    settings = load_settings()
    return settings, db.connect(settings.db_path)


def cmd_status(args) -> int:
    settings, conn = _open_db()
    try:
        jobs = db.list_jobs(conn, channel_id=args.channel, state=args.state)
    finally:
        conn.close()
    print(f"database: {settings.db_path}   DRY_RUN: {'on' if settings.dry_run else 'OFF'}")
    print()
    headers = [h for h, _ in STATUS_COLUMNS]
    rows = [[_cell(j, key) for _, key in STATUS_COLUMNS] for j in jobs]
    print(format_table(headers, rows))
    print(f"({len(jobs)} job{'s' if len(jobs) != 1 else ''})")
    return 0


def cmd_check_config(args) -> int:
    paths = args.paths or channel_files(load_settings().channels_dir)
    if not paths:
        print("no channel files found", file=sys.stderr)
        return 1
    failed = 0
    for path in paths:
        try:
            cfg = load_channel(path)
        except ConfigError as e:
            failed += 1
            print(f"FAIL {e}", file=sys.stderr)
        else:
            print(f"OK   {path} (channel id: {cfg.id})")
    return 1 if failed else 0


def _stage_runners():
    # Imported lazily so `status` and `check-config` stay fast.
    from network.stages import assemble, captions, ideas, package, publish, script, signals, visuals, voice
    return {"signals": signals.run, "ideas": ideas.run, "script": script.run,
            "voice": voice.run, "captions": captions.run,
            "visuals": visuals.run, "assemble": assemble.run,
            "package": package.run, "publish": publish.run}


def cmd_run_stage(args) -> int:
    from network.costs import BudgetExceeded
    from network.providers.llm import LLMError

    from network.providers.youtube import YouTubeError

    settings = load_settings()
    channels = {k: c for k, c in load_channels(settings.channels_dir).items()
                if c.format in STAGE_FORMATS[args.stage]}
    if args.channel:
        if args.channel not in channels:
            print(f"unknown channel {args.channel!r}; have {sorted(channels)}", file=sys.stderr)
            return 1
        channels = {args.channel: channels[args.channel]}
    run = _stage_runners()[args.stage]
    print(f"DRY_RUN: {'on (no paid calls)' if settings.dry_run else 'OFF (live calls)'}")
    rc = 0
    conn = db.connect(settings.db_path)
    try:
        for channel in channels.values():
            try:
                extra = {"schedule": False} if args.stage == "publish" and args.no_schedule else {}
                report = run(conn, channel, settings, **extra)
            except (BudgetExceeded, LLMError, YouTubeError) as e:
                print(f"[{args.stage}] {channel.id}: stopped: {e}", file=sys.stderr)
                rc = 1
                continue
            print(report.summary())
            if hasattr(report, "ok") and not report.ok:
                rc = 1
    finally:
        conn.close()
    return rc


def cmd_approve(args) -> int:
    from network import gates
    _, conn = _open_db()
    try:
        before, after = gates.approve(conn, args.job_id)
    except gates.GateError as e:
        print(e, file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"job {args.job_id}: {before} -> {after}")
    return 0


def cmd_reject(args) -> int:
    from network import gates
    _, conn = _open_db()
    try:
        before = gates.reject(conn, args.job_id, args.reason)
    except gates.GateError as e:
        print(e, file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"job {args.job_id}: {before} -> rejected")
    return 0


def cmd_retry(args) -> int:
    _, conn = _open_db()
    try:
        job = db.get_job(conn, args.job_id)
        if job is None or job["state"] != db.FAILED:
            print(f"job {args.job_id} is not failed", file=sys.stderr)
            return 1
        db.retry(conn, args.job_id)
        print(f"job {args.job_id}: failed -> {job['failed_at_state']} (will be picked up on the next run)")
        return 0
    finally:
        conn.close()


def cmd_show(args) -> int:
    settings, conn = _open_db()
    try:
        job = db.get_job(conn, args.job_id)
        if job is None:
            print(f"no job {args.job_id}", file=sys.stderr)
            return 1
        state = job["state"] + (f" (at {job['failed_at_state']})" if job["failed_at_state"] else "")
        print(f"job {job['id']}  channel {job['channel_id']}  state {state}  attempts {job['attempts']}")
        if job["error"]:
            print(f"error: {job['error']}")
        idea = conn.execute("SELECT * FROM ideas WHERE id = ?", (job["idea_id"],)).fetchone()
        if idea:
            print(f"\nIDEA  (score {idea['score']:.0f})\n  {idea['title']}")
            print(textwrap.indent(textwrap.fill(idea["angle"], 90), "  "))
            ids = json.loads(idea["signal_ids"])
            if ids:
                rows = conn.execute(
                    f"SELECT id, source, title, url FROM signals WHERE id IN ({','.join('?' * len(ids))})", ids
                ).fetchall()
                print("\nSOURCES")
                for r in rows:
                    print(f"  [{r['source']}] {r['title']}\n    {r['url']}")
        clip = db.get_artifact(conn, job["id"], "video")
        if clip and not idea:
            m = json.loads(clip["meta"])
            print(f"\nCLIP  {settings.data_dir / Path(clip['path'])}")
            print(f"  streamer: {m.get('streamer_name')} ({m.get('source')}), credit: {m.get('credit')}")
            print(f"  stream: {m.get('stream_title')} [{m.get('game')}]")
            print(f"  window: {m['window'][0]} to {m['window'][1]} UTC, "
                  f"{m.get('probe', {}).get('duration', 0):.1f}s")
            print(f"  chat spike: x{m['spike']['intensity']} ({m['spike']['messages']} messages)")
            print(f"  permission: {m.get('permission_evidence')}")
            if m.get("chat_samples"):
                print("  chat: " + " | ".join(m["chat_samples"][-8:])[:300])
        md = db.get_artifact(conn, job["id"], "metadata")
        if md:
            doc = json.loads((settings.data_dir / Path(md["path"])).read_text(encoding="utf-8"))
            print(f"\nYOUTUBE METADATA\n  title: {doc['title']}\n  tags: {', '.join(doc.get('tags', []))}")
            print(textwrap.indent(doc["description"], "  | "))
        if job["youtube_video_id"]:
            print(f"\nUPLOADED: https://youtu.be/{job['youtube_video_id']}"
                  + (f" (publishes {job['scheduled_publish_at']} UTC)" if job["scheduled_publish_at"] else ""))
        art = db.get_artifact(conn, job["id"], "script")
        if art:
            path = settings.data_dir / Path(art["path"])
            doc = json.loads(path.read_text(encoding="utf-8"))
            print(f"\nSCRIPT  ({doc['word_count']} words, {path})")
            for i, line in enumerate(doc["lines"], 1):
                print(textwrap.indent(textwrap.fill(f"{i}. {line['text']}", 90), "  "))
                print(textwrap.indent(textwrap.fill(f"[image: {line['visual_prompt']}]", 90), "     "))
            print("  cited:")
            for u in doc["sources"]:
                print(f"    {u}")
        return 0
    finally:
        conn.close()


def _setup_logging() -> None:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")


def _clip_channel(settings, channel_id: str | None):
    from network.config import ClipChannelConfig
    clip_channels = {k: c for k, c in load_channels(settings.channels_dir).items()
                     if isinstance(c, ClipChannelConfig)}
    if not clip_channels:
        raise ConfigError("no channel with format: clip in channels/")
    if channel_id is None:
        if len(clip_channels) > 1:
            raise ConfigError(f"several clip channels; pass --channel (one of {sorted(clip_channels)})")
        return next(iter(clip_channels.values()))
    if channel_id not in clip_channels:
        raise ConfigError(f"{channel_id!r} is not a clip channel; have {sorted(clip_channels)}")
    return clip_channels[channel_id]


def _helix(settings):
    from network.twitch.api import Helix
    from network.twitch.auth import TokenManager
    client_id = settings.secret("TWITCH_CLIENT_ID")
    return Helix(client_id, TokenManager(client_id))


def cmd_twitch_login(args) -> int:
    from network.twitch.auth import TOKEN_FILE, TwitchAuthError, device_login
    settings = load_settings()
    try:
        token = device_login(settings.secret("TWITCH_CLIENT_ID"))
    except TwitchAuthError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"Logged in as {token.login}. Token saved to {TOKEN_FILE}")
    return 0


def cmd_clips_check(args) -> int:
    from network.twitch.api import HelixError
    from network.twitch.auth import TwitchAuthError
    settings = load_settings()
    channel = _clip_channel(settings, args.channel)
    try:
        helix = _helix(settings)
        logins = [s.login for s in channel.enabled_streamers]
        users = helix.users(logins)
        live = helix.live_streams([l for l in logins if l in users])
    except (TwitchAuthError, HelixError) as e:
        print(e, file=sys.stderr)
        return 1
    print(f"channel {channel.id}: reading chat as {helix.tokens.token.login}")
    for s in channel.enabled_streamers:
        if s.login not in users:
            state = "NOT FOUND on Twitch"
        elif s.login in live:
            st = live[s.login]
            state = f"LIVE  {st.get('viewer_count')} viewers  {st.get('game_name')}: {st.get('title')}"
        else:
            state = "offline"
        print(f"  {s.login:25} {state}")
    return 0


def _channel_by_id(settings, channel_id: str):
    channels = load_channels(settings.channels_dir)
    if channel_id not in channels:
        raise ConfigError(f"unknown channel {channel_id!r}; have {sorted(channels)}")
    return channels[channel_id]


def cmd_youtube_login(args) -> int:
    from network.providers import youtube
    settings = load_settings()
    channel = _channel_by_id(settings, args.channel)
    secret_file = settings.secret("YOUTUBE_CLIENT_SECRET_FILE")
    if not Path(secret_file).is_file():
        print(f"YOUTUBE_CLIENT_SECRET_FILE does not exist: {secret_file}", file=sys.stderr)
        return 1
    print(f"Sign in with the Google account that owns the YouTube channel for '{channel.id}'.")
    path = youtube.login(secret_file, channel.id)
    info = youtube.YouTubeClient(youtube.load_credentials(channel.id)).channel()
    print(f"Logged in. Uploads for '{channel.id}' go to: {info['snippet']['title']} "
          f"(https://www.youtube.com/channel/{info['id']}). Token: {path}")
    return 0


def cmd_youtube_check(args) -> int:
    from datetime import datetime, timezone
    from network.providers import youtube
    from network.stages.publish import UPLOADS_PER_DAY, uploads_today
    settings = load_settings()
    channel = _channel_by_id(settings, args.channel)
    try:
        info = youtube.YouTubeClient(youtube.load_credentials(channel.id)).channel()
    except youtube.YouTubeError as e:
        print(e, file=sys.stderr)
        return 1
    conn = db.connect(settings.db_path)
    try:
        used = uploads_today(conn, datetime.now(timezone.utc))
    finally:
        conn.close()
    print(f"'{channel.id}' uploads to: {info['snippet']['title']} (https://www.youtube.com/channel/{info['id']})")
    print(f"uploads today (all channels, resets midnight Pacific): {used}/{UPLOADS_PER_DAY}")
    print(f"privacy from config: {channel.publish.privacy}")
    return 0


def _elevenlabs(settings, channel_id: str | None):
    from network.providers.tts import ElevenLabsTTS
    model = "eleven_flash_v2_5"
    if channel_id:
        ch = _channel_by_id(settings, channel_id)
        model = getattr(getattr(ch, "voice", None), "model", None) or model
    return ElevenLabsTTS(settings.secret("TTS_API_KEY"), model)


def cmd_tts_check(args) -> int:
    from network.providers.tts import TTSError
    settings = load_settings()
    try:
        sub = _elevenlabs(settings, None).subscription()
    except TTSError as e:
        print(e, file=sys.stderr)
        return 1
    used, limit = sub.get("character_count"), sub.get("character_limit")
    print(f"ElevenLabs plan: {sub.get('tier')}  credits used {used} of {limit} "
          f"({(limit or 0) - (used or 0)} left)")
    if sub.get("next_character_count_reset_unix"):
        from datetime import datetime
        print(f"resets: {datetime.fromtimestamp(sub['next_character_count_reset_unix']):%Y-%m-%d %H:%M}")
    return 0


def cmd_tts_voices(args) -> int:
    from network.providers.tts import TTSError
    settings = load_settings()
    try:
        voices = _elevenlabs(settings, None).voices(args.search)
    except TTSError as e:
        print(e, file=sys.stderr)
        return 1
    rows = []
    for v in voices:
        labels = v.get("labels") or {}
        rows.append([v.get("voice_id", ""), v.get("name", ""), labels.get("gender", ""),
                     labels.get("accent", ""), labels.get("age", ""),
                     (labels.get("descriptive") or labels.get("use_case") or "")[:30]])
    print(format_table(["voice_id", "name", "gender", "accent", "age", "style"], rows))
    print("\nListen first: python -m network.cli tts-sample --voice <voice_id>")
    return 0


def cmd_tts_sample(args) -> int:
    from network.providers.tts import TTSError
    settings = load_settings()
    tts = _elevenlabs(settings, args.channel)
    if args.model:
        tts.model_id = args.model
    out_dir = settings.data_dir / "samples"
    out_dir.mkdir(parents=True, exist_ok=True)
    for voice_id in args.voice:
        try:
            r = tts.synthesize(args.text, voice_id, args.speed)
        except TTSError as e:
            print(f"{voice_id}: {e}", file=sys.stderr)
            return 1
        out = out_dir / f"{voice_id}_{tts.model_id}.mp3"
        out.write_bytes(r.audio)
        print(f"{out}  ({len(r.word_timings or [])} word timings, ~{r.characters} credits max)")
    return 0


def _post_clip_hook(conn, settings, channel):
    """After each clip: package it and, if the final gate is on auto, upload it right away."""
    import logging
    from network.clips.pipeline import produce_clip
    from network.stages import package, publish
    log = logging.getLogger("clips")

    def produce(*args, **kwargs):
        job = produce_clip(*args, **kwargs)
        if job is None:
            return job
        try:
            log.info(package.run(conn, channel, settings).summary())
            if channel.gates.final_auto_approve:
                log.info(publish.run(conn, channel, settings).summary())
        except Exception as e:  # noqa: BLE001 - never stop watching because an upload failed
            log.error("packaging/upload after clip %s failed: %s", job, e)
        return job
    return produce


def cmd_clips_watch(args) -> int:
    import threading
    from network.clips.watcher import ClipWatcher
    from network.twitch.auth import TwitchAuthError
    _setup_logging()
    settings = load_settings()
    channel = _clip_channel(settings, args.channel)
    conn = db.connect(settings.db_path)
    stop = threading.Event()
    try:
        watcher = ClipWatcher(conn, channel, settings, _helix(settings),
                              producer=_post_clip_hook(conn, settings, channel))
        mode = "upload automatically" if channel.gates.final_auto_approve else "wait for approval"
        print(f"Watching {len(watcher.streamers)} streamer(s) for {channel.id}; clips will {mode}. "
              "Ctrl+C to stop.")
        watcher.run(stop)
    except KeyboardInterrupt:
        stop.set()
    except TwitchAuthError as e:
        print(e, file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def cmd_clips_simulate(args) -> int:
    from network.clips.simulate import simulate
    _setup_logging()
    settings = load_settings()
    channel = _clip_channel(settings, args.channel)
    conn = db.connect(settings.db_path)
    try:
        jobs = simulate(conn, settings, channel, args.streamer, args.video, args.chat)
    finally:
        conn.close()
    print(f"created clip jobs: {jobs or 'none'}")
    return 0


def cmd_clips_score(args) -> int:
    from network.clips.detector import SpikeDetector
    from network.clips.simulate import load_chat, replay
    settings = load_settings()
    channel = _clip_channel(settings, args.channel)
    spikes = replay(load_chat(args.chat), SpikeDetector(channel.detection))
    for s in spikes:
        m, sec = divmod(int(s.start), 60)
        print(f"{m:02d}:{sec:02d}  {s.explain()}")
        if s.samples:
            print("        chat: " + " | ".join(t[:40] for t in s.samples[:5]))
    kept = sum(s.accepted for s in spikes)
    print(f"{len(spikes)} spike(s), {kept} would be clipped (min_score {channel.detection.min_score:g})")
    return 0


def cmd_run(args) -> int:
    from network.scheduler import run_forever
    settings = load_settings()
    print(f"Starting the scheduler (DRY_RUN={'on' if settings.dry_run else 'OFF'}). Ctrl+C to stop. "
          f"Log: {settings.data_dir / 'logs' / 'scheduler.log'}")
    run_forever(settings, run_now=not args.no_initial)
    return 0


def cmd_telegram_chatid(args) -> int:
    import os
    from network.telegram import TelegramBot, TelegramError
    load_settings()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set in .env", file=sys.stderr)
        return 1
    bot = TelegramBot(token, "0")
    try:
        me = bot.me()
        ups = bot.call("getUpdates", timeout=0)
    except TelegramError as e:
        print(e, file=sys.stderr)
        return 1
    chats = {}
    for u in ups:
        m = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
        c = m.get("chat") or {}
        if c.get("id"):
            chats[c["id"]] = c.get("username") or c.get("first_name") or c.get("title") or ""
    print(f"Bot: @{me.get('username')}")
    if not chats:
        print(f"No messages yet: open Telegram, send any message to @{me.get('username')}, then run this again.")
    for cid, name in chats.items():
        print(f"chat id {cid}  ({name})  -> put TELEGRAM_CHAT_ID={cid} in .env")
    return 0


def cmd_telegram_test(args) -> int:
    from network.stages.approve import make_bot
    from network.telegram import TelegramError
    bot = make_bot(load_settings())
    if bot is None:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first", file=sys.stderr)
        return 1
    try:
        bot.send("🪿 Goose Getaway pipeline connected. Approvals will arrive here.")
    except TelegramError as e:
        print(e, file=sys.stderr)
        return 1
    print("Sent. Check Telegram.")
    return 0


def _not_yet(milestone: int, what: str):
    def run(args) -> int:
        print(f"'{what}' is not implemented yet (Milestone {milestone}).", file=sys.stderr)
        return 2
    return run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m network.cli")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("status", help="print the jobs table")
    p.add_argument("--channel", help="only this channel id")
    p.add_argument("--state", choices=db.JOB_STATES, help="only jobs in this state")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("show", help="print a job's idea, sources and script")
    p.add_argument("job_id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("check-config", help="validate channel YAML files")
    p.add_argument("paths", nargs="*", type=Path, help="files to check (default: channels/*.yaml)")
    p.set_defaults(func=cmd_check_config)

    p = sub.add_parser("run", help="start the scheduler: the whole pipeline, continuously")
    p.add_argument("--no-initial", action="store_true",
                   help="wait for the first scheduled times instead of running signals/ideas now")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("telegram-chatid", help="show chat ids that recently messaged your bot")
    p.set_defaults(func=cmd_telegram_chatid)

    p = sub.add_parser("telegram-test", help="send a test message to your Telegram chat")
    p.set_defaults(func=cmd_telegram_test)

    p = sub.add_parser("run-stage", help="run one stage once")
    p.add_argument("stage", choices=STAGE_NAMES)
    p.add_argument("--channel", help="only this channel id (default: all)")
    p.add_argument("--no-schedule", action="store_true",
                   help="publish only: upload without a scheduled publish time (stays at config privacy)")
    p.set_defaults(func=cmd_run_stage)

    p = sub.add_parser("approve", help="approve a job waiting at a gate")
    p.add_argument("job_id", type=int)
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", help="reject a job waiting at a gate")
    p.add_argument("job_id", type=int)
    p.add_argument("--reason")
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("retry", help="put a failed job back in the state it failed in")
    p.add_argument("job_id", type=int)
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("youtube-login", help="sign in to the YouTube channel a config uploads to")
    p.add_argument("--channel", required=True)
    p.set_defaults(func=cmd_youtube_login)

    p = sub.add_parser("youtube-check", help="show which YouTube channel a config uploads to, and quota used")
    p.add_argument("--channel", required=True)
    p.set_defaults(func=cmd_youtube_check)

    p = sub.add_parser("tts-check", help="show ElevenLabs credits used and left")
    p.set_defaults(func=cmd_tts_check)

    p = sub.add_parser("tts-voices", help="list ElevenLabs default voices")
    p.add_argument("--search")
    p.set_defaults(func=cmd_tts_voices)

    p = sub.add_parser("tts-sample", help="render a short sample per voice into data/samples")
    p.add_argument("--voice", nargs="+", required=True)
    p.add_argument("--channel", default="gta6", help="take the model from this channel's config")
    p.add_argument("--model", help="override the model, e.g. eleven_v3")
    p.add_argument("--speed", type=float, default=1.05)
    p.add_argument("--text", default=(
        "GTA 6 almost got blocked in South Korea. The rating board split three to three, "
        "and one leaked clip nearly changed everything."))
    p.set_defaults(func=cmd_tts_sample)

    p = sub.add_parser("twitch-login", help="log in to Twitch (device code) for reading chat")
    p.set_defaults(func=cmd_twitch_login)

    clips = sub.add_parser("clips", help="live clipping from permitted streamers")
    csub = clips.add_subparsers(dest="clips_command", required=True, metavar="action")
    p = csub.add_parser("check", help="check the Twitch login and who is live")
    p.add_argument("--channel")
    p.set_defaults(func=cmd_clips_check)
    p = csub.add_parser("watch", help="watch live streams and clip chat spikes")
    p.add_argument("--channel")
    p.set_defaults(func=cmd_clips_watch)
    p = csub.add_parser("simulate", help="run detection and cutting on a local video + chat log")
    p.add_argument("--channel")
    p.add_argument("--streamer", required=True, help="a permitted streamer login from the channel YAML")
    p.add_argument("--video", required=True, type=Path)
    p.add_argument("--chat", required=True, type=Path, help="JSON lines: {\"t\": sec, \"text\": ..., \"emotes\": n}")
    p.set_defaults(func=cmd_clips_simulate)
    p = csub.add_parser("score", help="replay a chat log and show every spike with its score (no video)")
    p.add_argument("--channel")
    p.add_argument("--chat", required=True, type=Path, help="JSON lines: {\"t\": sec, \"text\": ..., \"user\": ...}")
    p.set_defaults(func=cmd_clips_score)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # e.g. curly quotes on a cp1252 console
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

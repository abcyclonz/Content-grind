# Handover: Content Network (GooseGetaway)

Last updated: 2026-11-17

This repo is an automated YouTube Shorts pipeline. Stage 1 runs one channel, **GooseGetaway**, which
posts GTA VI news Shorts. A second channel type that clips live streams is built, but it is waiting on
streamer permissions. The full spec is in `SPEC.md`.

---

## 1. What it does

```
RSS news ─► ideas (LLM) ─► [you approve] ─► script (LLM) ─► [you approve]
        ─► voice (ElevenLabs) ─► word-timed captions (faster-whisper)
        ─► background visuals ─► assembled vertical video + goose-honk outro
        ─► title / description / tags (trend-aware) ─► [you approve] ─► YouTube upload (private)
```

- **Approvals** happen in Telegram, through the bot @GooseGetawayBot. It sends ideas, scripts and the
  finished video with Approve, Reject and Edit buttons.
- **Scheduler:** `python -m network.cli run` runs everything:
  - news every 2 h;
  - ideas daily at 07:00 IST;
  - the pipeline every 3 min;
  - Telegram polling all the time.
- **State** is kept in SQLite (`data/network.db`). Every stage can be re-run safely, and a failed job
  resumes where it stopped (`retry`).
- **Uploads are private**, because `use_publish_times: false` is set in `channels/gta6.yaml`. Nothing
  goes public automatically. That switch should only be flipped after the videos have been reviewed.

## 2. Current status

| Area | Status |
|---|---|
| News → script → voice → captions → video → package → upload | ✅ Works end to end, live |
| Telegram approvals | ✅ Works |
| YouTube upload | ✅ 2 Shorts uploaded **private** (youtu.be/RdxuPDjaYqQ, youtu.be/RJym2iwJcPU) |
| Visuals | ⚠️ Only procedural neon gradients. **Videos look "empty"**, the main weakness |
| Live-stream clipper (Twitch) | 🟡 Code done and tested on simulations. **Not yet run on a real stream** |
| Chat-hype detection | ✅ Just upgraded: scores spikes 0-100 and ignores spam, bots, raids and plain conversation |
| Kick / other platforms | ❌ Not started |

Jobs in the database right now:
- #1 and #2 are published (private).
- #3 was rejected.
- **#4 ("Kemi Badenoch on GTA 6") is packaged and waiting at the final approval.**

The scheduler is **not running** at the moment.

## 3. Setup on a new machine (Windows)

1. Install Python 3.12+.
2. Run these commands:
   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
3. Copy FFmpeg 9.x into `tools/ffmpeg/bin/` (ffmpeg.exe and ffprobe.exe). The `tools/` folder is not in
   git.
4. Copy `.env.example` to `.env` and fill it in (section 4).
5. Put the YouTube OAuth client JSON in `%USERPROFILE%\.content-network\client_secret.json`. Then run
   `python -m network.cli youtube-login --channel gta6`.
6. Check everything:
   ```
   python -m network.cli check-config
   python -m network.cli tts-check
   python -m network.cli youtube-check --channel gta6
   python -m network.cli telegram-test
   .venv\Scripts\python -m pytest -q    (225 tests, all should pass)
   ```
7. The LLM is the **Claude Code CLI**, not an API key. `claude` must be installed and logged in on the
   machine. See `network/providers/llm.py`.

## 4. Secrets

They live in **`.env`** and **`~/.content-network/`**. None of them are in git. Ask the owner for the
values over a private channel. Never commit them.

| Name | What it is |
|---|---|
| `DRY_RUN` | `1` means nothing is uploaded or charged. `0` is live |
| `TTS_API_KEY` | ElevenLabs key. The Starter plan has 40k credits/month, and each Short costs up to about 900 |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | The approval bot, and the chat it sends to |
| `YOUTUBE_CLIENT_SECRET_FILE` | Path to the Google OAuth client JSON |
| `TWITCH_CLIENT_ID` | **Still empty.** It comes from dev.twitch.tv/console (Public client) |
| `~/.content-network/youtube_token_gta6.json` | YouTube login. It expires weekly while the Google app is in "Testing" mode |

## 5. Everyday commands

```
python -m network.cli status                     # all jobs
python -m network.cli show <job>                 # details of one job
python -m network.cli run                        # start the scheduler (leave it running)
python -m network.cli run-stage <stage> --channel gta6   # stages: signals ideas script voice captions visuals assemble package publish
python -m network.cli approve <job> / reject <job> / retry <job>
python -m network.cli clips score --chat log.jsonl       # replay a chat log, show spike scores
python -m network.cli clips simulate --streamer X --video v.mp4 --chat log.jsonl
```

## 6. Code map

| Path | What |
|---|---|
| `channels/gta6.yaml` | The channel config: sources, voice, outro, privacy, gates |
| `channels/examples/clips.example.yaml` | Template for a clips channel |
| `network/stages/` | One file per pipeline stage |
| `network/providers/` | LLM (Claude CLI), TTS (ElevenLabs/Google), YouTube |
| `network/render.py`, `media.py` | FFmpeg video building (captions via libass, crossfades, outro) |
| `network/align.py` | Word timing for captions (faster-whisper on CPU) |
| `network/trends.py` | Trending Shorts lookup used to write titles |
| `network/telegram.py`, `stages/approve.py` | Telegram approvals |
| `network/scheduler.py` | The `run` loop |
| `network/twitch/` | Twitch login (device code), Helix API, EventSub chat |
| `network/clips/` | Live clipper: recorder, chat-spike detector, cutter, watcher |
| `network/db.py` | SQLite schema and migrations (append-only), plus job state machine |
| `tests/` | pytest, all offline using fixtures |

## 7. Rules we agreed on (please keep them)

1. **Nothing public without a human watching it first.** Uploads stay private.
2. **No other streamer's footage is published without their written permission.** A clips channel
   config will not load unless each streamer has a `permission.evidence` link or file. Any test renders
   using someone's stream stay local only (for example, the Kai Cenat test in
   `data/samples/test_short_kaicenat_PRIVATE.mp4`).
3. **No new paid services.** The owner does not want to spend more. ElevenLabs Starter is the only
   paid thing.
4. Secrets stay in `.env` and `~/.content-network/`, never in the code.
5. Shorts are flagged `containsSyntheticMedia` (AI voice), and descriptions say "Narration is
   AI-generated."

## 8. Next steps (in priority order)

1. **Better visuals for news Shorts.** Use free stock footage from Pexels or Pixabay (needs a free API
   key) matched to each script, with Cloudflare FLUX free-tier AI images as backup. This fixes the
   "empty video" problem.
2. **Twitch setup.** Create the app (Public client, redirect `http://localhost`), put the Client ID in
   `.env`, then run `python -m network.cli twitch-login`. After that, test `clips watch` on a live GTA
   stream in local-only mode.
3. **Streamer permissions.** Collect written OK from streamers, then create `channels/<clips>.yaml` from
   the example.
4. **Google OAuth app to production.** It needs a homepage and a privacy-policy URL. Until then, the
   YouTube login must be redone every 7 days.
5. Optional:
   - add an audio-loudness check to confirm chat spikes;
   - add an AI "what happened" line on clips;
   - add a split gameplay/facecam layout for stream clips;
   - add Kick support.

## 9. Gotchas

- **Windows:**
  - Call `claude.exe` directly. The `.cmd` shim mangles JSON arguments.
  - The final render moves files across drives, so a `shutil.move` fallback is in place.
- **NVENC fails on this GPU** because the driver is too old, so encoding falls back to libx264
  automatically.
- **Whisper on GPU needs CUDA 12 cuBLAS,** which isn't installed. CPU int8 is fast enough (a few seconds
  per Short).
- **YouTube quota:** about 100 uploads per day per project. The publish stage stops before that limit.
- **Twitch refresh tokens are one-time use.** The code saves each new one immediately, so don't copy
  token files between machines that are both running.

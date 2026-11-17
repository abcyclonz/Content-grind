# Content Network: Build Spec, Stage 1

Stage 1 goal: one channel (GTA VI), one format (Shorts), running end to end from trend signal to scheduled YouTube upload, with approval gates that can be switched off per stage.

Later stages (not in scope now): GTA daily long video, explainer channel, phones channel, clips channel, analytics feedback loop.

## 1. Instructions for Claude Code

- Build milestone by milestone (section 10). Stop after each one, show me how to run its acceptance test, and wait for my go-ahead.
- Verify external API details against current official docs before coding them (YouTube Data API fields and quota, Anthropic model names, provider SDKs). Do not rely on memory for these.
- Never write secrets into code or commit them. All keys come from `.env`.
- Ask before installing anything system-wide. Python packages go in a virtual environment.
- Every stage must be idempotent and resumable: re-running a stage on a finished job does nothing; re-running on a failed job retries it.
- Write a small test for each stage using fixture data, so stages can be tested without spending API credits.

## 2. Environment

- Windows 11 laptop, Intel i5, NVIDIA RTX 3060 mobile (6 GB VRAM).
- Python 3.11+, FFmpeg on PATH, SQLite.
- No local LLMs or local image/video generation. All generation goes through cloud APIs.
- The GPU is used only for: FFmpeg `h264_nvenc` encoding (fall back to `libx264`), and optionally `faster-whisper` small model for caption timing (fall back to CPU int8).
- Use `pathlib` everywhere; no hard-coded path separators. The project must also run on a Linux server later without changes.

## 3. Design principles

1. One pipeline, many channels. A channel is a YAML config file; no channel-specific code.
2. Providers sit behind interfaces (`LLMProvider`, `TTSProvider`, `ImageProvider`) so they can be swapped in config.
3. All state lives in SQLite. Files on disk are referenced from the database.
4. Each stage has an `auto_approve` flag per channel. Off means: send a Telegram message and wait.
5. A global `DRY_RUN` mode runs everything except paid API calls and the upload, using fixtures.
6. Every paid call is logged with estimated cost. A daily budget cap per channel stops the pipeline when exceeded.

## 4. Project structure

```
content-network/
  SPEC.md
  .env.example
  requirements.txt
  channels/
    gta6.yaml
  network/
    cli.py              # entry point: run, run-stage, status, approve
    config.py           # loads .env and channel YAML
    db.py               # schema, migrations, job state helpers
    scheduler.py        # APScheduler loop
    providers/
      llm.py            # Anthropic implementation
      tts.py
      image.py
    stages/
      signals.py
      ideas.py
      script.py
      voice.py
      captions.py
      visuals.py
      assemble.py
      package.py
      approve.py        # Telegram bot
      publish.py        # YouTube upload
  assets/
    gta6/
      official/         # images I add by hand (optional)
      music/            # music I have rights to use (optional)
      fonts/
  data/
    network.db
    jobs/<job_id>/      # script.json, voice.mp3, images, final.mp4, thumb
  tests/
    fixtures/
```

## 5. Channel config (channels/gta6.yaml)

```yaml
id: gta6
name: "TBD"
language: en
format: short
shorts_per_day: 3
publish_times_local: ["09:00", "14:00", "19:00"]
timezone: "Asia/Kolkata"
daily_budget_usd: 5

sources:
  rss: []            # Claude Code: propose a list, verify each feed responds
  keywords: ["GTA 6", "GTA VI", "Rockstar Games", "Vice City", "Leonida"]

models:
  script: claude-sonnet-5-5
  scoring: claude-haiku-5-5
  metadata: claude-haiku-5-5

voice:
  provider: TBD
  voice_id: TBD
  speed: 1.05

visuals:
  provider: TBD
  style_prompt: >
    Stylized neon-lit Florida coastal city, cinematic, vertical 9:16.
    Original imagery only. No logos, no game UI, no named characters.
  images_per_short: 6
  use_official_folder: true

video:
  width: 1080
  height: 1920
  fps: 30
  max_seconds: 55
  caption_style: word_highlight

gates:                # false = wait for Telegram approval
  ideas_auto_approve: false
  script_auto_approve: false
  final_auto_approve: false

publish:
  privacy: private    # switch to public or scheduled when ready
  category_id: 20     # Gaming; verify
  made_for_kids: false
```

## 6. Database

Tables (SQLite):

- `signals`: id, channel_id, source, url, title, text, published_at, fetched_at, hash (unique).
- `ideas`: id, channel_id, title, angle, signal_ids (JSON), score, status, created_at.
- `jobs`: id, channel_id, idea_id, state, error, attempts, created_at, updated_at, scheduled_publish_at, youtube_video_id.
- `artifacts`: id, job_id, kind (script, voice, captions, image, video, thumbnail, metadata), path, meta (JSON).
- `costs`: id, job_id, provider, operation, units, est_usd, created_at.
- `published_topics`: channel_id, topic_hash, title, published_at (for de-duplication).

Job states, in order:

```
idea_pending -> idea_approved -> scripted -> script_approved -> voiced
-> captioned -> visuals_ready -> assembled -> packaged -> final_approved
-> scheduled -> published
```

Plus `failed` (with error and attempts) and `rejected`.

## 7. Stages

### 7.1 signals
Fetch all RSS feeds in the channel config with `feedparser`. Keep items matching keywords from the last 48 hours. Store title, link, and cleaned article text (fetch the page, extract main text). De-duplicate by hash.

### 7.2 ideas
Send recent signals to the scoring model. Ask for ranked short-video ideas as JSON: title, angle (one sentence), supporting signal ids, score 0 to 100. Drop ideas similar to `published_topics`. Create jobs for the top N where N is `shorts_per_day`. Gate: ideas.

### 7.3 script
Input: the idea plus the full text of its supporting signals. Rules for the prompt:

- Use only facts present in the supplied source text. GTA VI launched 19 November 2026, after the model's training data, so memory about the game is unreliable. If the sources do not support a claim, leave it out.
- 110 to 140 spoken words. Hook in the first sentence. No intro, no "in this video".
- Vary structure between videos: do not reuse the same opening pattern or sign-off.

Output JSON: `hook`, `lines` (array of {text, visual_prompt}), `sources` (URLs). Gate: script. On approval via Telegram, I can also reply with edited text, which replaces the script.

### 7.4 voice
Call the TTS provider with the joined script. Save `voice.mp3`. Normalize loudness with FFmpeg (`loudnorm`).

### 7.5 captions
Get word-level timestamps. Use the TTS provider's timestamps if it returns them; otherwise run `faster-whisper` (small) on the audio and align to the known script text. Save `captions.json`.

### 7.6 visuals
For each script line, either pick an unused image from `assets/gta6/official/` (if enabled and available) or generate one from `style_prompt` plus the line's `visual_prompt`. Save 9:16 images. Assign each a time span from the caption timestamps.

### 7.7 assemble
FFmpeg render: images with slow zoom/pan, crossfades, burned-in word-highlight captions, voice track, optional music from `assets/gta6/music/` ducked under the voice. Output `final.mp4`, 1080x1920, 30 fps, H.264 + AAC. Try `h264_nvenc`, fall back to `libx264`. Fail the job if duration exceeds `max_seconds`.

### 7.8 package
Metadata model writes: title (under 70 characters), description (with source links), tags. Save as `metadata.json`. Shorts do not need a custom thumbnail in stage 1.

### 7.9 approve (Telegram)
A bot using long polling (no public webhook needed). For each gate it sends the item (text, or the video file for the final gate) with Approve / Reject buttons. Only my chat id is accepted. If the gate's `auto_approve` is true, skip the message and advance.

### 7.10 publish
YouTube Data API v3, OAuth installed-app flow, refresh token stored locally outside the repo. Resumable upload with retry. Set privacy from config; if a publish time is set, upload as private with `publishAt`. Set the altered or synthetic content disclosure field if the API exposes it (verify in current docs). Record `youtube_video_id`.

Check quota before uploading and stop cleanly if it would be exceeded. Note for me: uploads from an unaudited API project may be locked to private until Google's audit is passed; report clearly if this happens.

### 7.11 scheduler
A single long-running process using APScheduler: signals every 2 hours; ideas once each morning; the remaining stages poll for jobs in the matching state every few minutes. `python -m network.cli run` starts it. `python -m network.cli status` prints the jobs table.

## 8. Providers

- LLM: Anthropic API. Model names come from the channel config; do not hard-code them.
- TTS and images: I have not chosen providers. Before Milestone 4 and Milestone 5, propose two or three current options each with price per Short, and let me pick. Implement the chosen one behind the interface.

## 9. Secrets (.env)

```
ANTHROPIC_API_KEY=
TTS_API_KEY=
IMAGE_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
YOUTUBE_CLIENT_SECRET_FILE=
DRY_RUN=1
```

## 10. Milestones and acceptance tests

1. Skeleton: project structure, config loader, database, CLI `status`. Test: `status` prints an empty jobs table; config validation rejects a broken YAML.
2. Signals and ideas. Test: running both stages creates at least 3 jobs in `idea_pending` from live feeds.
3. Script. Test: an approved idea produces `script.json` that passes schema validation and cites source URLs.
4. Voice and captions. Test: `voice.mp3` exists; `captions.json` has a timestamp for every word.
5. Visuals and assembly. Test: `final.mp4` plays, is 1080x1920, under 55 seconds, with readable captions in sync.
6. Telegram gates. Test: I receive each gate message, and Approve / Reject move the job correctly; flipping `auto_approve` skips the message.
7. Publish. Test: one video uploads to my channel as private with the correct title and description.
8. Scheduler. Test: with all gates on auto-approve and `DRY_RUN=0`, the system produces and uploads 3 private Shorts in a day without me touching it.

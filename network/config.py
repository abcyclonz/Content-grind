"""Load .env settings and validate channel YAML files."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHANNELS_DIR = PROJECT_ROOT / "channels"
ASSETS_DIR = PROJECT_ROOT / "assets"

SECRET_NAMES = (
    "ANTHROPIC_API_KEY",
    "TTS_API_KEY",
    "IMAGE_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "YOUTUBE_CLIENT_SECRET_FILE",
    "TWITCH_CLIENT_ID",
    "GOOGLE_TTS_CREDENTIALS_FILE",
)

# Tokens obtained by OAuth logins live outside the repo, per user.
SECRETS_DIR = Path.home() / ".content-network"


class ConfigError(Exception):
    """Raised for any invalid .env value or channel config."""


# --------------------------------------------------------------------------
# Channel config
# --------------------------------------------------------------------------

class _Strict(BaseModel):
    # extra="forbid": a misspelled key is an error, not silently ignored.
    # strict=True: no "yes" -> True or "5" -> 5 coercion.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


NonEmptyStr = Annotated[str, Field(min_length=1)]


class Sources(_Strict):
    rss: list[str] = Field(default_factory=list)
    keywords: list[NonEmptyStr] = Field(min_length=1)
    max_age_hours: int = Field(default=48, ge=1, le=24 * 14)

    @field_validator("rss")
    @classmethod
    def _urls(cls, v: list[str]) -> list[str]:
        for url in v:
            if not re.match(r"^https?://\S+$", url):
                raise ValueError(f"not an http(s) URL: {url!r}")
        return v


class LLM(_Strict):
    # "anthropic_api" can be added here later if an API key becomes available.
    provider: Literal["claude_cli"] = "claude_cli"


class Models(_Strict):
    # Names are not checked against a list: they come from config only.
    script: NonEmptyStr
    scoring: NonEmptyStr
    metadata: NonEmptyStr


class Script(_Strict):
    min_words: int = Field(default=110, ge=20)
    max_words: int = Field(default=140, ge=20)

    @model_validator(mode="after")
    def _order(self) -> "Script":
        if self.min_words > self.max_words:
            raise ValueError("min_words must not exceed max_words")
        return self


class Voice(_Strict):
    provider: NonEmptyStr          # elevenlabs | google_chirp3
    voice_id: NonEmptyStr
    model: str | None = None       # provider model, e.g. eleven_flash_v2_5
    speed: float = Field(gt=0, le=2)


class Outro(_Strict):
    """Channel signature at the very end: a short sound plus a text pop."""
    sound: str | None = None      # file under the project, e.g. assets/gta6/brand/goose_honk.wav
    text: str = ""
    seconds: float = Field(default=1.2, ge=0.3, le=3.0)

    @field_validator("sound")
    @classmethod
    def _exists(cls, v: str | None) -> str | None:
        if v and not (PROJECT_ROOT / v).is_file():
            raise ValueError(f"outro sound not found: {v!r}")
        return v


class Captions(_Strict):
    whisper_model: str = "small"
    device: Literal["auto", "cuda", "cpu"] = "auto"


class Visuals(_Strict):
    provider: NonEmptyStr          # "TBD" allowed until Milestone 5
    style_prompt: NonEmptyStr
    images_per_short: int = Field(ge=1, le=30)
    use_official_folder: bool


class Video(_Strict):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: int = Field(ge=1, le=60)
    max_seconds: int = Field(ge=1, le=60)  # YouTube Shorts limit
    caption_style: Literal["word_highlight"]


class Gates(_Strict):
    ideas_auto_approve: bool
    script_auto_approve: bool
    final_auto_approve: bool


class Publish(_Strict):
    privacy: Literal["private", "unlisted", "public"]
    category_id: int = Field(ge=1)
    made_for_kids: bool
    # YouTube's altered/synthetic content disclosure (status.containsSyntheticMedia).
    # Unset: true for Shorts (AI voice and images), false for clips (real footage).
    contains_synthetic_media: bool | None = None
    # Shorts: true = upload private with publishAt at the next publish_times_local slot,
    # so YouTube makes them PUBLIC automatically at that time. false = stay at `privacy`.
    use_publish_times: bool = True


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ChannelConfig(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: NonEmptyStr
    # What the channel covers, in plain words; goes into every LLM prompt.
    topic_brief: NonEmptyStr
    language: str = Field(min_length=2)
    format: Literal["short"]
    shorts_per_day: int = Field(ge=1, le=20)
    publish_times_local: list[str]
    timezone: str
    daily_budget_usd: float = Field(gt=0)
    sources: Sources
    llm: LLM = Field(default_factory=LLM)
    models: Models
    script: Script = Field(default_factory=Script)
    voice: Voice
    captions: Captions = Field(default_factory=Captions)
    outro: Outro | None = None
    visuals: Visuals
    video: Video
    gates: Gates
    publish: Publish

    @field_validator("publish_times_local")
    @classmethod
    def _times(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if not _HHMM.match(t)]
        if bad:
            raise ValueError(f"times must be HH:MM (24h), got {bad}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate publish times")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown IANA timezone {v!r}") from None
        return v

    @model_validator(mode="after")
    def _times_match_count(self) -> "ChannelConfig":
        if len(self.publish_times_local) != self.shorts_per_day:
            raise ValueError(
                f"publish_times_local has {len(self.publish_times_local)} entries "
                f"but shorts_per_day is {self.shorts_per_day}"
            )
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def assets_dir(self) -> Path:
        return ASSETS_DIR / self.id


# --------------------------------------------------------------------------
# Clip channels (format: clip): highlights from streamers who gave permission
# --------------------------------------------------------------------------

class Permission(_Strict):
    # URL (DM link, panel, Discord post) or a file under the project, e.g. a screenshot.
    evidence: NonEmptyStr
    granted_on: date
    conditions: str = ""

    @field_validator("evidence")
    @classmethod
    def _evidence_exists(cls, v: str) -> str:
        if re.match(r"^https?://\S+$", v):
            return v
        if not (PROJECT_ROOT / v).is_file():
            raise ValueError(f"permission evidence must be a URL or an existing file in the project: {v!r}")
        return v

    @field_validator("granted_on")
    @classmethod
    def _not_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("granted_on is in the future")
        return v


class Streamer(_Strict):
    platform: Literal["twitch", "youtube"]  # youtube is used for past streams (clips youtube) only
    login: str = Field(pattern=r"^[a-z0-9_]{3,25}$")  # Twitch logins are lower case
    display_name: str | None = None
    credit: str | None = None  # text burned into the clip; default "twitch.tv/<login>"
    enabled: bool = True
    permission: Permission

    @property
    def credit_text(self) -> str:
        if self.credit:
            return self.credit
        return f"youtube.com/@{self.login}" if self.platform == "youtube" else f"twitch.tv/{self.login}"

    @property
    def name(self) -> str:
        return self.display_name or self.login


class Detection(_Strict):
    window_seconds: int = Field(default=8, ge=2, le=60)
    baseline_minutes: int = Field(default=5, ge=1, le=60)
    warmup_minutes: int = Field(default=3, ge=0, le=60)
    spike_ratio: float = Field(default=3.0, gt=1)
    min_messages_per_second: float = Field(default=1.5, ge=0)
    cooldown_seconds: int = Field(default=120, ge=0)
    hype_weight: float = Field(default=1.5, ge=1)  # weight of emote spam / caps / "LUL" messages
    min_zscore: float = Field(default=3.0, ge=0)   # also this many robust std devs above normal
    min_unique_chatters: int = Field(default=5, ge=1)
    max_messages_per_chatter: int = Field(default=2, ge=1)   # per window: spammers count once or twice
    min_score: float = Field(default=40, ge=0, le=100)       # 0-100 judgement needed to clip
    ignore_users: list[str] = Field(default_factory=list)     # extra bots (common ones are built in)
    ignore_commands: bool = True                              # skip "!enter", "!discord", ...


class ClipSettings(_Strict):
    pre_seconds: int = Field(default=25, ge=0, le=55)
    post_seconds: int = Field(default=10, ge=0, le=55)
    buffer_minutes: int = Field(default=5, ge=1, le=60)
    max_clips_per_day: int = Field(default=10, ge=1)
    layout: Literal["blur_pad", "center_crop"] = "blur_pad"
    quality: str = "1080p60,1080p,720p60,720p,best"  # streamlink quality list


class ClipVideo(_Strict):
    width: int = Field(default=1080, gt=0)
    height: int = Field(default=1920, gt=0)
    fps: int = Field(default=30, ge=1, le=60)
    max_seconds: int = Field(default=59, ge=1, le=60)


class ClipGates(_Strict):
    final_auto_approve: bool


class ClipModels(_Strict):
    metadata: NonEmptyStr  # writes clip titles from stream title + chat reaction


class ClipChannelConfig(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: NonEmptyStr
    language: str = Field(min_length=2)
    format: Literal["clip"]
    timezone: str
    daily_budget_usd: float = Field(gt=0)
    streamers: list[Streamer] = Field(min_length=1)
    detection: Detection = Field(default_factory=Detection)
    clip: ClipSettings = Field(default_factory=ClipSettings)
    video: ClipVideo = Field(default_factory=ClipVideo)
    llm: LLM = Field(default_factory=LLM)
    models: ClipModels
    gates: ClipGates
    publish: Publish
    # Optional: daily publish slots (local HH:MM). Empty = clips go up straight away at
    # `publish.privacy`. Set = each clip is uploaded private with publishAt at the next
    # free slot, so YouTube makes it public at that time (one clip per slot).
    publish_times_local: list[str] = Field(default_factory=list)

    @field_validator("publish_times_local")
    @classmethod
    def _times(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if not _HHMM.match(t)]
        if bad:
            raise ValueError(f"times must be HH:MM (24h), got {bad}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate publish times")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown IANA timezone {v!r}") from None
        return v

    @model_validator(mode="after")
    def _checks(self) -> "ClipChannelConfig":
        logins = [s.login for s in self.streamers]
        dupes = {l for l in logins if logins.count(l) > 1}
        if dupes:
            raise ValueError(f"streamer listed twice: {sorted(dupes)}")
        if self.clip.pre_seconds + self.clip.post_seconds > self.video.max_seconds:
            raise ValueError("clip.pre_seconds + clip.post_seconds exceeds video.max_seconds")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def enabled_streamers(self) -> list[Streamer]:
        return [s for s in self.streamers if s.enabled]


AnyChannel = ChannelConfig | ClipChannelConfig
_FORMATS = {"short": ChannelConfig, "clip": ClipChannelConfig}


def _format_validation_error(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "(top level)"
        lines.append(f"  {loc}: {e['msg']}")
    return "\n".join(lines)


def load_channel(path: Path | str) -> AnyChannel:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"{path}: cannot read file ({e.strerror})") from None
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(e, "problem", None) or str(e)
        raise ConfigError(f"{path}: invalid YAML{where}: {problem}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping of keys, got {type(raw).__name__}")
    model = _FORMATS.get(raw.get("format"))
    if model is None:
        raise ConfigError(f"{path}: invalid channel config\n  format: must be one of {sorted(_FORMATS)}, "
                          f"got {raw.get('format')!r}")
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"{path}: invalid channel config\n{_format_validation_error(e)}") from None


def channel_files(channels_dir: Path = CHANNELS_DIR) -> list[Path]:
    return sorted(p for p in channels_dir.glob("*.y*ml") if p.suffix in (".yaml", ".yml"))


def load_channels(channels_dir: Path = CHANNELS_DIR) -> dict[str, AnyChannel]:
    channels: dict[str, AnyChannel] = {}
    for path in channel_files(channels_dir):
        cfg = load_channel(path)
        if cfg.id in channels:
            raise ConfigError(f"{path}: duplicate channel id {cfg.id!r}")
        channels[cfg.id] = cfg
    return channels


# --------------------------------------------------------------------------
# .env settings
# --------------------------------------------------------------------------

def _parse_bool(name: str, value: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be 1 or 0, got {value!r}")


@dataclass(frozen=True)
class Settings:
    dry_run: bool
    data_dir: Path
    channels_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "network.db"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def llm_workdir(self) -> Path:
        """Empty folder the Claude CLI runs in, so it picks up no project files."""
        return self.data_dir / "llm_workdir"

    @staticmethod
    def secret(name: str) -> str:
        """Return a secret from the environment, or raise if it is missing.

        Secrets are read on demand and never stored on this object, so they
        cannot leak through repr() or logs.
        """
        if name not in SECRET_NAMES:
            raise KeyError(f"unknown secret {name!r}")
        value = os.environ.get(name, "").strip()
        if not value:
            raise ConfigError(f"{name} is not set; add it to .env")
        return value


def load_settings(env_file: Path | None = None) -> Settings:
    # Real environment variables win over .env values.
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    data_dir = os.environ.get("NETWORK_DATA_DIR", "").strip()
    return Settings(
        dry_run=_parse_bool("DRY_RUN", os.environ.get("DRY_RUN", "1")),
        data_dir=Path(data_dir) if data_dir else PROJECT_ROOT / "data",
        channels_dir=CHANNELS_DIR,
    )

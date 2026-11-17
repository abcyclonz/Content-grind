"""LLM providers.

claude_cli: runs the Claude Code CLI headless (`claude -p`) with the user's
Claude login. Tools, MCP servers and project settings are turned off and the
default system prompt is replaced, so it behaves like a plain API call.

In DRY_RUN a FixtureLLMProvider returns canned output and costs nothing.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from network import costs, db
from network.config import ChannelConfig, Settings


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    data: dict[str, Any]
    est_usd: float
    model: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class LLMProvider(Protocol):
    name: str

    def complete_json(
        self, *, model: str, system: str, prompt: str, schema: dict[str, Any]
    ) -> LLMResult: ...


# --------------------------------------------------------------------------
# Claude Code CLI
# --------------------------------------------------------------------------

def find_claude_exe() -> str:
    """Locate the real claude executable.

    On Windows, npm installs a claude.cmd batch shim. Batch files mangle
    arguments containing newlines, quotes or %, so we call the claude.exe it
    wraps instead. Override with CLAUDE_CLI_PATH.
    """
    found = os.environ.get("CLAUDE_CLI_PATH", "").strip() or shutil.which("claude")
    if not found:
        raise LLMError("Claude Code CLI not found on PATH; install it or set CLAUDE_CLI_PATH")
    path = Path(found)
    if path.suffix.lower() in (".cmd", ".bat", ".ps1"):
        exe = path.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if exe.exists():
            return str(exe)
        raise LLMError(f"{path} is a shell shim; set CLAUDE_CLI_PATH to the real claude executable")
    return str(path)


class ClaudeCLIProvider:
    name = "claude_cli"

    def __init__(
        self,
        workdir: Path,
        exe: str | None = None,
        timeout: float = 300,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self.workdir = Path(workdir)
        self.exe = exe
        self.timeout = timeout
        self.runner = runner

    def build_command(self, model: str, system: str, schema: dict[str, Any]) -> list[str]:
        return [
            self.exe or find_claude_exe(),
            "-p",
            "--model", model,
            "--output-format", "json",
            "--json-schema", json.dumps(schema, separators=(",", ":")),
            "--system-prompt", system,
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--no-session-persistence",
        ]

    def complete_json(
        self, *, model: str, system: str, prompt: str, schema: dict[str, Any]
    ) -> LLMResult:
        self.workdir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        # The point of this provider is the Claude login; an (empty or real)
        # ANTHROPIC_API_KEY loaded from .env would silently override it.
        env.pop("ANTHROPIC_API_KEY", None)
        try:
            proc = self.runner(
                self.build_command(model, system, schema),
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout,
                cwd=self.workdir,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise LLMError(f"claude CLI timed out after {self.timeout:.0f}s") from None
        except OSError as e:
            raise LLMError(f"could not start claude CLI: {e}") from None

        try:
            out = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError):
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            raise LLMError(f"claude CLI exit {proc.returncode}, no JSON output: {tail}") from None
        if proc.returncode != 0 or out.get("is_error"):
            detail = out.get("result") or out.get("api_error_status") or out.get("subtype")
            raise LLMError(f"claude CLI error (exit {proc.returncode}): {str(detail)[:400]}")

        data = out.get("structured_output")
        if data is None:
            try:
                data = json.loads(out.get("result") or "")
            except json.JSONDecodeError:
                raise LLMError("claude CLI returned no structured output") from None
        if not isinstance(data, dict):
            raise LLMError(f"expected a JSON object, got {type(data).__name__}")
        missing = [k for k in schema.get("required", []) if k not in data]
        if missing:
            raise LLMError(f"model output is missing required keys {missing}")
        return LLMResult(data=data, est_usd=float(out.get("total_cost_usd") or 0.0), model=model, raw=out)


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------

class FixtureLLMProvider:
    """Returns canned output from a stage-supplied function; never calls out."""

    name = "fixture"

    def __init__(self, responder: Callable[[], dict[str, Any]]):
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self, *, model: str, system: str, prompt: str, schema: dict[str, Any]
    ) -> LLMResult:
        self.calls.append({"model": model, "system": system, "prompt": prompt, "schema": schema})
        return LLMResult(data=self.responder(), est_usd=0.0, model=model)


def make_llm(
    channel: ChannelConfig, settings: Settings, fixture: Callable[[], dict[str, Any]]
) -> LLMProvider:
    if settings.dry_run:
        return FixtureLLMProvider(fixture)
    if channel.llm.provider == "claude_cli":
        return ClaudeCLIProvider(settings.llm_workdir)
    raise LLMError(f"unknown LLM provider {channel.llm.provider!r}")


def complete_json_logged(
    conn: sqlite3.Connection,
    channel: ChannelConfig,
    llm: LLMProvider,
    *,
    operation: str,
    model: str,
    system: str,
    prompt: str,
    schema: dict[str, Any],
    job_id: int | None = None,
) -> LLMResult:
    """Budget check, call, cost log: the only way stages should call an LLM."""
    costs.check_budget(conn, channel)
    result = llm.complete_json(model=model, system=system, prompt=prompt, schema=schema)
    db.log_cost(
        conn, channel.id, llm.name, operation, result.est_usd,
        units=float((result.raw.get("usage") or {}).get("output_tokens") or 0) or None,
        job_id=job_id,
    )
    return result

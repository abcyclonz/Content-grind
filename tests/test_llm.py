import json
import subprocess

import pytest

from network.providers import llm as llm_mod
from network.providers.llm import ClaudeCLIProvider, LLMError

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


class FakeRunner:
    def __init__(self, stdout="", returncode=0, stderr="", exc=None):
        self.stdout, self.returncode, self.stderr, self.exc = stdout, returncode, stderr, exc
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def _provider(tmp_path, runner):
    return ClaudeCLIProvider(tmp_path / "wd", exe="claude-test", runner=runner)


def _call(p):
    return p.complete_json(model="claude-haiku-5-5", system="sys\nline 2", prompt="hello", schema=SCHEMA)


def test_success_uses_structured_output_and_cost(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-pass-through")
    out = {"is_error": False, "result": '{"ok":true}', "structured_output": {"ok": True},
           "total_cost_usd": 0.0012, "usage": {"output_tokens": 40}}
    runner = FakeRunner(stdout=json.dumps(out))
    r = _call(_provider(tmp_path, runner))
    assert r.data == {"ok": True} and r.est_usd == pytest.approx(0.0012)

    cmd, kw = runner.calls[0]
    assert cmd[0] == "claude-test" and "-p" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-5-5"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--system-prompt") + 1] == "sys\nline 2"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == SCHEMA
    assert kw["input"] == "hello"
    assert kw["cwd"] == tmp_path / "wd" and (tmp_path / "wd").is_dir()
    assert "ANTHROPIC_API_KEY" not in kw["env"]


def test_falls_back_to_result_text(tmp_path):
    runner = FakeRunner(stdout=json.dumps({"is_error": False, "result": '{"ok": false}'}))
    assert _call(_provider(tmp_path, runner)).data == {"ok": False}


@pytest.mark.parametrize(
    "runner, match",
    [
        (FakeRunner(stdout=json.dumps({"is_error": True, "result": "usage limit reached"}), returncode=1),
         "usage limit"),
        (FakeRunner(stdout="not json", stderr="boom", returncode=1), "no JSON output: boom"),
        (FakeRunner(stdout=json.dumps({"is_error": False, "structured_output": {"other": 1}})),
         "missing required"),
        (FakeRunner(stdout=json.dumps({"is_error": False, "result": "plain words"})),
         "no structured output"),
        (FakeRunner(exc=subprocess.TimeoutExpired("claude", 5)), "timed out"),
        (FakeRunner(exc=FileNotFoundError("nope")), "could not start"),
    ],
)
def test_errors_become_llm_errors(tmp_path, runner, match):
    with pytest.raises(LLMError, match=match):
        _call(_provider(tmp_path, runner))


def test_find_exe_resolves_windows_shim(tmp_path, monkeypatch):
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off")
    real = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    real.parent.mkdir(parents=True)
    real.write_text("")
    monkeypatch.setenv("CLAUDE_CLI_PATH", str(shim))
    assert llm_mod.find_claude_exe() == str(real)


def test_find_exe_plain_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_PATH", str(tmp_path / "claude"))
    assert llm_mod.find_claude_exe() == str(tmp_path / "claude")


def test_find_exe_missing(monkeypatch):
    monkeypatch.delenv("CLAUDE_CLI_PATH", raising=False)
    monkeypatch.setattr(llm_mod.shutil, "which", lambda name: None)
    with pytest.raises(LLMError, match="not found"):
        llm_mod.find_claude_exe()

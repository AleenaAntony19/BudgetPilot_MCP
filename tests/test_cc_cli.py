"""bp install / uninstall / doctor / mode / transcript-check."""

import json
import os
import stat

import pytest

from budgetpilot import cc_cli, installer
from budgetpilot.cli import main
from budgetpilot.core import state
from tests.hookutil import TRANSCRIPT


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    return root


@pytest.fixture
def no_claude(monkeypatch):
    monkeypatch.setattr(cc_cli, "find_claude", lambda: None)


def test_install_merges_hooks_and_uninstall_removes_exactly_them(repo, no_claude, capsys):
    settings = repo / ".claude" / "settings.local.json"
    original = {"permissions": {"allow": ["Bash(ls *)"]},
                "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]}]}}
    settings.write_text(json.dumps(original))

    assert main(["install", "--scope", "project", "--repo", str(repo), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Hooks registered in" in out and "`claude` command was not found" in out and '"mcpServers"' in out
    installed = json.loads(settings.read_text())
    assert installed["permissions"] == original["permissions"]
    assert set(installer.count_hooks(installed, installer.HOOK_MODULE)) == {
        "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd"}
    assert len(list(repo.glob(".claude/settings.local.json.bp-backup-*"))) == 1

    assert main(["install", "--scope", "project", "--repo", str(repo), "--yes", "--hooks"]) == 0  # idempotent
    again = json.loads(settings.read_text())
    assert again == installed

    capsys.readouterr()
    assert main(["uninstall"]) == 0
    assert json.loads(settings.read_text()) == original
    assert "Removed 7 hook entries" in capsys.readouterr().out
    assert main(["uninstall"]) == 0
    assert "Nothing to remove" in capsys.readouterr().out


def test_install_needs_confirmation_when_not_interactive(repo, no_claude, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["install", "--scope", "project", "--repo", str(repo)]) == 1
    assert not (repo / ".claude" / "settings.local.json").exists()


def test_install_registers_mcp_through_the_claude_cli(repo, monkeypatch, tmp_path):
    calls = tmp_path / "calls.txt"
    fake = tmp_path / "claude"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {calls}\nexit 0\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(cc_cli, "find_claude", lambda: str(fake))
    assert cc_cli.run_install(scope="project", repo=str(repo), mcp=True, yes=True, out=lambda _: None) == 0
    lines = calls.read_text().splitlines()
    assert lines[0] == "mcp remove --scope project budgetpilot"
    assert lines[1].startswith("mcp add --scope project budgetpilot -e BP_HOME=")
    assert lines[1].endswith(f"-m budgetpilot.adapters.mcp_server --repo {repo}")
    cc_cli.run_uninstall(out=lambda _: None)
    assert calls.read_text().splitlines()[-1] == "mcp remove --scope project budgetpilot"


def test_claude_md_block_is_idempotent_and_removable(repo, no_claude):
    path = repo / "CLAUDE.md"
    path.write_text("# Project notes\n\nKeep me.\n")
    for _ in range(2):
        cc_cli.run_install(scope="project", repo=str(repo), claude_md=True, yes=True, out=lambda _: None)
    text = path.read_text()
    assert text.count(cc_cli.CLAUDE_MD_BEGIN) == 1 and "bp_mode_on" in text
    assert len(cc_cli.CLAUDE_MD_BLOCK.strip().splitlines()) <= 10
    cc_cli.run_uninstall(out=lambda _: None)
    assert path.read_text() == "# Project notes\n\nKeep me.\n"


def test_mode_command_shares_state_with_hooks(capsys, monkeypatch):
    monkeypatch.delenv("BP_MODE", raising=False)
    assert main(["mode", "on"]) == 0
    assert state.get_mode() == "enforce" and capsys.readouterr().out.startswith("BudgetPilot ON")
    assert main(["mode", "off"]) == 0 and state.get_mode() == "off"
    assert main(["budget", "tight"]) == 0 and state.get_budget_slider() == "tight"


def test_transcript_check(capsys):
    assert main(["transcript-check", str(TRANSCRIPT)]) == 0
    out = capsys.readouterr().out
    assert "unique messages 12 (up to 3 rows per message)" in out and "output 2,003" in out
    assert main(["transcript-check", str(TRANSCRIPT), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["cache_read"] == 465598
    assert main(["transcript-check", "/nonexistent.jsonl"]) == 1


def test_measure_hooks_runs_the_real_process_in_isolation():
    home = state.get_bp_home()
    before = sorted(p.name for p in home.iterdir())
    result = cc_cli.measure_hooks(runs=1)
    assert result["ok"], result["errors"]
    assert result["read_rewrite_limit"] in (400, 800)
    assert {"UserPromptSubmit", "PreToolUse Read", "Stop", "mode off", "python start-up alone"} <= set(result["timings"])
    assert all(t["p50"] > 0 for t in result["timings"].values())
    assert sorted(p.name for p in home.iterdir()) == before  # the user's home was not touched


def test_doctor_reports_api_key_and_unregistered_hooks(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cc_cli, "find_claude", lambda: None)
    monkeypatch.setattr(cc_cli, "measure_hooks", lambda runs: {"ok": True, "errors": [], "read_rewrite_limit": 400,
                                                               "timings": {"x": {"p50": 1.0, "p95": 2.0, "n": 1}}})
    monkeypatch.setattr(cc_cli, "mcp_handshake", lambda: {"ok": True, "tools": ["a"], "schema_tokens_est": 10,
                                                          "longest_description_words": 5})
    assert cc_cli.run_doctor(claude=True, runs=1) == 1
    out = capsys.readouterr().out
    assert "[WARN] ANTHROPIC_API_KEY is set" in out and "fix: unset ANTHROPIC_API_KEY" in out
    assert "[FAIL] claude executable" in out and "[FAIL] Hooks registered" in out and "fix: bp install --yes" in out
    assert "[PASS] Database writable" in out and "p50" in out


def test_find_claude_prefers_explicit_binary(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho 9.9.9\n")
    fake.chmod(0o755)
    monkeypatch.setenv("BP_CLAUDE_BIN", str(fake))
    assert cc_cli.find_claude() == str(fake)
    assert cc_cli.claude_version(str(fake)) == "9.9.9"

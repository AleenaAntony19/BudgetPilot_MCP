"""MCP control tools, the human recovery choice (3E loop) and a real stdio handshake."""

import asyncio
import json

import pytest

from budgetpilot import cc_cli
from budgetpilot.adapters.mcp_server import create_mcp_server
from budgetpilot.core import state
from tests.hookutil import db, fixture, make_repo, pre, run_hook, start_task, task_row


@pytest.fixture
def server():
    return create_mcp_server("TestBudgetPilot")


def call(server, name, args=None):
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(server.call_tool(name, args or {}))
    finally:
        loop.close()
    text = result.content[0].text
    data = json.loads(text)
    assert text == json.dumps(data, separators=(",", ":"), ensure_ascii=False)  # compact, not pretty-printed
    return data, result


def test_tool_surface_is_minimal_and_descriptions_are_short(server):
    loop = asyncio.new_event_loop()
    try:
        tools = loop.run_until_complete(server.list_tools())
    finally:
        loop.close()
    assert {t.name for t in tools} == cc_cli.EXPECTED_MCP_TOOLS
    assert "bp_escalate" not in {t.name for t in tools}
    for tool in tools:
        assert 0 < len(tool.description.split()) <= 25, tool.name


def test_bp_mode_and_budget_share_state_with_hooks(server, monkeypatch):
    monkeypatch.delenv("BP_MODE", raising=False)
    data, _ = call(server, "bp_mode", {"mode": "on"})
    assert data["mode"] == "enforce" and data["message"].startswith("BudgetPilot ON")
    assert state.get_mode() == "enforce"
    assert call(server, "bp_mode", {"mode": "observe"})[0]["mode"] == "observe"
    assert "unknown mode" in call(server, "bp_mode", {"mode": "turbo"})[0]["message"]
    assert call(server, "bp_set_budget", {"budget_slider": "tight"})[0]["budget"] == "tight"
    assert state.get_budget_slider() == "tight"
    assert call(server, "bp_set_budget", {"budget_slider": "huge"})[0]["budget"] == "tight"
    assert call(server, "bp_mode", {"mode": "off"})[0]["mode"] == "off"


def test_bp_status_is_compact(server, monkeypatch):
    monkeypatch.delenv("BP_MODE", raising=False)
    data, result = call(server, "bp_status")
    assert data["mode"] == "off" and data["budget"] == "balanced"
    assert len(result.content[0].text.encode()) / 3.8 <= 120
    conn = db(state.get_bp_home())
    assert conn.execute("SELECT kind FROM connections WHERE name='mcp'").fetchone()[0] == "mcp"  # heartbeat


def test_bp_request_scope_uses_the_scope_table(server):
    data, _ = call(server, "bp_request_scope", {"reason": "need more files", "session_id": "s"})
    assert data == {"approved": True, "previous_scope": 1, "granted_scope": 2, "max_read_files": 8}


def test_stdio_handshake_with_the_installed_mcp_client():
    result = cc_cli.mcp_handshake()
    assert result["ok"], result
    assert result["schema_tokens_est"] < 1500 and result["longest_description_words"] <= 25


# ------------------------------------------------------------------ human-in-the-loop recovery
@pytest.fixture
def failing_task(tmp_path, monkeypatch):
    """A task with a failed test run waiting for the human's decision."""
    repo = make_repo(tmp_path / "repo")
    home = state.get_bp_home()          # the MCP server (this process) and the hooks share BP_HOME
    monkeypatch.delenv("BP_MODE", raising=False)
    start_task(repo, home, "Fix the failing addition test.", hitl="1")
    run_hook(fixture("PreToolUse__Edit", repo), home, hitl="1")          # snapshot of calc.py before the edit
    (repo / "pkg" / "calc.py").write_text("def add(a, b):\n    return a * b  # wrong fix\n")
    run_hook(fixture("PostToolUse__Edit", repo), home, hitl="1")
    run_hook(fixture("PostToolUseFailure__Bash__pytest_fail", repo), home, hitl="1")
    assert task_row(home)["pending_recovery"]
    return repo, home


def test_options_then_execute(server, failing_task):
    repo, home = failing_task
    options, _ = call(server, "bp_recovery_options")
    assert options["pending"] is True and len(options["signature"]) == 8
    assert options["recommended"] in ("estimate", "execute", "expand", "escalate")
    assert set(options["choices"]) == {"estimate", "execute", "expand", "escalate", "retry", "deny", "mode_off"}

    data, result = call(server, "bp_recovery_choose", {"action": "execute", "note": "looks local"})
    assert not result.is_error and data["action"] == "execute" and data["directive"].startswith("BudgetPilot [Execute]")
    assert len(data["directive"].encode()) / 3.8 <= 150
    event = db(home).execute("SELECT chosen, decided_by FROM recovery_events").fetchone()
    assert tuple(event) == ("reflect", "human")
    assert task_row(home)["pending_recovery"] is None
    assert call(server, "bp_recovery_options")[0] == {"pending": False}
    assert run_hook(fixture("PreToolUse__Edit", repo, tool_use_id="next"), home, hitl="1")[1] == ""  # no more ask


def test_estimate_restores_the_pre_task_snapshot_only_when_chosen(server, failing_task):
    repo, home = failing_task
    assert "wrong fix" in (repo / "pkg" / "calc.py").read_text()      # nothing restored silently
    data, _ = call(server, "bp_recovery_choose", {"action": "replan"})  # router name accepted too
    assert data["action"] == "estimate" and "pkg/calc.py" in data["directive"]
    assert (repo / "pkg" / "calc.py").read_text() == "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"


def test_expand_raises_scope_and_budgets(server, failing_task):
    repo, home = failing_task
    before = task_row(home)
    data, _ = call(server, "bp_recovery_choose", {"action": "expand"})
    after = task_row(home)
    assert after["scope"] == min(3, before["scope"] + 1) and after["ctx_budget"] >= before["ctx_budget"]
    assert data["directive"].startswith("BudgetPilot [Expand]: scope")


def test_escalate_is_advisory_only(server, failing_task):
    data, _ = call(server, "bp_recovery_choose", {"action": "escalate"})
    assert "cannot switch models" in data["directive"] and "/model" in data["directive"]


def test_deny_and_retry_and_mode_off(server, failing_task):
    repo, home = failing_task
    data, _ = call(server, "bp_recovery_choose", {"action": "deny"})
    assert "declined" in data["directive"] and task_row(home)["state"]["recovery_declined"] is True
    # a later failure in the same task is recorded but no longer managed
    again = fixture("PostToolUseFailure__Bash__pytest_fail", repo, tool_use_id="f2")
    assert run_hook(again, home, hitl="1")[1] == ""
    assert task_row(home)["pending_recovery"] is None
    assert call(server, "bp_recovery_choose", {"action": "retry"})[1].is_error       # nothing pending now
    data, _ = call(server, "bp_recovery_choose", {"action": "mode_off"})
    assert data["action"] == "mode_off" and state.get_mode() == "off"


def test_unknown_choice_is_rejected(server, failing_task):
    data, result = call(server, "bp_recovery_choose", {"action": "explode"})
    assert result.is_error and "unknown action" in data["error"]
    assert task_row(failing_task[1])["pending_recovery"]


def test_resolution_is_remembered_for_the_next_session(server, failing_task):
    repo, home = failing_task
    call(server, "bp_recovery_choose", {"action": "execute"})
    run_hook(fixture("PostToolUse__Bash__pytest_target", repo), home, hitl="1")       # tests pass now
    conn = db(home)
    assert json.loads(conn.execute("SELECT resolved_by FROM failure_fingerprints").fetchone()[0]) == {"reflect": 1}
    assert conn.execute("SELECT outcome FROM recovery_events").fetchone()[0] == "resolved"
    # a new session hits the same failure: the options mention what worked before
    start_task(repo, home, "Fix it again.", hitl="1", prompt_id="later")
    failure = fixture("PostToolUseFailure__Bash__pytest_fail", repo, prompt_id="later", session_id="another-session")
    text = run_hook(failure, home, hitl="1")[2]["hookSpecificOutput"]["additionalContext"]
    assert "Seen 2x in this repo; previously resolved by Execute (1x)." in text

"""
Unit and integration tests for BudgetPilot Execute Gate (Phase E).
Tests cover:
1. Policy Enforcement Matrix:
   - Scope 1: Max 3 file reads outside named set, 400-line read window clamping, depth 1 list_dir, deny full-suite tests.
   - Scope 2: Max 10 file reads, 800-line read window clamping, package directory search/list.
   - Scope 3: Unrestricted operations.
   - Edits: Always allowed across all scope levels.
2. Safety & Overrides:
   - First read of file named directly in prompt or traceback is never gated.
   - Repeating a denied call within 3 turns is allowed and logged as an override.
   - Switching to observe mode when max_denies_per_task is reached.
3. Execution Modes:
   - enforce, observe (log without blocking), off.
4. Fail-open handling on unexpected gate exceptions.
5. request_scope tool in agent/tools.py.
"""

from pathlib import Path
import pytest

from budgetpilot.agent.tools import execute_tool, request_scope
from budgetpilot.core.gate import ExecuteGate, check as gate_check
from budgetpilot.core.tracker import TaskState, TaskStatus, TaskTracker
from budgetpilot.core.types import BpEvent, GateVerdict, GateVerdictStatus


def test_edits_always_allowed_all_scopes():
    """Verify edit_file is always allowed across Scope 1, 2, and 3."""
    for sc in (1, 2, 3):
        state = TaskState(session_id="sess_edit")
        state.metadata["scope"] = sc
        state.metadata["mode"] = "enforce"

        ev = BpEvent(
            event_type="edit_file",
            payload={"tool_name": "edit_file", "arguments": {"path": "src/core.py", "old": "a", "new": "b"}},
        )
        verdict = gate_check(ev, state)
        assert verdict.status == GateVerdictStatus.ALLOW
        assert "edits are always allowed" in verdict.reason.lower()


def test_scope_3_unrestricted():
    """Verify Scope 3 permits all operations unconditionally."""
    state = TaskState(session_id="sess_s3")
    state.metadata["scope"] = 3
    state.metadata["mode"] = "enforce"

    # Deep list_dir
    ev1 = BpEvent(event_type="list_dir", payload={"arguments": {"path": ".", "depth": 5}})
    v1 = gate_check(ev1, state)
    assert v1.status == GateVerdictStatus.ALLOW

    # Large read window
    ev2 = BpEvent(event_type="read_file", payload={"arguments": {"path": "large.py", "start": 1, "end": 5000}})
    v2 = gate_check(ev2, state)
    assert v2.status == GateVerdictStatus.ALLOW

    # Full suite test run
    ev3 = BpEvent(event_type="run_tests", payload={"arguments": {"command": "pytest"}})
    v3 = gate_check(ev3, state)
    assert v3.status == GateVerdictStatus.ALLOW


def test_scope_1_named_file_never_gated():
    """Verify first read of a file named in prompt or traceback is never gated."""
    state = TaskState(session_id="sess_named")
    state.metadata["scope"] = 1
    state.metadata["mode"] = "enforce"
    state.metadata["prompt"] = "Fix the assertion in calc.py line 40"

    # Even with a huge read window, first read of named file is immediately allowed
    ev = BpEvent(
        event_type="read_file",
        payload={"arguments": {"path": "calc.py", "start": 1, "end": 1500}},
    )
    v = gate_check(ev, state)
    assert v.status == GateVerdictStatus.ALLOW
    assert "first read of file named directly" in v.reason.lower()


def test_scope_1_max_3_unnamed_reads():
    """Verify Scope 1 allows at most 3 file reads outside the named set."""
    state = TaskState(session_id="sess_unnamed")
    state.metadata["scope"] = 1
    state.metadata["mode"] = "enforce"
    state.metadata["prompt"] = "Fix bug in main.py"  # main.py is named

    # Reads 1, 2, 3 outside named set
    state.read_set.add("f1.py")
    state.read_set.add("f2.py")
    state.read_set.add("f3.py")

    # 4th read of a new unnamed file -> DENY
    ev_deny = BpEvent(
        event_type="read_file",
        payload={"arguments": {"path": "f4.py", "start": 1, "end": 50}},
    )
    v_deny = gate_check(ev_deny, state)
    assert v_deny.status == GateVerdictStatus.DENY
    assert "exceeded maximum 3 file reads outside named set" in v_deny.reason.lower()

    # Re-reading an already read file -> ALLOW
    ev_repeat = BpEvent(
        event_type="read_file",
        payload={"arguments": {"path": "f1.py", "start": 1, "end": 50}},
    )
    v_repeat = gate_check(ev_repeat, state)
    assert v_repeat.status == GateVerdictStatus.ALLOW


def test_scope_1_read_window_clamping():
    """Verify Scope 1 clamps read windows exceeding 400 lines via REWRITE."""
    state = TaskState(session_id="sess_clamp1")
    state.metadata["scope"] = 1
    state.metadata["mode"] = "enforce"

    ev = BpEvent(
        event_type="read_file",
        payload={"arguments": {"path": "service.py", "start": 100, "end": 700}},
    )
    v = gate_check(ev, state)
    assert v.status == GateVerdictStatus.REWRITE
    assert v.rewritten_call is not None
    assert v.rewritten_call.arguments["start"] == 100
    assert v.rewritten_call.arguments["end"] == 499  # 100 + 399 = 400 lines
    assert "clamped to 400 lines" in v.reason.lower()


def test_scope_1_list_dir_depth():
    """Verify Scope 1 clamps list_dir depth > 1 to depth 1 via REWRITE."""
    state = TaskState(session_id="sess_depth1")
    state.metadata["scope"] = 1
    state.metadata["mode"] = "enforce"

    # Depth 3 rewritten to depth 1
    ev_deep = BpEvent(
        event_type="list_dir",
        payload={"arguments": {"path": ".", "depth": 3}},
    )
    v_deep = gate_check(ev_deep, state)
    assert v_deep.status == GateVerdictStatus.REWRITE
    assert v_deep.rewritten_call.arguments["depth"] == 1

    # Depth 1 allowed as-is
    ev_ok = BpEvent(
        event_type="list_dir",
        payload={"arguments": {"path": ".", "depth": 1}},
    )
    v_ok = gate_check(ev_ok, state)
    assert v_ok.status == GateVerdictStatus.ALLOW


def test_scope_1_deny_full_suite_tests():
    """Verify Scope 1 denies full-suite test runs and suggests targeted path."""
    state = TaskState(session_id="sess_test1")
    state.metadata["scope"] = 1
    state.metadata["mode"] = "enforce"

    # Full suite: no target specified
    ev_full = BpEvent(
        event_type="run_tests",
        payload={"arguments": {"command": "pytest"}},
    )
    v_full = gate_check(ev_full, state)
    assert v_full.status == GateVerdictStatus.DENY
    assert "full-suite test runs denied in scope 1" in v_full.reason.lower()
    assert "suggest targeted path" in v_full.reason.lower()

    # Targeted test run: allowed
    ev_target = BpEvent(
        event_type="run_tests",
        payload={"arguments": {"command": "pytest", "target": "tests/test_foo.py"}},
    )
    v_target = gate_check(ev_target, state)
    assert v_target.status == GateVerdictStatus.ALLOW


def test_scope_2_max_10_file_reads():
    """Verify Scope 2 allows at most 10 distinct file reads."""
    state = TaskState(session_id="sess_s2_reads")
    state.metadata["scope"] = 2
    state.metadata["mode"] = "enforce"

    for i in range(10):
        state.read_set.add(f"mod_{i}.py")

    # 11th distinct file -> DENY
    ev_deny = BpEvent(
        event_type="read_file",
        payload={"arguments": {"path": "mod_10.py", "start": 1, "end": 100}},
    )
    v_deny = gate_check(ev_deny, state)
    assert v_deny.status == GateVerdictStatus.DENY
    assert "exceeded maximum 10 file reads for scope 2" in v_deny.reason.lower()


def test_scope_2_read_window_clamping():
    """Verify Scope 2 clamps read windows exceeding 800 lines via REWRITE."""
    state = TaskState(session_id="sess_clamp2")
    state.metadata["scope"] = 2
    state.metadata["mode"] = "enforce"

    ev = BpEvent(
        event_type="read_file",
        payload={"arguments": {"path": "lib.py", "start": 50, "end": 1050}},
    )
    v = gate_check(ev, state)
    assert v.status == GateVerdictStatus.REWRITE
    assert v.rewritten_call.arguments["start"] == 50
    assert v.rewritten_call.arguments["end"] == 849  # 50 + 799 = 800 lines
    assert "clamped to 800 lines" in v.reason.lower()


def test_scope_2_package_directory_search_and_list():
    """Verify Scope 2 allows package directory search/list while restricting root search."""
    state = TaskState(session_id="sess_pkg_search")
    state.metadata["scope"] = 2
    state.metadata["mode"] = "enforce"

    # Targeted package search -> ALLOW
    ev_pkg = BpEvent(
        event_type="search",
        payload={"arguments": {"pattern": "UserService", "path": "budgetpilot"}},
    )
    v_pkg = gate_check(ev_pkg, state)
    assert v_pkg.status == GateVerdictStatus.ALLOW

    # Root broad search -> DENY with suggestion to target package directory
    ev_root = BpEvent(
        event_type="search",
        payload={"arguments": {"pattern": "UserService", "path": "."}},
    )
    v_root = gate_check(ev_root, state)
    assert v_root.status == GateVerdictStatus.DENY
    assert "specify a package directory" in v_root.reason.lower()


def test_safety_override_within_3_turns():
    """Verify repeating a denied call within 3 turns is allowed as an override."""
    state = TaskState(session_id="sess_override")
    state.metadata["scope"] = 1
    state.metadata["mode"] = "enforce"
    state.metadata["turn"] = 2

    ev = BpEvent(
        event_type="run_tests",
        payload={"tool_name": "run_tests", "arguments": {"command": "pytest"}},
    )

    # 1. First attempt: DENY
    v1 = gate_check(ev, state)
    assert v1.status == GateVerdictStatus.DENY
    assert "override_key" in v1.metadata

    # 2. Second attempt in Turn 4 (2 turns later, <= 3 turns delta) -> ALLOW (Override)
    state.metadata["turn"] = 4
    v2 = gate_check(ev, state)
    assert v2.status == GateVerdictStatus.ALLOW
    assert v2.metadata.get("override") is True
    assert "repeated denied call within 3 turns" in v2.reason.lower()

    # Verify override was recorded in metadata
    assert len(state.metadata["overrides"]) == 1


def test_safety_override_expired_after_3_turns():
    """Verify repeating a denied call more than 3 turns later is NOT overridden."""
    state = TaskState(session_id="sess_override_exp")
    state.metadata["scope"] = 1
    state.metadata["mode"] = "enforce"
    state.metadata["turn"] = 1

    ev = BpEvent(
        event_type="run_tests",
        payload={"tool_name": "run_tests", "arguments": {"command": "pytest"}},
    )

    # Denied at turn 1
    v1 = gate_check(ev, state)
    assert v1.status == GateVerdictStatus.DENY

    # Repeated at turn 5 (4 turns delta > 3) -> Not overridden, policy denies again
    state.metadata["turn"] = 5
    v2 = gate_check(ev, state)
    assert v2.status == GateVerdictStatus.DENY


def test_max_denies_per_task_switches_to_observe_mode():
    """Verify exceeding max_denies_per_task switches task state mode to observe."""
    state = TaskState(session_id="sess_max_denies")
    state.metadata["scope"] = 1
    state.metadata["mode"] = "enforce"
    state.metadata["max_denies_per_task"] = 2

    # Deny 1
    ev1 = BpEvent(event_type="search", payload={"arguments": {"pattern": "p1", "path": "."}})
    v1 = gate_check(ev1, state)
    assert v1.status == GateVerdictStatus.DENY

    # Deny 2 (hits threshold 2) -> mode switches to observe
    ev2 = BpEvent(event_type="search", payload={"arguments": {"pattern": "p2", "path": "."}})
    v2 = gate_check(ev2, state)
    assert v2.status == GateVerdictStatus.DENY
    assert state.metadata["mode"] == "observe"

    # Call 3 is now evaluated in observe mode -> ALLOW without blocking
    ev3 = BpEvent(event_type="search", payload={"arguments": {"pattern": "p3", "path": "."}})
    v3 = gate_check(ev3, state)
    assert v3.status == GateVerdictStatus.ALLOW
    assert "[observe mode]" in v3.reason.lower()


def test_execution_modes_enforce_observe_off():
    """Verify behavior of enforce, observe, and off execution modes."""
    ev = BpEvent(
        event_type="run_tests",
        payload={"arguments": {"command": "pytest"}},
    )

    # 1. enforce mode -> DENY
    s_enforce = TaskState(session_id="s1")
    s_enforce.metadata["mode"] = "enforce"
    v_enf = gate_check(ev, s_enforce)
    assert v_enf.status == GateVerdictStatus.DENY

    # 2. observe mode -> ALLOW with observed status logged
    s_observe = TaskState(session_id="s2")
    s_observe.metadata["mode"] = "observe"
    v_obs = gate_check(ev, s_observe)
    assert v_obs.status == GateVerdictStatus.ALLOW
    assert "[observe mode]" in v_obs.reason.lower()
    assert v_obs.metadata.get("observed_status") == "deny"

    # 3. off mode -> immediate ALLOW
    s_off = TaskState(session_id="s3")
    s_off.metadata["mode"] = "off"
    v_off = gate_check(ev, s_off)
    assert v_off.status == GateVerdictStatus.ALLOW
    assert "gate is off" in v_off.reason.lower()


def test_gate_fail_open_resilience(monkeypatch):
    """Verify gate returns ALLOW if an unexpected exception occurs inside gate check."""
    state = TaskState(session_id="sess_err")
    ev = BpEvent(event_type="read_file", payload={"arguments": {"path": "foo.py"}})

    # Simulate unexpected bug in extract_named_files
    def broken_extract(s):
        raise RuntimeError("Simulated internal gate failure")

    monkeypatch.setattr("budgetpilot.core.gate.extract_named_files", broken_extract)

    v = gate_check(ev, state)
    assert v.status == GateVerdictStatus.ALLOW
    assert v.metadata.get("fail_open") is True
    assert "fail-open" in v.reason.lower()


def test_request_scope_tool_and_tracker_integration():
    """Verify request_scope tool function, execution dispatcher, and tracker state integration."""
    out, ok, meta = request_scope(reason="Need to inspect multi-file components")
    assert ok is True
    assert "Scope expansion requested" in out
    assert meta["tool"] == "request_scope"
    assert meta["scope_increment"] == 1

    # Test execution via dispatcher
    out_disp, ok_disp, meta_disp = execute_tool(
        name="request_scope",
        arguments={"reason": "Need broader refactor"},
    )
    assert ok_disp is True
    assert "Scope expansion requested" in out_disp

    # Test tracker integration
    tracker = TaskTracker(session_id="sess_req_scope")
    assert tracker.state.metadata.get("current_scope", 1) == 1

    tracker.record_tool_call("request_scope", {"reason": "Scope 1 insufficient"})
    assert tracker.state.metadata["current_scope"] == 2

    tracker.record_tool_call("request_scope", {"reason": "Scope 2 insufficient"})
    assert tracker.state.metadata["current_scope"] == 3

    # Capped at 3
    tracker.record_tool_call("request_scope", {"reason": "Already at max"})
    assert tracker.state.metadata["current_scope"] == 3

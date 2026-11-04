"""
Unit and integration tests for BudgetPilot Ultra-Low Latency Hooks Adapter (Phase N).
Tests:
- Standard-library-only pure Python AST invariant (zero 3rd-party imports)
- Claude Code tool and parameter canonical mapping
- Payload conversion to BpEvent
- Task lifecycle triggers (TaskStart, TaskEnd)
- PreToolUse gate policy checks (rewrite, deny, allow)
- PostToolUse telemetry recording
- Execution latency benchmark (<60ms invariant)
- CLI stdin/stdout execution interface
"""

import ast
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import pytest

from budgetpilot.adapters.hooks import (
    CLAUDE_TOOL_MAP,
    handle_hook_event,
    handle_lifecycle_trigger,
    map_claude_tool_to_canonical,
    measure_interception_latency,
    payload_to_bp_event,
)
from budgetpilot.core.types import BpEvent


# =====================================================================
# 1. Architectural Invariant: Pure Standard Library
# =====================================================================

def test_hooks_is_pure_standard_library():
    """
    Verify that budgetpilot/adapters/hooks.py imports ONLY standard library modules.
    Must NOT import ollama, mcp, fastapi, uvicorn, requests, pydantic, or any external package.
    """
    hooks_file = Path(__file__).resolve().parent.parent / "budgetpilot" / "adapters" / "hooks.py"
    assert hooks_file.exists()

    tree = ast.parse(hooks_file.read_text(), filename=str(hooks_file))

    # Allowed packages: standard library + internal budgetpilot.core.types (which is also pure stdlib)
    allowed_top_modules = {
        "ast", "asyncio", "dataclasses", "enum", "io", "json", "logging", "math",
        "os", "pathlib", "random", "re", "shutil", "sqlite3", "subprocess", "sys",
        "time", "typing", "unittest", "uuid", "budgetpilot"
    }

    forbidden = {"ollama", "mcp", "fastapi", "uvicorn", "pydantic", "requests", "httpx"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_pkg = alias.name.split(".")[0]
                assert top_pkg not in forbidden, f"Forbidden import '{top_pkg}' in hooks.py"
                assert top_pkg in allowed_top_modules, f"External library '{top_pkg}' not permitted in stdlib shim"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_pkg = node.module.split(".")[0]
                assert top_pkg not in forbidden, f"Forbidden import-from '{top_pkg}' in hooks.py"
                assert top_pkg in allowed_top_modules, f"External library '{top_pkg}' not permitted in stdlib shim"


# =====================================================================
# 2. Tool & Parameter Canonical Mapping
# =====================================================================

def test_map_claude_tool_view_to_read_file():
    """Verify mapping Claude Code 'View' tool to canonical read_file."""
    canon, args = map_claude_tool_to_canonical(
        "View",
        {"file_path": "budgetpilot/core/gate.py", "view_range": [10, 50]}
    )
    assert canon == "read_file"
    assert args["path"] == "budgetpilot/core/gate.py"
    assert args["start"] == 10
    assert args["end"] == 50


def test_map_claude_tool_edit_to_edit_file():
    """Verify mapping Claude Code 'FileEdit' tool to canonical edit_file."""
    canon, args = map_claude_tool_to_canonical(
        "FileEdit",
        {
            "file_path": "main.py",
            "old_string": "def foo(): pass",
            "new_string": "def foo(): return 42",
        }
    )
    assert canon == "edit_file"
    assert args["path"] == "main.py"
    assert args["old"] == "def foo(): pass"
    assert args["new"] == "def foo(): return 42"


def test_map_claude_tool_ls_to_list_dir():
    """Verify mapping Claude Code 'LS' tool to canonical list_dir."""
    canon, args = map_claude_tool_to_canonical(
        "LS",
        {"path": "budgetpilot", "depth": 2}
    )
    assert canon == "list_dir"
    assert args["path"] == "budgetpilot"
    assert args["depth"] == 2


def test_map_claude_tool_grep_to_search():
    """Verify mapping Claude Code 'Grep' tool to canonical search."""
    canon, args = map_claude_tool_to_canonical(
        "Grep",
        {"pattern": "def check_policy", "path": "budgetpilot/core"}
    )
    assert canon == "search"
    assert args["pattern"] == "def check_policy"
    assert args["path"] == "budgetpilot/core"


def test_map_claude_tool_bash_test_command():
    """Verify mapping Claude Code 'Bash' tool running pytest to run_tests."""
    canon, args = map_claude_tool_to_canonical(
        "Bash",
        {"command": "pytest tests/test_verifier.py -v"}
    )
    assert canon == "run_tests"
    assert "pytest" in args["command"]


# =====================================================================
# 3. Payload Conversion to BpEvent
# =====================================================================

def test_payload_to_bp_event():
    """Verify converting Claude Code JSON payload into BpEvent dataclass."""
    payload = {
        "event": "PreToolUse",
        "tool": "View",
        "parameters": {
            "file_path": "budgetpilot/agent/loop.py",
            "view_range": [1, 100],
        },
        "session_id": "claude_sess_abc",
        "timestamp": 1727330000.0,
    }

    event = payload_to_bp_event(payload)
    assert isinstance(event, BpEvent)
    assert event.event_type == "read_file"
    assert event.session_id == "claude_sess_abc"
    assert event.payload["tool_name"] == "read_file"
    assert event.payload["arguments"]["path"] == "budgetpilot/agent/loop.py"
    assert event.payload["arguments"]["start"] == 1
    assert event.payload["arguments"]["end"] == 100
    assert event.metadata["source"] == "claude_code_hook"


# =====================================================================
# 4. Task Lifecycle Triggers
# =====================================================================

def test_handle_lifecycle_trigger(tmp_path):
    """Verify TaskStart and TaskEnd update sessions and task_events tables."""
    db_file = tmp_path / "hooks.db"
    sess_id = "test_lifecycle_sess"

    # 1. TaskStart
    res_start = handle_lifecycle_trigger(
        event_name="TaskStart",
        payload={"session_id": sess_id, "prompt": "Implement Phase N"},
        db_path=db_file,
    )
    assert res_start["status"] == "initialized"
    assert res_start["session_id"] == sess_id

    # 2. TaskEnd
    res_end = handle_lifecycle_trigger(
        event_name="TaskEnd",
        payload={"session_id": sess_id, "status": "completed"},
        db_path=db_file,
    )
    assert res_end["status"] == "finalized"
    assert res_end["session_id"] == sess_id


# =====================================================================
# 5. PreToolUse Policy Checks (Rewrite, Deny, Allow)
# =====================================================================

def test_handle_hook_event_read_rewrite(tmp_path, monkeypatch):
    """Verify unbounded read_file is rewritten to 300-line window."""
    monkeypatch.setenv("BP_HOME", str(tmp_path))
    monkeypatch.setenv("BP_MODE", "enforce")

    payload = {
        "event": "PreToolUse",
        "tool": "View",
        "parameters": {"file_path": "big.py", "start": 1},  # unbounded
        "session_id": "sess_read_rewrite",
    }

    res = handle_hook_event(payload, db_path=tmp_path / "bp.db")
    assert res["verdict"] == "rewrite"
    assert res["decision"] == "rewrite"
    assert res["rewritten_parameters"]["start"] == 1
    assert res["rewritten_parameters"]["end"] == 301
    assert "Unbounded read rewritten" in res["reason"]


def test_handle_hook_event_empty_edit_deny(tmp_path, monkeypatch):
    """Verify edit_file with empty old_string is denied."""
    monkeypatch.setenv("BP_HOME", str(tmp_path))
    monkeypatch.setenv("BP_MODE", "enforce")

    payload = {
        "event": "PreToolUse",
        "tool": "FileEdit",
        "parameters": {"file_path": "calc.py", "old_string": "", "new_string": "x = 1"},
        "session_id": "sess_edit_deny",
    }

    res = handle_hook_event(payload, db_path=tmp_path / "bp.db")
    assert res["verdict"] == "deny"
    assert res["decision"] == "deny"
    assert "cannot be empty" in res["reason"]


def test_handle_hook_event_allowed_action(tmp_path, monkeypatch):
    """Verify normal bounded actions are allowed."""
    monkeypatch.setenv("BP_HOME", str(tmp_path))
    monkeypatch.setenv("BP_MODE", "enforce")

    payload = {
        "event": "PreToolUse",
        "tool": "Grep",
        "parameters": {"pattern": "def test_verifier", "path": "tests"},
        "session_id": "sess_allowed",
    }

    res = handle_hook_event(payload, db_path=tmp_path / "bp.db")
    assert res["verdict"] == "allow"
    assert res["decision"] == "allow"


def test_handle_hook_event_post_tool_use(tmp_path):
    """Verify PostToolUse logs execution result."""
    payload = {
        "event": "PostToolUse",
        "tool": "Grep",
        "parameters": {"pattern": "hello"},
        "result": "found 1 match",
        "duration": 0.015,
        "is_error": False,
        "session_id": "sess_post",
    }

    res = handle_hook_event(payload, db_path=tmp_path / "bp.db")
    assert res["verdict"] == "recorded"
    assert res["decision"] == "allow"
    assert res["tool"] == "search"


# =====================================================================
# 6. Ultra-Low Latency Benchmark (<60ms Invariant)
# =====================================================================

def test_interception_latency_under_60ms(tmp_path):
    """
    Enforce SLA invariant:
    Tool interception overhead must be strictly under 60 milliseconds.
    """
    payload = {
        "event": "PreToolUse",
        "tool": "View",
        "parameters": {"file_path": "core.py", "view_range": [1, 50]},
        "session_id": "bench_latency_sess",
    }

    avg_ms = measure_interception_latency(payload, iterations=50, db_path=tmp_path / "latency.db")
    # Python stdlib should easily achieve < 10ms; invariant requires < 60ms
    assert avg_ms < 60.0, f"Interception latency too high: {avg_ms:.2f}ms (target < 60ms)"


# =====================================================================
# 7. CLI / Stdin Mode Execution
# =====================================================================

def test_hooks_cli_stdin_stdout_execution(tmp_path):
    """Verify invoking python -m budgetpilot.adapters.hooks via stdin/stdout pipe."""
    payload = {
        "event": "PreToolUse",
        "tool": "View",
        "parameters": {"file_path": "sample.py", "view_range": [1, 50]},
        "session_id": "cli_pipe_sess",
    }

    p = subprocess.run(
        [sys.executable, "-m", "budgetpilot.adapters.hooks"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )

    assert p.returncode == 0
    out_json = json.loads(p.stdout.strip())
    assert out_json["verdict"] in ("allow", "rewrite", "deny")
    assert "latency_ms" in out_json

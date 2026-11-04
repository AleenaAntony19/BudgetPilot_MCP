"""
Unit and integration tests for BudgetPilot MCP 2.x Server Adapter (Phase M).
Tests control tools (bp_status, bp_set_budget, bp_request_scope, bp_escalate, bp_report)
and gate-enforced proxy tools (bp_list_dir, bp_search, bp_read_file, bp_edit_file, bp_run_tests).
"""

import asyncio
import json
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from budgetpilot.adapters.mcp_server import create_mcp_server
from budgetpilot.agent.escalation import EscalationResult
from budgetpilot.config import ExecutionMode, load_config
from budgetpilot.core.store import init_db, safe_execute_write


@pytest.fixture
def mcp_server():
    """Create a fresh MCP server instance for testing."""
    return create_mcp_server("TestBudgetPilot")


def test_mcp_server_tool_registration(mcp_server):
    """Verify that all 5 control tools and 5 proxy tools are registered."""
    loop = asyncio.new_event_loop()
    try:
        tools = loop.run_until_complete(mcp_server.list_tools())
    finally:
        loop.close()

    tool_names = [t.name for t in tools]

    # 5 Control Tools
    assert "bp_status" in tool_names
    assert "bp_set_budget" in tool_names
    assert "bp_request_scope" in tool_names
    assert "bp_escalate" in tool_names
    assert "bp_report" in tool_names

    # 5 Proxy Tools
    assert "bp_list_dir" in tool_names
    assert "bp_search" in tool_names
    assert "bp_read_file" in tool_names
    assert "bp_edit_file" in tool_names
    assert "bp_run_tests" in tool_names


def test_mcp_control_bp_status(mcp_server, tmp_path, monkeypatch):
    """Verify bp_status returns active mode, budget slider, CRC guarantees, and spend."""
    monkeypatch.setenv("BP_HOME", str(tmp_path))
    db_file = tmp_path / "bp.db"
    init_db(db_file)

    # Seed session
    safe_execute_write(
        conn_or_path=db_file,
        sql="INSERT INTO sessions (session_id, mode, status, total_cost) VALUES ('mcp_sess_1', 'enforce', 'active', 0.012);",
    )

    loop = asyncio.new_event_loop()
    try:
        call_res = loop.run_until_complete(mcp_server.call_tool("bp_status", {}))
    finally:
        loop.close()

    assert not call_res.is_error
    data = call_res.structured_content or json.loads(call_res.content[0].text)

    assert "active_mode" in data
    assert "active_budget_slider" in data
    assert "calibration_guarantees" in data
    assert "financials" in data
    assert "tight" in data["calibration_guarantees"]
    assert "balanced" in data["calibration_guarantees"]
    assert "loose" in data["calibration_guarantees"]
    assert data["financials"]["total_sessions"] >= 1


def test_mcp_control_bp_set_budget(mcp_server, tmp_path, monkeypatch):
    """Verify bp_set_budget updates slider and execution mode."""
    monkeypatch.setenv("BP_HOME", str(tmp_path))

    loop = asyncio.new_event_loop()
    try:
        call_res = loop.run_until_complete(
            mcp_server.call_tool("bp_set_budget", {"budget_slider": "tight", "mode": "enforce"})
        )
    finally:
        loop.close()

    assert not call_res.is_error
    data = call_res.structured_content or json.loads(call_res.content[0].text)
    assert data["success"] is True
    assert data["active_mode"] == "enforce"
    assert data["active_budget_slider"] == "tight"

    # Verify invalid parameter handling
    loop2 = asyncio.new_event_loop()
    try:
        err_res = loop2.run_until_complete(
            mcp_server.call_tool("bp_set_budget", {"budget_slider": "hyperdrive"})
        )
    finally:
        loop2.close()

    err_data = err_res.structured_content or json.loads(err_res.content[0].text)
    assert err_data["success"] is False
    assert "Invalid budget slider" in err_data["error"]


def test_mcp_control_bp_request_scope(mcp_server, tmp_path, monkeypatch):
    """Verify bp_request_scope grants and records scope elevation."""
    monkeypatch.setenv("BP_HOME", str(tmp_path))
    sess_id = "test_scope_mcp_sess"

    loop = asyncio.new_event_loop()
    try:
        call_res = loop.run_until_complete(
            mcp_server.call_tool("bp_request_scope", {
                "reason": "Need to edit second file for integration test",
                "session_id": sess_id,
            })
        )
    finally:
        loop.close()

    assert not call_res.is_error
    data = call_res.structured_content or json.loads(call_res.content[0].text)
    assert data["approved"] is True
    assert data["granted_scope"] == 2
    assert data["max_read_files"] == 15


@patch("budgetpilot.agent.escalation.run_escalation")
def test_mcp_control_bp_escalate(mock_run_esc, mcp_server):
    """Verify bp_escalate calls run_escalation and returns patch results."""
    mock_run_esc.return_value = EscalationResult(
        session_id="esc_mcp_sess",
        success=True,
        patch="--- a/utils.py\n+++ b/utils.py\n@@ -1 +1 @@\n-old\n+new",
        tokens_used=1200,
        cost_usd=0.015,
        wall_time=2.5,
        turns_used=3,
        error=None,
    )

    loop = asyncio.new_event_loop()
    try:
        call_res = loop.run_until_complete(
            mcp_server.call_tool("bp_escalate", {
                "task_prompt": "Fix bug in verifier",
                "cost_cap_usd": 0.50,
            })
        )
    finally:
        loop.close()

    assert not call_res.is_error
    data = call_res.structured_content or json.loads(call_res.content[0].text)
    assert data["success"] is True
    assert "--- a/utils.py" in data["patch"]
    assert data["cost_usd"] == 0.015
    assert data["turns_used"] == 3


def test_mcp_control_bp_report(mcp_server, tmp_path, monkeypatch):
    """Verify bp_report outputs JSON session report."""
    monkeypatch.setenv("BP_HOME", str(tmp_path))
    db_file = tmp_path / "bp.db"
    init_db(db_file)

    safe_execute_write(
        conn_or_path=db_file,
        sql="INSERT INTO sessions (session_id, mode, status, total_cost) VALUES ('report_sess', 'enforce', 'done', 0.005);",
    )

    loop = asyncio.new_event_loop()
    try:
        call_res = loop.run_until_complete(
            mcp_server.call_tool("bp_report", {"format_type": "json"})
        )
    finally:
        loop.close()

    assert not call_res.is_error
    data = call_res.structured_content or json.loads(call_res.content[0].text)
    assert "sessions" in data or "session_id" in data or "total_sessions" in data or "message" in data


# =====================================================================
# Proxy Tools Tests
# =====================================================================

def test_mcp_proxy_list_dir_and_search(mcp_server, tmp_path):
    """Verify bp_list_dir and bp_search operate sandboxed in repo_root."""
    sample_file = tmp_path / "hello.py"
    sample_file.write_text("def hello():\n    return 'budgetpilot'\n")

    loop = asyncio.new_event_loop()
    try:
        # 1. bp_list_dir
        res_list = loop.run_until_complete(
            mcp_server.call_tool("bp_list_dir", {"path": ".", "repo_root": str(tmp_path)})
        )
        data_list = res_list.structured_content or json.loads(res_list.content[0].text)
        assert data_list["success"] is True
        assert "hello.py" in data_list["output"]

        # 2. bp_search
        res_search = loop.run_until_complete(
            mcp_server.call_tool("bp_search", {"pattern": "budgetpilot", "repo_root": str(tmp_path)})
        )
        data_search = res_search.structured_content or json.loads(res_search.content[0].text)
        assert data_search["success"] is True
        assert "hello.py" in data_search["output"]
    finally:
        loop.close()


def test_mcp_proxy_read_and_edit_file(mcp_server, tmp_path):
    """Verify bp_read_file and bp_edit_file operate with gate tracking."""
    test_file = tmp_path / "calc.py"
    test_file.write_text("def add(a, b):\n    return a - b\n")

    loop = asyncio.new_event_loop()
    try:
        # 1. bp_read_file
        res_read = loop.run_until_complete(
            mcp_server.call_tool("bp_read_file", {"path": "calc.py", "repo_root": str(tmp_path)})
        )
        data_read = res_read.structured_content or json.loads(res_read.content[0].text)
        assert data_read["success"] is True
        assert "return a - b" in data_read["output"]

        # 2. bp_edit_file
        res_edit = loop.run_until_complete(
            mcp_server.call_tool("bp_edit_file", {
                "path": "calc.py",
                "old": "return a - b",
                "new": "return a + b",
                "repo_root": str(tmp_path),
            })
        )
        data_edit = res_edit.structured_content or json.loads(res_edit.content[0].text)
        assert data_edit["success"] is True
        assert "Successfully modified calc.py" in data_edit["output"]
        assert "return a + b" in test_file.read_text()
    finally:
        loop.close()


def test_mcp_proxy_gate_denial(mcp_server, tmp_path, monkeypatch):
    """
    Verify that proxy tools respect gate denials in enforce mode:
    e.g. attempting to edit a second file in scope 1 is blocked.
    """
    monkeypatch.setenv("BP_HOME", str(tmp_path))
    monkeypatch.setenv("BP_MODE", "enforce")

    file1 = tmp_path / "file1.py"
    file1.write_text("x = 1\n")
    file2 = tmp_path / "file2.py"
    file2.write_text("y = 2\n")

    sess_id = "test_deny_sess"

    loop = asyncio.new_event_loop()
    try:
        # Edit first file -> touched_files = {file1.py}
        res1 = loop.run_until_complete(
            mcp_server.call_tool("bp_edit_file", {
                "path": "file1.py",
                "old": "x = 1",
                "new": "x = 10",
                "session_id": sess_id,
                "repo_root": str(tmp_path),
            })
        )
        data1 = res1.structured_content or json.loads(res1.content[0].text)
        assert data1["success"] is True

        # In Scope 1 (max 1 touched file), editing file2 is denied by ExecuteGate
        res2 = loop.run_until_complete(
            mcp_server.call_tool("bp_edit_file", {
                "path": "file2.py",
                "old": "y = 2",
                "new": "y = 20",
                "session_id": sess_id,
                "repo_root": str(tmp_path),
            })
        )
        data2 = res2.structured_content or json.loads(res2.content[0].text)
        assert data2["blocked"] is True
        assert data2["verdict"] == "deny"
        assert "[BLOCKED BY BUDGETPILOT GATE]" in data2["output"]
    finally:
        loop.close()

"""
Unit tests for BudgetPilot CLI commands (bp doctor, bp config, bp init).
"""

import json
from pathlib import Path
import tempfile
import pytest

from budgetpilot.cli import main, run_doctor


def test_cli_doctor_execution(capsys):
    """Verify bp doctor runs diagnostics and reports passed checks."""
    exit_code = run_doctor()
    captured = capsys.readouterr().out

    assert "BudgetPilot Doctor — System Diagnostics" in captured
    assert "[PASS] Git" in captured
    assert "[PASS] Database & Storage" in captured
    assert "[PASS] Prices Configuration" in captured
    assert "Gate Interception Latency" in captured
    assert "Diagnostics Result" in captured
    assert exit_code in (0, 1)


def test_cli_config_json_output(capsys):
    """Verify bp config --json prints valid parseable configuration."""
    exit_code = main(["config", "--json"])
    captured = capsys.readouterr().out

    assert exit_code == 0
    data = json.loads(captured)
    assert "mode" in data
    assert "tiers" in data
    assert "limits" in data
    assert "test_commands" in data


def test_cli_init_command(capsys):
    """Verify bp init updates execution mode and writes config."""
    exit_code = main(["init", "--mode", "enforce"])
    captured = capsys.readouterr().out

    assert exit_code == 0
    assert "Initialized BudgetPilot" in captured
    assert "enforce" in captured


def test_cli_report_empty_db(capsys):
    """Verify bp report gracefully handles empty database without throwing exceptions."""
    from budgetpilot.cli import run_report
    from budgetpilot.core.store import init_db

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "empty.db"
        init_db(db_path)

        # Terminal format
        code = run_report(db_path=db_path, format_type="terminal")
        captured = capsys.readouterr().out
        assert code == 0
        assert "No sessions logged" in captured

        # JSON format
        code_json = run_report(db_path=db_path, format_type="json")
        captured_json = capsys.readouterr().out
        assert code_json == 0
        data = json.loads(captured_json)
        assert data["sessions"] == []
        assert data["message"] == "No sessions logged"


def test_cli_report_single_session_and_formats(capsys):
    """Verify bp report outputs terminal, markdown, and JSON formats for a recorded session."""
    from budgetpilot.cli import run_report
    from budgetpilot.core.store import get_connection, init_db, safe_execute_write

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "report_test.db"
        init_db(db_path)
        sess_id = "test_report_sess_01"

        # Seed session
        safe_execute_write(
            conn_or_path=db_path,
            sql="""
            INSERT INTO sessions (session_id, mode, status, total_cost, total_tokens, metadata)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            params=(sess_id, "observe", "done", 0.0025, 1200, json.dumps({"passed": True})),
        )

        # Seed model call (agent)
        safe_execute_write(
            conn_or_path=db_path,
            sql="""
            INSERT INTO model_calls (session_id, model, input_tokens, output_tokens, total_tokens, estimated_cost, source, prompt, response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            params=(sess_id, "qwen2.5:3b", 800, 200, 1000, 0.0020, "live", "prompt", "reply"),
        )

        # Seed model call (escalation)
        safe_execute_write(
            conn_or_path=db_path,
            sql="""
            INSERT INTO model_calls (session_id, model, input_tokens, output_tokens, total_tokens, estimated_cost, source, prompt, response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            params=(sess_id, "qwen3:4b", 150, 50, 200, 0.0005, "escalation", "escalation prompt", "escalation reply"),
        )

        # Seed tool event
        safe_execute_write(
            conn_or_path=db_path,
            sql="""
            INSERT INTO tool_events (session_id, tool_name, arguments, result, duration, is_error)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            params=(sess_id, "list_dir", "{}", "file1.py", 0.025, 0),
        )

        # 1. Terminal format
        code_term = run_report(session_id=sess_id, format_type="terminal", db_path=db_path)
        out_term = capsys.readouterr().out
        assert code_term == 0
        assert f"BudgetPilot Session Report: {sess_id}" in out_term
        assert "agent" in out_term
        assert "escalation" in out_term
        assert "list_dir" in out_term
        assert "PASSED" in out_term

        # 2. Markdown format
        code_md = run_report(session_id=sess_id, format_type="md", db_path=db_path)
        out_md = capsys.readouterr().out
        assert code_md == 0
        assert f"# BudgetPilot Session Report: `{sess_id}`" in out_md
        assert "| Source |" in out_md
        assert "| `list_dir` |" in out_md

        # 3. JSON format
        code_json = run_report(session_id=sess_id, format_type="json", db_path=db_path)
        out_json = capsys.readouterr().out
        assert code_json == 0
        data = json.loads(out_json)
        assert data["session_id"] == sess_id
        assert data["pass_fail"] == "PASSED"
        assert data["cost_split"]["agent"]["calls"] == 1
        assert data["cost_split"]["escalation"]["calls"] == 1
        assert len(data["tool_calls"]) == 1
        assert data["tool_calls"][0]["tool_name"] == "list_dir"


def test_cli_report_all_sessions_summary(capsys):
    """Verify bp report without --session provides all-sessions summary."""
    from budgetpilot.cli import run_report
    from budgetpilot.core.store import init_db, safe_execute_write

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "all_reports.db"
        init_db(db_path)

        for i in range(3):
            safe_execute_write(
                conn_or_path=db_path,
                sql="INSERT INTO sessions (session_id, mode, status, total_cost, total_tokens) VALUES (?, ?, ?, ?, ?);",
                params=(f"sess_{i}", "enforce", "done" if i < 2 else "failed", 0.001 * (i + 1), 500 * (i + 1)),
            )

        code = run_report(session_id=None, format_type="terminal", db_path=db_path)
        out = capsys.readouterr().out
        assert code == 0
        assert "BudgetPilot All Sessions Report" in out
        assert "Total Sessions:   3" in out
        assert "Passed / Done:    2" in out
        assert "Failed:           1" in out


def test_cli_report_nonexistent_session(capsys):
    """Verify querying nonexistent session returns graceful notice."""
    from budgetpilot.cli import run_report
    from budgetpilot.core.store import init_db, safe_execute_write

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "empty2.db"
        init_db(db_path)
        safe_execute_write(
            conn_or_path=db_path,
            sql="INSERT INTO sessions (session_id) VALUES ('sess_real');",
        )

        code = run_report(session_id="ghost_session_404", format_type="terminal", db_path=db_path)
        out = capsys.readouterr().out
        assert code == 0
        assert "No session found for ID 'ghost_session_404'" in out

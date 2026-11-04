"""
Integration tests for BudgetPilot agent loop, end-to-end execution, and SQLite recording (Phase B).
"""

import json
from pathlib import Path
import tempfile
import pytest

from budgetpilot.agent.loop import AgentLoopResult, run_agent_loop
from budgetpilot.agent.model import ModelResult
from budgetpilot.cli import main
from budgetpilot.core.store import get_connection, init_db


def test_agent_loop_end_to_end_scripted(monkeypatch):
    """
    Verify full end-to-end autonomous agent loop execution:
    1. Tool calls intercepted, checked by gate, executed on disk, and logged.
    2. File modification successfully performed.
    3. SQLite sessions, tool_events, gate_decisions, and task_events persisted.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        db_path = repo_root / "test_loop.db"
        init_db(db_path)

        # Create target file in repository
        greet_file = repo_root / "greet.py"
        greet_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

        # Scripted model responses simulating reasoning turns
        responses = [
            # Turn 1: Inspect directory
            ModelResult(
                content="I will list the directory.",
                model="qwen2.5:3b",
                tier="cheap",
                prompt_tokens=40,
                output_tokens=15,
                total_tokens=55,
                tool_calls=[{"name": "list_dir", "arguments": {"path": "."}}],
            ),
            # Turn 2: Inspect file content
            ModelResult(
                content="I will read greet.py.",
                model="qwen2.5:3b",
                tier="cheap",
                prompt_tokens=65,
                output_tokens=15,
                total_tokens=80,
                tool_calls=[{"name": "read_file", "arguments": {"path": "greet.py"}}],
            ),
            # Turn 3: Perform replacement
            ModelResult(
                content="I will replace 'world' with 'budgetpilot'.",
                model="qwen2.5:3b",
                tier="cheap",
                prompt_tokens=90,
                output_tokens=25,
                total_tokens=115,
                tool_calls=[{
                    "name": "edit_file",
                    "arguments": {"path": "greet.py", "old": "return 'world'", "new": "return 'budgetpilot'"},
                }],
            ),
            # Turn 4: Final textual response
            ModelResult(
                content="Task completed: successfully updated greet.py to return 'budgetpilot'.",
                model="qwen2.5:3b",
                tier="cheap",
                prompt_tokens=120,
                output_tokens=30,
                total_tokens=150,
                tool_calls=None,
            ),
        ]

        turn_counter = 0

        def mock_chat(**kwargs):
            nonlocal turn_counter
            res = responses[turn_counter]
            turn_counter += 1
            return res

        monkeypatch.setattr("budgetpilot.agent.loop.chat", mock_chat)

        result: AgentLoopResult = run_agent_loop(
            prompt="Update greet.py to return 'budgetpilot'",
            repo_path=str(repo_root),
            mode="enforce",
            max_turns=5,
            db_path=db_path,
            tier="cheap",
        )

        # 1. Verify Loop Outcome
        assert result.status == "completed"
        assert result.turns == 4
        assert result.tool_calls_count == 3
        assert "greet.py" in result.touched_files
        assert "greet.py" in result.read_files
        assert "Task completed" in result.answer

        # 2. Verify file on disk was modified
        assert "return 'budgetpilot'" in greet_file.read_text(encoding="utf-8")

        # 3. Verify SQLite persistence
        conn = get_connection(db_path)

        # Check sessions
        sess_row = conn.execute(
            "SELECT status, mode FROM sessions WHERE session_id = ?;",
            (result.session_id,),
        ).fetchone()
        assert sess_row is not None
        assert sess_row["status"] == "done"
        assert sess_row["mode"] == "enforce"

        # Check tool_events
        cursor = conn.execute(
            "SELECT tool_name, is_error FROM tool_events WHERE session_id = ? ORDER BY id ASC;",
            (result.session_id,),
        )
        tool_events = cursor.fetchall()
        assert len(tool_events) == 3
        assert [e["tool_name"] for e in tool_events] == ["list_dir", "read_file", "edit_file"]
        assert all(e["is_error"] == 0 for e in tool_events)

        # Check gate_decisions
        cursor = conn.execute(
            "SELECT verdict FROM gate_decisions WHERE session_id = ?;",
            (result.session_id,),
        )
        gate_decisions = cursor.fetchall()
        assert len(gate_decisions) == 3
        assert all(d["verdict"] == "allow" for d in gate_decisions)

        # Check task_events
        cursor = conn.execute(
            "SELECT event_type FROM task_events WHERE session_id = ?;",
            (result.session_id,),
        )
        task_events = [e["event_type"] for e in cursor.fetchall()]
        assert "task_start" in task_events
        assert "task_end" in task_events

        # Check model_calls
        cursor = conn.execute(
            "SELECT COUNT(*) FROM model_calls WHERE session_id = ?;",
            (result.session_id,),
        )
        assert cursor.fetchone()[0] == 4

        conn.close()


def test_agent_loop_max_turns_exceeded(monkeypatch):
    """Verify agent loop terminates and records status when max_turns is reached."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "max_turns.db"
        init_db(db_path)

        # Model that always requests list_dir
        def mock_infinite_chat(**kwargs):
            return ModelResult(
                content="Keep searching",
                model="qwen2.5:3b",
                tier="cheap",
                prompt_tokens=20,
                output_tokens=10,
                total_tokens=30,
                tool_calls=[{"name": "list_dir", "arguments": {"path": "."}}],
            )

        monkeypatch.setattr("budgetpilot.agent.loop.chat", mock_infinite_chat)

        result = run_agent_loop(
            prompt="Endless task",
            repo_path=tmpdir,
            max_turns=3,
            db_path=db_path,
        )

        assert result.status == "max_turns_exceeded"
        assert result.turns == 3
        assert result.tool_calls_count == 3


def test_cli_bp_run_execution(monkeypatch, capsys):
    """Verify bp run executes and displays final answer and summary."""
    with tempfile.TemporaryDirectory() as tmpdir:
        def mock_chat(**kwargs):
            return ModelResult(
                content="All issues resolved.",
                model="qwen2.5:3b",
                tier="cheap",
                prompt_tokens=50,
                output_tokens=20,
                total_tokens=70,
                tool_calls=None,
            )

        monkeypatch.setattr("budgetpilot.agent.loop.chat", mock_chat)

        exit_code = main([
            "run",
            "--task", "Fix typo in README",
            "--repo", str(tmpdir),
            "--mode", "enforce",
        ])

        captured = capsys.readouterr().out
        assert exit_code == 0
        assert "BudgetPilot Autonomous Agent Run" in captured
        assert "Fix typo in README" in captured
        assert "All issues resolved." in captured
        assert "Execution Summary" in captured
        assert "Total Reference Cost" in captured


def test_agent_loop_fail_open_on_gate_error(monkeypatch):
    """
    Verify fail-open invariant: if an internal BudgetPilot exception occurs
    (e.g., gate check crashes), the operation is allowed and execution completes cleanly.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        db_path = repo_root / "fail_open.db"
        init_db(db_path)

        dummy_file = repo_root / "sample.py"
        dummy_file.write_text("x = 10\n", encoding="utf-8")

        responses = [
            ModelResult(
                content="Reading sample.py",
                model="qwen2.5:3b",
                tier="cheap",
                prompt_tokens=30,
                output_tokens=10,
                total_tokens=40,
                tool_calls=[{"name": "read_file", "arguments": {"path": "sample.py"}}],
            ),
            ModelResult(
                content="Completed despite gate failure.",
                model="qwen2.5:3b",
                tier="cheap",
                prompt_tokens=50,
                output_tokens=20,
                total_tokens=70,
                tool_calls=None,
            ),
        ]
        turn = 0

        def mock_chat(**kwargs):
            nonlocal turn
            res = responses[turn]
            turn += 1
            return res

        def mock_broken_gate(*args, **kwargs):
            raise RuntimeError("Simulated internal BudgetPilot gate exception")

        monkeypatch.setattr("budgetpilot.agent.loop.chat", mock_chat)
        monkeypatch.setattr("budgetpilot.agent.loop.gate_check", mock_broken_gate)

        result = run_agent_loop(
            prompt="Read sample.py",
            repo_path=str(repo_root),
            mode="enforce",
            max_turns=5,
            db_path=db_path,
        )

        # Invariant: Must fail open and complete the task successfully
        assert result.status == "completed"
        assert result.turns == 2
        assert result.tool_calls_count == 1
        assert "sample.py" in result.read_files
        assert "Completed despite gate failure." in result.answer

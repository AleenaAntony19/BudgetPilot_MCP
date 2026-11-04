"""
Unit tests for BudgetPilot task tracker, lifecycle, read/touched sets, git snapshots, and tool normalizer (Phase B).
"""

import json
from pathlib import Path
import subprocess
import tempfile
import pytest

from budgetpilot.core.gate import ExecuteGate, check as gate_check
from budgetpilot.core.store import get_connection, init_db
from budgetpilot.core.tracker import (
    TaskState,
    TaskStatus,
    TaskTracker,
    create_git_snapshot,
)
from budgetpilot.core.types import (
    BpEvent,
    GateVerdictStatus,
    ToolCall,
    normalize_tool_call,
)


def test_task_lifecycle_and_sqlite_persistence():
    """Verify task lifecycle transitions and persistence to SQLite sessions and task_events."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_bp.db"
        init_db(db_path)

        tracker = TaskTracker(
            session_id="sess_lifecycle_1",
            repo_root=tmpdir,
            db_path=db_path,
            mode="enforce",
        )

        assert tracker.state.status == TaskStatus.IDLE

        # 1. Start task
        state = tracker.start_task()
        assert state.status == TaskStatus.EXECUTING

        conn = get_connection(db_path)
        # Check sessions table
        sess_row = conn.execute(
            "SELECT mode, status, metadata FROM sessions WHERE session_id = 'sess_lifecycle_1';"
        ).fetchone()
        assert sess_row is not None
        assert sess_row["status"] == "executing"
        assert sess_row["mode"] == "enforce"

        # Check task_events table for task_start
        ev_row = conn.execute(
            "SELECT event_type, payload FROM task_events WHERE session_id = 'sess_lifecycle_1' AND event_type = 'task_start';"
        ).fetchone()
        assert ev_row is not None
        start_payload = json.loads(ev_row["payload"])
        assert start_payload["status"] == "executing"

        # 2. Transition to VERIFYING
        tracker.update_status(TaskStatus.VERIFYING, payload={"test_cmd": "pytest"})
        assert tracker.state.status == TaskStatus.VERIFYING

        sess_row = conn.execute(
            "SELECT status FROM sessions WHERE session_id = 'sess_lifecycle_1';"
        ).fetchone()
        assert sess_row["status"] == "verifying"

        # 3. Conclude task with success -> DONE
        tracker.end_task(success=True, metadata={"reason": "tests passed"})
        assert tracker.state.status == TaskStatus.DONE

        sess_row = conn.execute(
            "SELECT status FROM sessions WHERE session_id = 'sess_lifecycle_1';"
        ).fetchone()
        assert sess_row["status"] == "done"

        ev_end = conn.execute(
            "SELECT payload FROM task_events WHERE session_id = 'sess_lifecycle_1' AND event_type = 'task_end';"
        ).fetchone()
        assert ev_end is not None
        end_payload = json.loads(ev_end["payload"])
        assert end_payload["success"] is True

        conn.close()


def test_read_and_touched_sets_tracking():
    """Verify tracking and path normalization for read_set and touched_set."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        tracker = TaskTracker(session_id="sess_sets", repo_root=repo_root)

        # Explicit tracking
        tracker.record_read("src/app.py")
        tracker.record_read(repo_root / "README.md")
        tracker.record_touch(repo_root / "src/app.py")

        assert "src/app.py" in tracker.state.read_set
        assert "README.md" in tracker.state.read_set
        assert tracker.state.touched_set == {"src/app.py"}

        # Automatic tool call tracking
        tracker.record_tool_call("read_file", {"path": "docs/STATUS.md"})
        tracker.record_tool_call("edit_file", {"path": "src/app.py", "old": "1", "new": "2"})
        tracker.record_tool_call("edit_file", {"path": "src/utils.py", "old": "a", "new": "b"})

        assert "docs/STATUS.md" in tracker.state.read_set
        assert "src/utils.py" in tracker.state.touched_set
        assert len(tracker.state.touched_set) == 2


def test_git_snapshot_inside_git_repo():
    """Verify non-destructive git snapshot creation using HEAD and git stash create."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir)

        # Initialize real git repository
        subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo_dir), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True)

        initial_file = repo_dir / "init.txt"
        initial_file.write_text("initial commit content", encoding="utf-8")
        subprocess.run(["git", "add", "init.txt"], cwd=str(repo_dir), check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_dir), check=True)

        # 1. Clean repo snapshot
        snap1, head1 = create_git_snapshot(repo_dir)
        assert snap1 is not None
        assert head1 is not None
        assert snap1 == head1  # Clean working tree -> snapshot_ref is HEAD

        # 2. Modify working tree with uncommitted edit
        initial_file.write_text("modified uncommitted content", encoding="utf-8")

        snap2, head2 = create_git_snapshot(repo_dir)
        assert snap2 is not None
        assert head2 is not None
        assert head2 == head1  # HEAD commit didn't change
        assert snap2 != head2  # Stash create generated a distinct working tree commit!

        # Verify working tree content was NOT lost or modified
        assert initial_file.read_text(encoding="utf-8") == "modified uncommitted content"


def test_git_snapshot_outside_git_repo():
    """Verify create_git_snapshot gracefully returns (None, None) outside git repositories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        snap, head = create_git_snapshot(tmpdir)
        assert snap is None
        assert head is None

        # None input
        snap_none, head_none = create_git_snapshot(None)
        assert snap_none is None
        assert head_none is None


def test_normalize_tool_call_payloads():
    """Verify converting various tool call payloads into standard BpEvent instances."""
    # 1. Standard ToolCall dataclass
    tc = ToolCall(tool_name="read_file", arguments={"path": "main.py"})
    ev1 = normalize_tool_call(tc, session_id="sess_1")
    assert ev1.event_type == "read_file"
    assert ev1.session_id == "sess_1"
    assert ev1.payload["tool_name"] == "read_file"
    assert ev1.payload["arguments"]["path"] == "main.py"

    # 2. Ollama / OpenAI function call dictionary
    dict_call = {
        "function": {
            "name": "edit_file",
            "arguments": json.dumps({"path": "calc.py", "old": "1", "new": "2"}),
        },
        "id": "call_abc123",
    }
    ev2 = BpEvent.from_tool_call(dict_call, session_id="sess_2", cost_usd=0.0005)
    assert ev2.event_type == "edit_file"
    assert ev2.payload["arguments"]["old"] == "1"
    assert ev2.payload["call_id"] == "call_abc123"
    assert ev2.cost_usd == 0.0005

    # 3. Canonical tool types: search, list_dir, run_tests
    for t_name in ("search", "list_dir", "run_tests"):
        ev = normalize_tool_call({"name": t_name, "arguments": {}})
        assert ev.event_type == t_name

    # 4. Unknown tool -> normalized to 'other'
    ev_unknown = normalize_tool_call({"name": "custom_mcp_fetch", "arguments": {}})
    assert ev_unknown.event_type == "other"
    assert ev_unknown.payload["tool_name"] == "custom_mcp_fetch"


def test_gate_check_passthrough():
    """Verify gate.check and ExecuteGate.check return passthrough allow verdicts."""
    ev = BpEvent(event_type="read_file", payload={"path": "main.py", "start": 1, "end": 100})
    state = TaskState(session_id="sess_gate_test")

    # Functional check
    verdict1 = gate_check(ev, state)
    assert verdict1.status == GateVerdictStatus.ALLOW
    assert "allowed by execute gate" in verdict1.reason.lower()

    # Class method check
    gate = ExecuteGate(mode="observe")
    verdict2 = gate.check(ev, state)
    assert verdict2.status == GateVerdictStatus.ALLOW

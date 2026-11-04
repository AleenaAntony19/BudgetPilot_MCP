"""
Integration tests for BudgetPilot baseline benchmark runner, git worktree lifecycle,
and SQLite reporting (Phase C).
"""

import json
from pathlib import Path
import subprocess
import tempfile
import pytest
import yaml

from budgetpilot.agent.model import ModelResult
from budgetpilot.bench.baseline import (
    BenchmarkSummary,
    BenchmarkTask,
    TaskRunResult,
    cleanup_worktree,
    create_worktree,
    load_tasks,
    run_baseline_benchmark,
    run_benchmark_task,
)
from budgetpilot.cli import main
from budgetpilot.core.store import get_connection, init_db


def setup_mock_git_repo(path: Path) -> Path:
    """Helper to initialize a real git repository with an initial commit."""
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@budgetpilot.io"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "BudgetPilot Tester"], cwd=str(path), check=True)

    # Initial file
    (path / "calc.py").write_text("def add_two(x):\n    return x + 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit with buggy calc"], cwd=str(path), check=True, capture_output=True)
    return path


def test_load_tasks_valid_and_schema():
    """Verify loading and parsing tasks.yaml with schema validation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tasks_file = Path(tmpdir) / "tasks.yaml"

        # 1. Top-level list
        tasks_data = [
            {
                "id": "task_1",
                "prompt": "Fix bug in calc.py",
                "verify_command": "python -c 'import calc; assert calc.add_two(2) == 4'",
                "base_commit": "HEAD",
                "expected_scope": 1,
            },
            {
                "id": "task_2",
                "prompt": "Implement subtract",
                "verify_command": "pytest",
            },
        ]
        tasks_file.write_text(yaml.dump(tasks_data), encoding="utf-8")

        loaded = load_tasks(tasks_file)
        assert len(loaded) == 2
        assert loaded[0].id == "task_1"
        assert loaded[0].expected_scope == 1
        assert loaded[0].base_commit == "HEAD"
        # task_2 default values
        assert loaded[1].id == "task_2"
        assert loaded[1].expected_scope == 1
        assert loaded[1].base_commit == "HEAD"

        # 2. Dictionary with 'tasks' key
        dict_data = {"tasks": tasks_data}
        tasks_file.write_text(yaml.dump(dict_data), encoding="utf-8")
        loaded_dict = load_tasks(tasks_file)
        assert len(loaded_dict) == 2
        assert loaded_dict[0].id == "task_1"

        # 3. Invalid expected_scope
        bad_scope = [{"id": "bad", "prompt": "p", "verify_command": "v", "expected_scope": 5}]
        tasks_file.write_text(yaml.dump(bad_scope), encoding="utf-8")
        with pytest.raises(ValueError, match="expected_scope must be 1, 2, or 3"):
            load_tasks(tasks_file)

        # 4. Missing required field (verify_command)
        missing_verify = [{"id": "bad", "prompt": "p"}]
        tasks_file.write_text(yaml.dump(missing_verify), encoding="utf-8")
        with pytest.raises(ValueError, match="verify_command"):
            load_tasks(tasks_file)

        # 5. Non-existent file
        with pytest.raises(FileNotFoundError):
            load_tasks(Path(tmpdir) / "missing.yaml")


def test_worktree_creation_and_safe_cleanup():
    """Verify git worktree creation at HEAD and safe cleanup in finally block."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir) / "repo"
        repo_root.mkdir()
        setup_mock_git_repo(repo_root)

        # Create worktree
        wt_path = create_worktree(repo_root, base_commit="HEAD")
        assert wt_path.exists()
        assert (wt_path / "calc.py").exists()
        assert "add_two" in (wt_path / "calc.py").read_text(encoding="utf-8")

        # Verify worktree appears in git worktree list
        res = subprocess.run(["git", "worktree", "list"], cwd=str(repo_root), capture_output=True, text=True)
        assert str(wt_path) in res.stdout

        # Cleanup worktree
        cleanup_worktree(repo_root, wt_path)
        assert not wt_path.exists()

        # Verify worktree list no longer contains the path
        res2 = subprocess.run(["git", "worktree", "list"], cwd=str(repo_root), capture_output=True, text=True)
        assert str(wt_path) not in res2.stdout

        # Calling cleanup again is safe and idempotent
        cleanup_worktree(repo_root, wt_path)


def test_worktree_creation_non_git_repo_raises():
    """Verify create_worktree raises ValueError when called on a non-git directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        non_git = Path(tmpdir) / "empty"
        non_git.mkdir()
        with pytest.raises(ValueError, match="not a valid git repository"):
            create_worktree(non_git, base_commit="HEAD")


def test_run_benchmark_task_pass_with_scripted_model(monkeypatch):
    """
    Verify successful benchmark task run:
    1. Agent runs in worktree ungated (mode=off).
    2. Model modifies code to satisfy verify_command.
    3. verify_command succeeds (exit code 0).
    4. SQLite sessions, reports, and usage are persisted.
    5. Worktree is safely cleaned up.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir) / "repo"
        repo_root.mkdir()
        setup_mock_git_repo(repo_root)
        db_path = repo_root / "bench.db"
        init_db(db_path)

        task = BenchmarkTask(
            id="task_fix_calc",
            prompt="Fix calc.py so add_two(x) returns x + 2",
            verify_command="python -c 'import calc; assert calc.add_two(3) == 5'",
            base_commit="HEAD",
            expected_scope=1,
        )

        responses = [
            ModelResult(
                content="I will fix calc.py.",
                model="qwen2.5:3b",
                tier="cheap",
                prompt_tokens=40,
                output_tokens=20,
                total_tokens=60,
                tool_calls=[{
                    "name": "edit_file",
                    "arguments": {
                        "path": "calc.py",
                        "old": "return x + 1",
                        "new": "return x + 2",
                    },
                }],
            ),
            ModelResult(
                content="Task completed: updated calc.py to return x + 2.",
                model="qwen2.5:3b",
                tier="cheap",
                prompt_tokens=70,
                output_tokens=25,
                total_tokens=95,
                tool_calls=None,
            ),
        ]
        turn = 0

        def mock_chat(**kwargs):
            nonlocal turn
            res = responses[turn]
            turn += 1
            return res

        monkeypatch.setattr("budgetpilot.agent.loop.chat", mock_chat)

        result: TaskRunResult = run_benchmark_task(
            task=task,
            repo_root=repo_root,
            repeat=1,
            db_path=db_path,
        )

        # 1. Outcome validation
        assert result.passed is True
        assert result.exit_code == 0
        assert result.tool_calls == 1
        assert result.status == "completed"
        assert result.cost_usd > 0.0
        assert result.wall_time > 0.0

        # 2. Worktree cleanup check (main repo file must NOT be modified by worktree edits)
        assert "return x + 1" in (repo_root / "calc.py").read_text(encoding="utf-8")

        # 3. Database persistence check
        conn = get_connection(db_path)

        # Check sessions
        sess_row = conn.execute(
            "SELECT status, mode, total_tokens FROM sessions WHERE session_id = ?;",
            (result.session_id,),
        ).fetchone()
        assert sess_row is not None
        assert sess_row["status"] == "done"
        assert sess_row["mode"] == "off"
        assert sess_row["total_tokens"] > 0

        # Check reports
        report_row = conn.execute(
            "SELECT report_type, total_cost_usd, summary, metadata FROM reports WHERE session_id = ?;",
            (result.session_id,),
        ).fetchone()
        assert report_row is not None
        assert report_row["report_type"] == "benchmark_baseline"
        assert "PASSED" in report_row["summary"]
        report_meta = json.loads(report_row["metadata"])
        assert report_meta["passed"] is True
        assert report_meta["task_id"] == "task_fix_calc"

        # Check usage
        usage_row = conn.execute(
            "SELECT tier, cost_usd, tokens_in, tokens_out FROM usage WHERE session_id = ?;",
            (result.session_id,),
        ).fetchone()
        assert usage_row is not None
        assert usage_row["tier"] == "cheap"
        assert usage_row["cost_usd"] > 0.0

        conn.close()


def test_run_benchmark_task_fail_with_scripted_model(monkeypatch):
    """
    Verify failed benchmark task run:
    1. Agent finishes without fixing bug.
    2. verify_command fails (exit code != 0).
    3. SQLite sessions records status='failed', reports records passed=False.
    4. Worktree is safely cleaned up despite failure.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir) / "repo"
        repo_root.mkdir()
        setup_mock_git_repo(repo_root)
        db_path = repo_root / "bench_fail.db"
        init_db(db_path)

        task = BenchmarkTask(
            id="task_fail_calc",
            prompt="Fix calc.py",
            verify_command="python -c 'import calc; assert calc.add_two(3) == 5'",
            base_commit="HEAD",
            expected_scope=1,
        )

        def mock_chat_no_fix(**kwargs):
            return ModelResult(
                content="I don't know how to fix this.",
                model="qwen2.5:3b",
                tier="cheap",
                prompt_tokens=30,
                output_tokens=15,
                total_tokens=45,
                tool_calls=None,
            )

        monkeypatch.setattr("budgetpilot.agent.loop.chat", mock_chat_no_fix)

        result: TaskRunResult = run_benchmark_task(
            task=task,
            repo_root=repo_root,
            repeat=1,
            db_path=db_path,
        )

        assert result.passed is False
        assert result.exit_code != 0
        assert "AssertionError" in result.verify_stderr

        # Check sessions table
        conn = get_connection(db_path)
        sess_row = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?;",
            (result.session_id,),
        ).fetchone()
        assert sess_row["status"] == "failed"

        # Check reports table
        report_row = conn.execute(
            "SELECT summary, metadata FROM reports WHERE session_id = ?;",
            (result.session_id,),
        ).fetchone()
        assert "FAILED" in report_row["summary"]
        report_meta = json.loads(report_row["metadata"])
        assert report_meta["passed"] is False

        conn.close()


def test_run_baseline_benchmark_full_suite(monkeypatch, capsys):
    """
    Verify full benchmark suite runner across multiple tasks and repetitions.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir) / "repo"
        repo_root.mkdir()
        setup_mock_git_repo(repo_root)
        db_path = repo_root / "suite.db"

        tasks_yaml = repo_root / "tasks.yaml"
        tasks_data = [
            {
                "id": "task_pass",
                "prompt": "Fix calc.py to return x + 2",
                "verify_command": "python -c 'import calc; assert calc.add_two(1) == 3'",
                "base_commit": "HEAD",
                "expected_scope": 1,
            },
            {
                "id": "task_fail",
                "prompt": "Fix calc.py to return x + 10",
                "verify_command": "python -c 'import calc; assert calc.add_two(1) == 11'",
                "base_commit": "HEAD",
                "expected_scope": 1,
            },
        ]
        tasks_yaml.write_text(yaml.dump(tasks_data), encoding="utf-8")

        # Mock model that fixes calc to return x + 2
        def mock_chat(**kwargs):
            messages = kwargs.get("messages", [])
            last_msg = messages[-1]["content"] if messages else ""
            if "Fix calc.py to return x + 2" in str(messages):
                if any(m.get("role") == "tool" for m in messages):
                    return ModelResult(
                        content="Done.",
                        model="qwen2.5:3b",
                        tier="cheap",
                        prompt_tokens=40,
                        output_tokens=10,
                        total_tokens=50,
                        tool_calls=None,
                    )
                return ModelResult(
                    content="Fixing to x + 2",
                    model="qwen2.5:3b",
                    tier="cheap",
                    prompt_tokens=30,
                    output_tokens=15,
                    total_tokens=45,
                    tool_calls=[{
                        "name": "edit_file",
                        "arguments": {"path": "calc.py", "old": "return x + 1", "new": "return x + 2"},
                    }],
                )
            else:
                return ModelResult(
                    content="Refusing to change",
                    model="qwen2.5:3b",
                    tier="cheap",
                    prompt_tokens=30,
                    output_tokens=10,
                    total_tokens=40,
                    tool_calls=None,
                )

        monkeypatch.setattr("budgetpilot.agent.loop.chat", mock_chat)

        summary: BenchmarkSummary = run_baseline_benchmark(
            repo_path=str(repo_root),
            tasks_file=str(tasks_yaml),
            repeats=2,
            db_path=db_path,
        )

        assert summary.total_tasks == 2
        assert summary.total_runs == 4
        assert summary.passed_runs == 2  # task_pass (2 runs) passed
        assert summary.failed_runs == 2  # task_fail (2 runs) failed
        assert summary.pass_rate == 50.0
        assert summary.total_tokens > 0
        assert summary.total_cost_usd > 0.0

        captured = capsys.readouterr().out
        assert "BudgetPilot Baseline Benchmark" in captured
        assert "task_pass" in captured
        assert "task_fail" in captured
        assert "Pass Rate:     50.0%" in captured


def test_cli_bp_bench_baseline(monkeypatch, capsys):
    """Verify bp bench baseline CLI subcommand execution."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir) / "repo"
        repo_root.mkdir()
        setup_mock_git_repo(repo_root)

        tasks_yaml = repo_root / "tasks.yaml"
        tasks_data = [{
            "id": "cli_task",
            "prompt": "Fix calc",
            "verify_command": "python -c 'exit(0)'",
            "base_commit": "HEAD",
        }]
        tasks_yaml.write_text(yaml.dump(tasks_data), encoding="utf-8")

        def mock_chat(**kwargs):
            return ModelResult(
                content="Done",
                model="qwen2.5:3b",
                tier="cheap",
                prompt_tokens=20,
                output_tokens=10,
                total_tokens=30,
                tool_calls=None,
            )

        monkeypatch.setattr("budgetpilot.agent.loop.chat", mock_chat)

        exit_code = main([
            "bench", "baseline",
            "--repo", str(repo_root),
            "--tasks", str(tasks_yaml),
            "--repeats", "1",
        ])

        captured = capsys.readouterr().out
        assert exit_code == 0
        assert "BudgetPilot Baseline Benchmark" in captured
        assert "cli_task" in captured
        assert "Pass Rate:     100.0%" in captured

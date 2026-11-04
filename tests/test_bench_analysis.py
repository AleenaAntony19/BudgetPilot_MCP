"""
Unit and integration tests for BudgetPilot Phase L:
- Benchmark multi-arm runner and git worktree isolation
- Analysis engine: bootstrap confidence intervals, paired cost difference at equal solve status,
  and expand-scope win rate calculations
- Report formatting: terminal, markdown, JSON, HTML
- CLI commands: bp bench run, bp status, bp report
- Architectural rule: Zero forbidden imports (ollama, mcp, fastapi, uvicorn)
"""

import ast
import json
from pathlib import Path
import tempfile
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from budgetpilot.agent.loop import AgentLoopResult
from budgetpilot.bench.analysis import (
    ArmSummary,
    BenchAnalysisReport,
    ExpandScopeWinRate,
    PairedDiffResult,
    analyze_bench_results,
    bootstrap_ci,
    compute_expand_scope_win_rate,
    compute_paired_cost_difference,
)
from budgetpilot.bench.baseline import BenchmarkTask
from budgetpilot.bench.runner import (
    ArmRunResult,
    arm_to_mode_and_slider,
    run_bench_arm_task,
    run_bench_suite,
)
from budgetpilot.cli import main, run_bench_run, run_report, run_status
from budgetpilot.core.store import init_db, safe_execute_write


# =====================================================================
# 1. Bootstrap CI Tests
# =====================================================================

def test_bootstrap_ci_empty_and_single():
    """Verify bootstrap_ci handles empty and single-element inputs gracefully."""
    mean, low, high = bootstrap_ci([])
    assert mean == 0.0
    assert low == 0.0
    assert high == 0.0

    mean, low, high = bootstrap_ci([42.5])
    assert mean == 42.5
    assert low == 42.5
    assert high == 42.5


def test_bootstrap_ci_deterministic_with_seed():
    """Verify bootstrap_ci is deterministic when seed is specified."""
    data = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    mean1, low1, high1 = bootstrap_ci(data, ci=0.95, n_resamples=500, seed=42)
    mean2, low2, high2 = bootstrap_ci(data, ci=0.95, n_resamples=500, seed=42)
    assert (mean1, low1, high1) == (mean2, low2, high2)
    assert low1 < high1
    # Mean of data is 5.5, CI should contain 5.5
    assert low1 <= 5.5 <= high1


def test_bootstrap_ci_confidence_intervals():
    """Verify higher confidence levels yield wider intervals."""
    data = [10.0, 12.0, 14.0, 15.0, 16.0, 18.0, 20.0, 22.0]
    _, low_90, high_90 = bootstrap_ci(data, ci=0.90, n_resamples=1000, seed=123)
    _, low_99, high_99 = bootstrap_ci(data, ci=0.99, n_resamples=1000, seed=123)

    width_90 = high_90 - low_90
    width_99 = high_99 - low_99
    assert width_99 >= width_90


# =====================================================================
# 2. Paired Cost Difference Tests (Equal Solve Status)
# =====================================================================

def test_paired_cost_difference_equal_solve_status():
    """
    Verify that only runs with identical solve status (passed==passed or failed==failed)
    are included in the paired cost difference and savings calculations.
    """
    runs = [
        # Baseline 'off' arm runs
        ArmRunResult("task1", "off", 1, "s1", passed=True, exit_code=0, wall_time=1.0, total_tokens=1000, prompt_tokens=800, output_tokens=200, tool_calls=2, cost_usd=0.010, status="done"),
        ArmRunResult("task2", "off", 1, "s2", passed=False, exit_code=1, wall_time=1.0, total_tokens=500, prompt_tokens=400, output_tokens=100, tool_calls=1, cost_usd=0.005, status="failed"),
        ArmRunResult("task3", "off", 1, "s3", passed=True, exit_code=0, wall_time=1.0, total_tokens=2000, prompt_tokens=1600, output_tokens=400, tool_calls=4, cost_usd=0.020, status="done"),

        # Target 'tight' arm runs
        # task1: both passed -> included
        # task2: target passed, base failed -> mismatched solve status -> EXCLUDED from equal status savings
        # task3: both passed -> included
        ArmRunResult("task1", "tight", 1, "s4", passed=True, exit_code=0, wall_time=0.8, total_tokens=600, prompt_tokens=500, output_tokens=100, tool_calls=1, cost_usd=0.006, status="done"),
        ArmRunResult("task2", "tight", 1, "s5", passed=True, exit_code=0, wall_time=0.9, total_tokens=700, prompt_tokens=550, output_tokens=150, tool_calls=2, cost_usd=0.007, status="done"),
        ArmRunResult("task3", "tight", 1, "s6", passed=True, exit_code=0, wall_time=0.7, total_tokens=1200, prompt_tokens=1000, output_tokens=200, tool_calls=2, cost_usd=0.012, status="done"),
    ]

    paired_dict = compute_paired_cost_difference(
        run_results=runs,
        baseline_arm="off",
        ci=0.95,
        seed=42,
    )

    assert "tight" in paired_dict
    paired = paired_dict["tight"]

    assert paired.arm == "tight"
    assert paired.baseline_arm == "off"
    assert paired.n_pairs_total == 3
    assert paired.n_equal_status == 2  # Only task1 and task3 have equal solve status
    assert paired.n_both_passed == 2

    # task1 diff: 0.006 - 0.010 = -0.004 (savings = +0.004)
    # task3 diff: 0.012 - 0.020 = -0.008 (savings = +0.008)
    # Mean cost diff = -0.006
    # Mean savings = 0.006
    assert pytest.approx(paired.mean_paired_cost_diff_usd, abs=1e-5) == -0.006
    assert pytest.approx(paired.mean_paired_savings_usd, abs=1e-5) == 0.006
    assert paired.ci_lower <= paired.mean_paired_savings_usd <= paired.ci_upper


def test_paired_cost_difference_empty_or_no_matches():
    """Verify paired cost diff returns empty mapping when no runs match."""
    paired = compute_paired_cost_difference([], baseline_arm="off")
    assert paired == {}


# =====================================================================
# 3. Expand-Scope Win Rate Tests
# =====================================================================

def test_expand_scope_win_rate_calculation():
    """
    Verify compute_expand_scope_win_rate identifies trials where
    expand_scope succeeded at lower cost than average escalate cost.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "win_rate.db"
        init_db(db_path)

        # Seed sessions first to respect foreign key constraints
        for i in range(2):
            safe_execute_write(
                conn_or_path=db_path,
                sql="INSERT INTO sessions (session_id) VALUES (?);",
                params=(f"esc_sess_{i}",),
            )
        for i in range(1, 5):
            safe_execute_write(
                conn_or_path=db_path,
                sql="INSERT INTO sessions (session_id) VALUES (?);",
                params=(f"exp_sess_{i}",),
            )

        # Insert escalate recovery runs with avg cost ~ $0.05
        for i, cost in enumerate([0.04, 0.06]):
            sess_id = f"esc_sess_{i}"
            safe_execute_write(
                conn_or_path=db_path,
                sql="""
                INSERT INTO recovery_decisions (id, session_id, failure_class, action)
                VALUES (?, ?, 'syntax_or_lint', 'escalate');
                """,
                params=(i + 1, sess_id),
            )
            safe_execute_write(
                conn_or_path=db_path,
                sql="""
                INSERT INTO recovery_outcomes (recovery_decision_id, session_id, action, succeeded, cost_usd, tokens_spent)
                VALUES (?, ?, 'escalate', 1, ?, 1000);
                """,
                params=(i + 1, sess_id, cost),
            )

        # Insert expand_scope recovery runs:
        # Case 1: succeeded, cost = $0.02 (< 0.05) -> WIN
        # Case 2: succeeded, cost = $0.07 (> 0.05) -> NOT A WIN (more expensive)
        # Case 3: failed (succeeded=0), cost = $0.01 -> NOT A WIN
        # Case 4: succeeded, cost = $0.03 (< 0.05) -> WIN
        expand_cases = [
            (10, "exp_sess_1", 1, 0.02),
            (11, "exp_sess_2", 1, 0.07),
            (12, "exp_sess_3", 0, 0.01),
            (13, "exp_sess_4", 1, 0.03),
        ]
        for dec_id, sess_id, succ, cost in expand_cases:
            safe_execute_write(
                conn_or_path=db_path,
                sql="""
                INSERT INTO recovery_decisions (id, session_id, failure_class, action)
                VALUES (?, ?, 'symbol_outside_touched', 'expand_scope');
                """,
                params=(dec_id, sess_id),
            )
            safe_execute_write(
                conn_or_path=db_path,
                sql="""
                INSERT INTO recovery_outcomes (recovery_decision_id, session_id, action, succeeded, cost_usd, tokens_spent)
                VALUES (?, ?, 'expand_scope', ?, ?, 500);
                """,
                params=(dec_id, sess_id, succ, cost),
            )

        win_rate_res = compute_expand_scope_win_rate(db_path)

        assert win_rate_res.total_expand_scope_attempts == 4
        assert win_rate_res.successful_expand_scope_attempts == 3
        assert win_rate_res.win_count == 2
        # Win rate = 2 / 4 = 0.50 (50%)
        assert pytest.approx(win_rate_res.win_rate, abs=1e-3) == 0.50
        # Success rate = 3 / 4 = 0.75
        assert pytest.approx(win_rate_res.success_rate, abs=1e-3) == 0.75
        # Avg escalate cost = (0.04 + 0.06) / 2 = 0.05
        assert pytest.approx(win_rate_res.avg_escalate_cost, abs=1e-4) == 0.05
        # Avg expand cost = (0.02 + 0.07 + 0.01 + 0.03) / 4 = 0.0325
        assert pytest.approx(win_rate_res.avg_expand_scope_cost, abs=1e-4) == 0.0325


# =====================================================================
# 4. Multi-Arm Analysis Engine & Formats Tests
# =====================================================================

def test_analyze_bench_results_and_reports():
    """Verify analyze_bench_results aggregates runs and formats terminal/md/json/html."""
    runs = [
        ArmRunResult("t1", "off", 1, "s1", True, 0, 1.0, 1000, 800, 200, 2, 0.010, "done"),
        ArmRunResult("t2", "off", 1, "s2", True, 0, 1.2, 1200, 900, 300, 3, 0.012, "done"),
        ArmRunResult("t1", "tight", 1, "s3", True, 0, 0.8, 600, 500, 100, 1, 0.006, "done"),
        ArmRunResult("t2", "tight", 1, "s4", True, 0, 0.9, 700, 550, 150, 2, 0.007, "done"),
        ArmRunResult("t1", "balanced", 1, "s5", True, 0, 0.9, 750, 600, 150, 2, 0.008, "done"),
        ArmRunResult("t2", "balanced", 1, "s6", False, 1, 1.0, 800, 650, 150, 2, 0.009, "failed"),
    ]

    report = analyze_bench_results(
        results=runs,
        baseline_arm="off",
    )

    assert "off" in report.arm_summaries
    assert "tight" in report.arm_summaries
    assert "balanced" in report.arm_summaries

    off_summary = report.arm_summaries["off"]
    assert off_summary.total_runs == 2
    assert off_summary.passed_runs == 2
    assert off_summary.pass_rate == 100.0
    assert pytest.approx(off_summary.total_cost_usd, abs=1e-5) == 0.022

    # Check paired diffs
    assert "tight" in report.paired_differences
    tight_diff = report.paired_differences["tight"]
    assert tight_diff.n_pairs_total == 2
    assert tight_diff.mean_paired_savings_usd > 0.0

    # 1. to_dict / to_json
    d = report.to_dict()
    assert d["baseline_arm"] == "off"
    assert "arm_summaries" in d
    assert "paired_differences" in d
    j = report.to_json()
    parsed = json.loads(j)
    assert parsed["baseline_arm"] == "off"

    # 2. to_terminal
    terminal_out = report.to_terminal()
    assert "BudgetPilot Benchmark Multi-Arm Analysis" in terminal_out
    assert "off" in terminal_out
    assert "tight" in terminal_out
    assert "Paired Savings at Equal Solve Status" in terminal_out

    # 3. to_markdown
    md_out = report.to_markdown()
    assert "# BudgetPilot Benchmark Multi-Arm Analysis (Phase L)" in md_out
    assert "| Arm | Total Runs | Passed | Pass Rate |" in md_out
    assert "| `off` |" in md_out
    assert "| `tight` |" in md_out

    # 4. to_html
    html_out = report.to_html()
    assert "<!DOCTYPE html>" in html_out
    assert "BudgetPilot Benchmark" in html_out
    assert "table" in html_out
    assert "tight" in html_out


# =====================================================================
# 5. Worktree Sandboxing & Arm Runner Mapping Tests
# =====================================================================

def test_arm_to_mode_and_slider_mapping():
    """Verify benchmark arm name maps to appropriate execution mode and slider."""
    assert arm_to_mode_and_slider("off") == ("off", "balanced")
    assert arm_to_mode_and_slider("observe") == ("observe", "balanced")
    assert arm_to_mode_and_slider("tight") == ("enforce", "tight")
    assert arm_to_mode_and_slider("balanced") == ("enforce", "balanced")
    assert arm_to_mode_and_slider("loose") == ("enforce", "loose")
    # Case insensitivity
    assert arm_to_mode_and_slider("TIGHT") == ("enforce", "tight")


@patch("budgetpilot.bench.runner.create_worktree")
@patch("budgetpilot.bench.runner.cleanup_worktree")
@patch("budgetpilot.agent.loop.run_agent_loop")
@patch("subprocess.run")
def test_run_bench_arm_task_execution(mock_subproc, mock_loop, mock_cleanup, mock_create, tmp_path):
    """
    Verify run_bench_arm_task:
    1. Creates worktree
    2. Runs agent loop with specified mode, slider, and caps
    3. Runs verify_command in worktree
    4. Guarantees worktree cleanup
    """
    mock_create.return_value = tmp_path / "worktree_sandbox"
    mock_cleanup.return_value = True

    # Mock agent loop with concrete dataclass instance
    mock_loop.return_value = AgentLoopResult(
        session_id="bench_sess_123",
        prompt="Fix the bug in utils.py",
        answer="Fixed",
        turns=2,
        tool_calls_count=3,
        read_files=["utils.py"],
        touched_files=["utils.py"],
        total_tokens=850,
        prompt_tokens=650,
        output_tokens=200,
        total_cost_usd=0.0075,
        status="completed",
        error=None,
    )

    # Mock verify_command
    mock_subproc.return_value = MagicMock(
        returncode=0,
        stdout="1 passed in 0.05s",
        stderr="",
    )

    task = BenchmarkTask(
        id="sample_task",
        prompt="Fix the bug in utils.py",
        verify_command="pytest tests/test_sample.py",
        base_commit="HEAD",
    )

    db_file = tmp_path / "bench_task_test.db"
    res = run_bench_arm_task(
        task=task,
        arm="tight",
        repo_root=tmp_path,
        repeat=1,
        tier="cheap",
        max_turns=15,
        cost_cap_usd=0.50,
        timeout=30,
        db_path=db_file,
    )

    # Verify worktree creation and cleanup
    mock_create.assert_called_once()
    mock_cleanup.assert_called_once()

    # Verify agent loop arguments
    mock_loop.assert_called_once()
    _, loop_kwargs = mock_loop.call_args
    assert loop_kwargs["mode"] == "enforce"
    assert loop_kwargs["budget_slider"] == "tight"
    assert loop_kwargs["max_turns"] == 15
    assert loop_kwargs["cost_cap_usd"] == 0.50

    # Verify result fields
    assert res.task_id == "sample_task"
    assert res.arm == "tight"
    assert res.passed is True
    assert res.exit_code == 0
    assert res.cost_usd == 0.0075
    assert res.total_tokens == 850
    assert "1 passed" in res.verify_stdout


# =====================================================================
# 6. Benchmark Suite Execution Engine
# =====================================================================

@patch("budgetpilot.bench.runner.load_tasks")
@patch("budgetpilot.bench.runner.run_bench_arm_task")
def test_run_bench_suite_execution(mock_run_arm, mock_load_tasks, tmp_path):
    """Verify run_bench_suite iterates across tasks, arms, and repeats."""
    task1 = BenchmarkTask(id="task_A", prompt="Fix A", verify_command="true", base_commit="HEAD")
    task2 = BenchmarkTask(id="task_B", prompt="Fix B", verify_command="true", base_commit="HEAD")
    mock_load_tasks.return_value = [task1, task2]

    # Setup arm results
    def fake_arm_task(task, arm, **kwargs):
        return ArmRunResult(
            task_id=task.id,
            arm=arm,
            repeat=kwargs.get("repeat", 1),
            session_id=f"sess_{task.id}_{arm}",
            passed=True,
            exit_code=0,
            wall_time=0.5,
            total_tokens=500,
            prompt_tokens=400,
            output_tokens=100,
            tool_calls=1,
            cost_usd=0.005,
            status="done",
        )

    mock_run_arm.side_effect = fake_arm_task

    tasks_file = tmp_path / "tasks.yaml"
    tasks_file.write_text("dummy: tasks")
    db_file = tmp_path / "suite.db"

    raw_results, report = run_bench_suite(
        tasks_path=tasks_file,
        repo_root=tmp_path,
        arms=["off", "tight"],
        repeats=2,
        tier="cheap",
        max_turns=10,
        cost_cap_usd=0.50,
        db_path=db_file,
    )

    # 2 tasks * 2 arms * 2 repeats = 8 calls
    assert mock_run_arm.call_count == 8
    assert len(raw_results) == 8
    assert "off" in report.arm_summaries
    assert "tight" in report.arm_summaries
    assert report.task_count == 2
    assert report.raw_results_count == 8


# =====================================================================
# 7. CLI Commands Tests: bp bench run, bp status, bp report
# =====================================================================

def test_cli_status_command(capsys, tmp_path):
    """Verify bp status displays calibration guarantees and historical savings."""
    db_file = tmp_path / "status.db"
    init_db(db_file)

    # Seed lambda_table with calibration guarantees using correct schema
    safe_execute_write(
        conn_or_path=db_file,
        sql="""
        INSERT INTO lambda_table (budget_slider, action, lambda_penalty, alpha_risk, metadata)
        VALUES ('tight', '*', 2.45, 0.30, '{"empirical_risk": 0.12, "n_rollouts": 100, "bound_satisfied": true}'),
               ('balanced', '*', 0.85, 0.20, '{"empirical_risk": 0.08, "n_rollouts": 100, "bound_satisfied": true}'),
               ('loose', '*', 0.15, 0.10, '{"empirical_risk": 0.03, "n_rollouts": 100, "bound_satisfied": true}');
        """,
    )

    # Seed sessions and avoided_estimates
    safe_execute_write(
        conn_or_path=db_file,
        sql="""
        INSERT INTO sessions (session_id, mode, status, total_cost, total_tokens)
        VALUES ('sess_1', 'enforce', 'done', 0.020, 2000),
               ('sess_2', 'enforce', 'done', 0.010, 1000);
        """,
    )
    safe_execute_write(
        conn_or_path=db_file,
        sql="""
        INSERT INTO avoided_estimates (session_id, mechanism, baseline_cost_usd, actual_cost_usd, saved_cost_usd, metadata)
        VALUES ('sess_1', 'gate_rewrite', 0.020, 0.005, 0.015, '{"avoided_low_usd": 0.010, "avoided_mid_usd": 0.015, "avoided_high_usd": 0.020, "override_count": 0}');
        """,
    )

    # 1. Terminal output
    code = run_status(output_json=False, db_path=db_file)
    out = capsys.readouterr().out
    assert code == 0
    assert "BudgetPilot System Status" in out
    assert "Conformal Calibration Guarantees" in out
    assert "tight" in out
    assert "balanced" in out
    assert "loose" in out
    assert "Historical Savings & Financials" in out

    # 2. JSON output
    code_json = run_status(output_json=True, db_path=db_file)
    out_json = capsys.readouterr().out
    assert code_json == 0
    status_data = json.loads(out_json)
    assert status_data["active_mode"] in ("enforce", "observe", "off")
    assert "historical_spend" in status_data
    assert pytest.approx(status_data["historical_spend"]["measured_spend_usd"], abs=1e-4) == 0.030
    assert pytest.approx(status_data["historical_spend"]["avoided_spend_mid_usd"], abs=1e-4) == 0.015
    assert len(status_data["calibration_guarantees"]) == 3


def test_cli_report_html_format(capsys, tmp_path):
    """Verify bp report --format html outputs valid HTML document."""
    db_file = tmp_path / "html_report.db"
    init_db(db_file)
    sess_id = "test_html_sess_01"

    safe_execute_write(
        conn_or_path=db_file,
        sql="""
        INSERT INTO sessions (session_id, mode, status, total_cost, total_tokens, budget_slider)
        VALUES (?, 'enforce', 'done', 0.015, 1500, 'tight');
        """,
        params=(sess_id,),
    )
    safe_execute_write(
        conn_or_path=db_file,
        sql="""
        INSERT INTO gate_events (session_id, tool_name, decision, reason)
        VALUES (?, 'read_file', 'rewrite', 'Oversized read rewritten');
        """,
        params=(sess_id,),
    )
    safe_execute_write(
        conn_or_path=db_file,
        sql="""
        INSERT INTO recovery_decisions (session_id, failure_class, action)
        VALUES (?, 'symbol_outside_touched', 'expand_scope');
        """,
        params=(sess_id,),
    )

    # Single session HTML
    code = run_report(session_id=sess_id, format_type="html", db_path=db_file)
    out = capsys.readouterr().out
    assert code == 0
    assert "<!DOCTYPE html>" in out
    assert f"<title>BudgetPilot Report: {sess_id}</title>" in out
    assert "Gate Interception Counts & Recovery Outcomes" in out

    # All sessions HTML
    code_all = run_report(session_id=None, format_type="html", db_path=db_file)
    out_all = capsys.readouterr().out
    assert code_all == 0
    assert "<!DOCTYPE html>" in out_all
    assert "<title>BudgetPilot All Sessions Report</title>" in out_all
    assert sess_id in out_all


@patch("budgetpilot.bench.runner.run_bench_suite")
def test_cli_bench_run_command(mock_suite, capsys, tmp_path):
    """Verify bp bench run invokes benchmark suite with parsed options."""
    mock_report = MagicMock()
    mock_report.to_terminal.return_value = "=== Benchmark Complete ==="
    mock_report.to_markdown.return_value = "# Benchmark Complete"
    mock_report.to_html.return_value = "<html>Benchmark Complete</html>"
    mock_suite.return_value = ([], mock_report)

    tasks_file = tmp_path / "tasks.yaml"
    tasks_file.write_text("tasks: []")

    exit_code = main([
        "bench", "run",
        "--tasks", str(tasks_file),
        "--arms", "off,tight,balanced",
        "--repeats", "2",
        "--max-turns", "15",
        "--cost-cap", "0.75",
        "--format", "terminal",
    ])

    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "=== Benchmark Complete ===" in captured

    mock_suite.assert_called_once()
    _, kwargs = mock_suite.call_args
    assert kwargs["arms"] == "off,tight,balanced"
    assert kwargs["repeats"] == 2
    assert kwargs["max_turns"] == 15
    assert kwargs["cost_cap_usd"] == 0.75


# =====================================================================
# 8. Architectural Invariant: Zero Forbidden Imports
# =====================================================================

def test_no_forbidden_imports_in_bench_subsystem():
    """
    Enforce architectural constraint:
    budgetpilot/bench/runner.py and budgetpilot/bench/analysis.py must NEVER
    import ollama, mcp, fastapi, or uvicorn.
    """
    forbidden = {"ollama", "mcp", "fastapi", "uvicorn"}
    repo_root = Path(__file__).resolve().parent.parent

    target_files = [
        repo_root / "budgetpilot" / "bench" / "runner.py",
        repo_root / "budgetpilot" / "bench" / "analysis.py",
    ]

    for file_path in target_files:
        assert file_path.exists(), f"File {file_path} must exist"
        tree = ast.parse(file_path.read_text(), filename=str(file_path))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_pkg = alias.name.split(".")[0]
                    assert top_pkg not in forbidden, f"Forbidden import '{top_pkg}' in {file_path.name}"
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top_pkg = node.module.split(".")[0]
                    assert top_pkg not in forbidden, f"Forbidden import-from '{top_pkg}' in {file_path.name}"

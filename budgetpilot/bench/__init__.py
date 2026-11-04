"""
BudgetPilot Benchmark Subsystem.
Phase C: Baseline ungated evaluation, worktree isolation, and benchmark reporting.
"""

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
from budgetpilot.bench.runner import (
    ArmRunResult,
    STANDARD_ARMS,
    arm_to_mode_and_slider,
    run_bench_arm_task,
    run_bench_suite,
)

__all__ = [
    "BenchmarkTask",
    "TaskRunResult",
    "BenchmarkSummary",
    "load_tasks",
    "create_worktree",
    "cleanup_worktree",
    "run_benchmark_task",
    "run_baseline_benchmark",
    "ArmRunResult",
    "STANDARD_ARMS",
    "arm_to_mode_and_slider",
    "run_bench_arm_task",
    "run_bench_suite",
    "ArmSummary",
    "BenchAnalysisReport",
    "ExpandScopeWinRate",
    "PairedDiffResult",
    "analyze_bench_results",
    "bootstrap_ci",
    "compute_expand_scope_win_rate",
    "compute_paired_cost_difference",
]

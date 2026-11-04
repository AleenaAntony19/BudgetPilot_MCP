"""
BudgetPilot Benchmark Suite Runner.
Phase L: Multi-arm execution engine with git worktree isolation,
enforced turn/cost caps, and integration with the analysis engine.

Command:
  `bp bench run --tasks tasks.yaml --arms off,observe,tight,balanced,loose --repeats 3`

Strict Rule: This module must NOT import ollama, mcp, fastapi, or uvicorn.
"""

from dataclasses import asdict, dataclass, field
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from budgetpilot.bench.analysis import BenchAnalysisReport, analyze_bench_results
from budgetpilot.bench.baseline import BenchmarkTask, cleanup_worktree, create_worktree, load_tasks
from budgetpilot.core.store import get_connection, init_db, log_fallback_error, safe_execute_write

logger = logging.getLogger(__name__)

# Valid and standard benchmark arms
STANDARD_ARMS: List[str] = ["off", "observe", "tight", "balanced", "loose"]


@dataclass
class ArmRunResult:
    """
    Execution outcome for a single (task, arm, repeat) benchmark evaluation trial.
    """
    task_id: str
    arm: str
    repeat: int
    session_id: str
    passed: bool
    exit_code: int
    wall_time: float
    total_tokens: int
    prompt_tokens: int
    output_tokens: int
    tool_calls: int
    cost_usd: float
    status: str
    error: Optional[str] = None
    verify_stdout: str = ""
    verify_stderr: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def arm_to_mode_and_slider(arm: str) -> Tuple[str, str]:
    """
    Map an arm name to (mode, budget_slider) settings.
    - 'off': ungated baseline (mode='off')
    - 'observe': passive observation (mode='observe')
    - 'tight' | 'balanced' | 'loose': active enforcement with corresponding slider
    """
    norm = str(arm).strip().lower()
    if norm == "off":
        return "off", "balanced"
    elif norm == "observe":
        return "observe", "balanced"
    elif norm in ("tight", "balanced", "loose"):
        return "enforce", norm
    else:
        # Default fallback: enforce with balanced
        return "enforce", norm


def run_bench_arm_task(
    task: BenchmarkTask,
    arm: str,
    repo_root: Union[Path, str],
    repeat: int = 1,
    tier: str = "cheap",
    max_turns: int = 20,
    cost_cap_usd: float = 1.0,
    timeout: int = 60,
    db_path: Optional[Union[Path, str]] = None,
) -> ArmRunResult:
    """
    Executes a single benchmark task under a specific arm within an isolated git worktree:
    1. Spawns fresh worktree at task.base_commit.
    2. Runs agent with enforced turn cap (max_turns) and cost cap (cost_cap_usd).
    3. Runs verify_command in worktree (hidden from agent).
    4. Records result into SQLite sessions and reports tables.
    5. Cleanly prunes and deletes worktree in a guaranteed finally block.
    """
    root = Path(repo_root).expanduser().resolve()
    init_db(db_path)

    mode, budget_slider = arm_to_mode_and_slider(arm)
    session_id = f"bench_{task.id}_{arm}_r{repeat}_{int(time.time() * 1000)}"
    t_start = time.time()

    worktree_path = create_worktree(root, base_commit=task.base_commit)

    try:
        from budgetpilot.agent.loop import AgentLoopResult, run_agent_loop

        # 1. Run agent loop with enforced turn and cost caps
        loop_res: AgentLoopResult = run_agent_loop(
            prompt=task.prompt,
            repo_path=str(worktree_path),
            mode=mode,
            max_turns=max_turns,
            session_id=session_id,
            db_path=db_path,
            tier=tier,
            cost_cap_usd=cost_cap_usd,
            budget_slider=budget_slider,
        )

        # 2. Run verify_command inside isolated worktree
        v_cmd = task.verify_command
        if not shutil.which("python") and re.match(r"^python\b", v_cmd):
            v_cmd = re.sub(r"^python\b", sys.executable, v_cmd)

        try:
            verify_res = subprocess.run(
                v_cmd,
                cwd=str(worktree_path),
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            exit_code = verify_res.returncode
            verify_stdout = verify_res.stdout or ""
            verify_stderr = verify_res.stderr or ""
            passed = (exit_code == 0)
        except subprocess.TimeoutExpired as te:
            exit_code = 124
            verify_stdout = te.stdout or "" if isinstance(te.stdout, str) else ""
            verify_stderr = f"Verification timed out after {timeout} seconds"
            passed = False
        except Exception as ve:
            exit_code = 1
            verify_stdout = ""
            verify_stderr = f"Verification execution error: {ve}"
            passed = False

        wall_time = time.time() - t_start

        # 3. Record metadata
        meta = {
            "benchmark": "multi_arm",
            "task_id": task.id,
            "arm": arm,
            "mode": mode,
            "budget_slider": budget_slider,
            "repeat": repeat,
            "passed": passed,
            "exit_code": exit_code,
            "wall_time": round(wall_time, 4),
            "tool_calls": loop_res.tool_calls_count,
            "cost_cap_usd": cost_cap_usd,
            "max_turns": max_turns,
            "status": loop_res.status,
        }

        # Update session
        safe_execute_write(
            conn_or_path=db_path,
            sql="""
            UPDATE sessions
            SET total_cost = ?, total_tokens = ?, status = ?, budget_slider = ?, metadata = ?
            WHERE session_id = ?;
            """,
            params=(
                loop_res.total_cost_usd,
                loop_res.total_tokens,
                "done" if passed else "failed",
                budget_slider,
                json.dumps(meta),
                session_id,
            ),
        )

        # Insert report row
        summary_text = (
            f"Benchmark Arm '{arm}': Task '{task.id}' repeat {repeat} "
            f"[{'PASSED' if passed else 'FAILED'}] "
            f"(time: {wall_time:.2f}s, tokens: {loop_res.total_tokens}, cost: ${loop_res.total_cost_usd:.6f})"
        )
        safe_execute_write(
            conn_or_path=db_path,
            sql="""
            INSERT INTO reports (session_id, report_type, summary, total_cost_usd, metadata)
            VALUES (?, 'bench_arm', ?, ?, ?);
            """,
            params=(
                session_id,
                summary_text,
                loop_res.total_cost_usd,
                json.dumps(meta),
            ),
        )

        return ArmRunResult(
            task_id=task.id,
            arm=arm,
            repeat=repeat,
            session_id=session_id,
            passed=passed,
            exit_code=exit_code,
            wall_time=wall_time,
            total_tokens=loop_res.total_tokens,
            prompt_tokens=loop_res.prompt_tokens,
            output_tokens=loop_res.output_tokens,
            tool_calls=loop_res.tool_calls_count,
            cost_usd=loop_res.total_cost_usd,
            status=loop_res.status,
            error=loop_res.error,
            verify_stdout=verify_stdout,
            verify_stderr=verify_stderr,
            metadata=meta,
        )

    finally:
        # 5. Clean up git worktree
        cleanup_worktree(root, worktree_path)


def run_bench_suite(
    tasks_path: Union[Path, str],
    arms: Union[str, Sequence[str]] = "off,observe,tight,balanced,loose",
    repeats: int = 3,
    repo_root: Union[Path, str] = ".",
    max_turns: int = 20,
    cost_cap_usd: float = 1.0,
    timeout: int = 60,
    db_path: Optional[Union[Path, str]] = None,
    tier: str = "cheap",
    baseline_arm: str = "off",
) -> Tuple[List[ArmRunResult], BenchAnalysisReport]:
    """
    Executes the full multi-arm benchmark suite:
    1. Loads benchmark tasks from tasks_path.
    2. Runs each (task, repeat, arm) in an isolated git worktree with enforced turn/cost caps.
    3. Analyzes results: paired differences, bootstrap CIs, and expand-scope win rates.
    4. Returns (raw_results, analysis_report).
    """
    # Parse arms
    if isinstance(arms, str):
        arms_list = [a.strip().lower() for a in arms.split(",") if a.strip()]
    else:
        arms_list = [str(a).strip().lower() for a in arms if str(a).strip()]

    if not arms_list:
        arms_list = list(STANDARD_ARMS)

    tasks = load_tasks(tasks_path)
    if not tasks:
        raise ValueError(f"No tasks found in {tasks_path}")

    root = Path(repo_root).expanduser().resolve()
    raw_results: List[ArmRunResult] = []

    logger.info(
        "Starting benchmark run: %d tasks x %d repeats x %d arms (%s)",
        len(tasks), repeats, len(arms_list), ", ".join(arms_list),
    )

    for task in tasks:
        for r in range(1, repeats + 1):
            for arm in arms_list:
                logger.info("Executing task '%s' [arm=%s, repeat=%d/%d]", task.id, arm, r, repeats)
                result = run_bench_arm_task(
                    task=task,
                    arm=arm,
                    repo_root=root,
                    repeat=r,
                    tier=tier,
                    max_turns=max_turns,
                    cost_cap_usd=cost_cap_usd,
                    timeout=timeout,
                    db_path=db_path,
                )
                raw_results.append(result)

    # Perform statistical analysis
    report = analyze_bench_results(
        results=raw_results,
        baseline_arm=baseline_arm,
        db_path=db_path,
    )

    return raw_results, report

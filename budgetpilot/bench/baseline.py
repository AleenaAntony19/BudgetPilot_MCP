"""
BudgetPilot Baseline Benchmark Runner.
Phase C: Ungated (mode=off) evaluation, git worktree sandboxing, verification, and SQLite reporting.
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
from typing import Any, Dict, List, Optional, Union
import uuid
import yaml

from budgetpilot.config import get_bp_home, load_config
from budgetpilot.core.store import get_connection, init_db, log_fallback_error, safe_execute_write

logger = logging.getLogger(__name__)


@dataclass
class BenchmarkTask:
    """
    Specification for a benchmark task from tasks.yaml.
    """
    id: str
    prompt: str
    verify_command: str
    base_commit: str = "HEAD"
    expected_scope: int = 1

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("BenchmarkTask requires a non-empty 'id'")
        if not self.prompt:
            raise ValueError("BenchmarkTask requires a non-empty 'prompt'")
        if not self.verify_command:
            raise ValueError("BenchmarkTask requires a non-empty 'verify_command'")
        if self.expected_scope not in (1, 2, 3):
            raise ValueError(f"expected_scope must be 1, 2, or 3, got: {self.expected_scope}")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TaskRunResult:
    """
    Outcome of an individual benchmark task execution.
    """
    task_id: str
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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BenchmarkSummary:
    """
    Aggregated outcome across all benchmark task executions.
    """
    total_tasks: int
    total_runs: int
    passed_runs: int
    failed_runs: int
    pass_rate: float
    total_tokens: int
    total_cost_usd: float
    total_wall_time: float
    results: List[TaskRunResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_tasks": self.total_tasks,
            "total_runs": self.total_runs,
            "passed_runs": self.passed_runs,
            "failed_runs": self.failed_runs,
            "pass_rate": self.pass_rate,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "total_wall_time": self.total_wall_time,
            "results": [r.to_dict() for r in self.results],
        }


def load_tasks(tasks_path: Union[Path, str]) -> List[BenchmarkTask]:
    """
    Parse and validate benchmark tasks from a tasks.yaml file.
    Supports either a top-level list of tasks or a dictionary with a 'tasks' key.
    """
    resolved_path = Path(tasks_path).expanduser().resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"Benchmark tasks file not found: {resolved_path}")

    with open(resolved_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data:
        return []

    raw_list: List[Any] = []
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        if "tasks" in data and isinstance(data["tasks"], list):
            raw_list = data["tasks"]
        else:
            raise ValueError(f"tasks.yaml must contain a list of tasks or a 'tasks' key, got keys: {list(data.keys())}")
    else:
        raise ValueError(f"Invalid tasks.yaml format; expected list or dict, got: {type(data).__name__}")

    tasks: List[BenchmarkTask] = []
    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            raise ValueError(f"Task index {idx} must be a dictionary, got: {type(item).__name__}")

        task = BenchmarkTask(
            id=str(item.get("id", "")).strip(),
            prompt=str(item.get("prompt", "")).strip(),
            verify_command=str(item.get("verify_command", "")).strip(),
            base_commit=str(item.get("base_commit", "HEAD")).strip() or "HEAD",
            expected_scope=int(item.get("expected_scope", 1)),
        )
        tasks.append(task)

    return tasks


def create_worktree(
    repo_root: Union[Path, str],
    base_commit: str = "HEAD",
    worktree_path: Optional[Union[Path, str]] = None,
) -> Path:
    """
    Create an isolated git worktree checked out at base_commit.
    """
    root = Path(repo_root).expanduser().resolve()
    if not (root / ".git").exists():
        raise ValueError(f"Directory {root} is not a valid git repository (.git missing).")

    if worktree_path is None:
        bp_wt_dir = get_bp_home() / "worktrees"
        bp_wt_dir.mkdir(parents=True, exist_ok=True)
        wt = bp_wt_dir / f"wt_{uuid.uuid4().hex[:12]}"
    else:
        wt = Path(worktree_path).expanduser().resolve()

    cmd = ["git", "worktree", "add", "--detach", str(wt), str(base_commit)]
    res = subprocess.run(
        cmd,
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"Failed to create git worktree at {wt} (base_commit={base_commit}):\n{res.stderr.strip()}"
        )

    return wt


def cleanup_worktree(
    repo_root: Union[Path, str],
    worktree_path: Union[Path, str],
) -> None:
    """
    Safely remove a git worktree and prune dangling worktree references.
    Safe to call in finally blocks even if creation was aborted or directory was deleted.
    """
    root = Path(repo_root).expanduser().resolve()
    wt = Path(worktree_path).expanduser().resolve()

    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt)],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        logger.debug("git worktree remove failed: %s", exc)

    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        logger.debug("git worktree prune failed: %s", exc)

    if wt.exists():
        try:
            shutil.rmtree(wt, ignore_errors=True)
        except Exception as exc:
            logger.debug("Failed to remove worktree directory %s: %s", wt, exc)


def run_benchmark_task(
    task: BenchmarkTask,
    repo_root: Union[Path, str],
    repeat: int = 1,
    tier: str = "cheap",
    max_turns: int = 20,
    timeout: int = 60,
    db_path: Optional[Union[Path, str]] = None,
) -> TaskRunResult:
    """
    Executes a single benchmark task within an isolated git worktree:
    1. Spawns fresh worktree at task.base_commit.
    2. Runs agent ungated (mode='off').
    3. Runs verify_command in worktree (hidden from agent).
    4. Records pass/fail, tokens, cost, and wall time to SQLite.
    5. Cleanly prunes worktree in finally block.
    """
    root = Path(repo_root).expanduser().resolve()
    init_db(db_path)

    session_id = f"bench_{task.id}_r{repeat}_{int(time.time() * 1000)}"
    t_start = time.time()

    worktree_path = create_worktree(root, base_commit=task.base_commit)

    try:
        from budgetpilot.agent.loop import AgentLoopResult, run_agent_loop

        # 1. Run the agent ungated (mode=off) inside the isolated worktree
        loop_res: AgentLoopResult = run_agent_loop(
            prompt=task.prompt,
            repo_path=str(worktree_path),
            mode="off",
            max_turns=max_turns,
            session_id=session_id,
            db_path=db_path,
            tier=tier,
        )

        # 2. Run verify_command in worktree (hidden from agent)
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

        # 3. Store results in SQLite (sessions, reports, usage)
        task_meta = {
            "benchmark": "baseline",
            "task_id": task.id,
            "repeat": repeat,
            "expected_scope": task.expected_scope,
            "passed": passed,
            "exit_code": exit_code,
            "wall_time": round(wall_time, 4),
            "tool_calls": loop_res.tool_calls_count,
        }

        # Update sessions record
        safe_execute_write(
            conn_or_path=db_path,
            sql="""
            UPDATE sessions
            SET total_cost = ?, total_tokens = ?, status = ?, metadata = ?
            WHERE session_id = ?;
            """,
            params=(
                loop_res.total_cost_usd,
                loop_res.total_tokens,
                "done" if passed else "failed",
                json.dumps(task_meta),
                session_id,
            ),
        )

        # Insert into reports
        summary_text = (
            f"Benchmark Baseline: Task '{task.id}' repeat {repeat} "
            f"[{'PASSED' if passed else 'FAILED'}] "
            f"(time: {wall_time:.2f}s, tokens: {loop_res.total_tokens}, cost: ${loop_res.total_cost_usd:.6f})"
        )
        report_meta = {
            "task_id": task.id,
            "repeat": repeat,
            "passed": passed,
            "exit_code": exit_code,
            "wall_time": round(wall_time, 4),
            "verify_stdout": verify_stdout[:2000],
            "verify_stderr": verify_stderr[:2000],
            "tool_calls": loop_res.tool_calls_count,
            "prompt_tokens": loop_res.prompt_tokens,
            "output_tokens": loop_res.output_tokens,
            "total_tokens": loop_res.total_tokens,
            "cost_usd": loop_res.total_cost_usd,
        }
        safe_execute_write(
            conn_or_path=db_path,
            sql="""
            INSERT INTO reports (session_id, report_type, summary, total_cost_usd, savings_usd, metadata)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            params=(
                session_id,
                "benchmark_baseline",
                summary_text,
                loop_res.total_cost_usd,
                0.0,
                json.dumps(report_meta),
            ),
        )

        # Insert into usage
        usage_meta = {
            "task_id": task.id,
            "repeat": repeat,
            "benchmark": "baseline",
            "passed": passed,
        }
        safe_execute_write(
            conn_or_path=db_path,
            sql="""
            INSERT INTO usage (session_id, tier, model, tokens_in, tokens_out, cost_usd, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            params=(
                session_id,
                tier,
                "default",
                loop_res.prompt_tokens,
                loop_res.output_tokens,
                loop_res.total_cost_usd,
                json.dumps(usage_meta),
            ),
        )

        return TaskRunResult(
            task_id=task.id,
            repeat=repeat,
            session_id=session_id,
            passed=passed,
            exit_code=exit_code,
            wall_time=round(wall_time, 4),
            total_tokens=loop_res.total_tokens,
            prompt_tokens=loop_res.prompt_tokens,
            output_tokens=loop_res.output_tokens,
            tool_calls=loop_res.tool_calls_count,
            cost_usd=loop_res.total_cost_usd,
            status=loop_res.status,
            error=loop_res.error,
            verify_stdout=verify_stdout,
            verify_stderr=verify_stderr,
        )

    finally:
        # Guarantee worktree cleanup
        cleanup_worktree(root, worktree_path)


def run_baseline_benchmark(
    repo_path: Union[Path, str] = ".",
    tasks_file: Union[Path, str] = "tasks.yaml",
    repeats: int = 1,
    tier: str = "cheap",
    max_turns: int = 20,
    timeout: int = 60,
    db_path: Optional[Union[Path, str]] = None,
) -> BenchmarkSummary:
    """
    Run the full ungated baseline benchmark suite across all tasks and repetitions.
    """
    repo_root = Path(repo_path).expanduser().resolve()
    tasks = load_tasks(tasks_file)

    if not tasks:
        print(f"No tasks found in {tasks_file}.")
        return BenchmarkSummary(
            total_tasks=0,
            total_runs=0,
            passed_runs=0,
            failed_runs=0,
            pass_rate=0.0,
            total_tokens=0,
            total_cost_usd=0.0,
            total_wall_time=0.0,
            results=[],
        )

    print("=" * 80)
    print(" BudgetPilot Baseline Benchmark (Ungated mode=off)")
    print(f" Repository:  {repo_root}")
    print(f" Tasks File:  {tasks_file} ({len(tasks)} tasks)")
    print(f" Repeats:     {repeats}")
    print(f" Model Tier:  {tier}")
    print("=" * 80)
    print()

    header = f"{'Task ID':<20} {'Repeat':<8} {'Status':<8} {'Exit':<6} {'Wall Time':<10} {'Tools':<7} {'Tokens':<8} {'Cost USD':<10}"
    print(header)
    print("-" * 80)

    results: List[TaskRunResult] = []
    bench_start = time.time()

    for task in tasks:
        for r in range(1, repeats + 1):
            run_res = run_benchmark_task(
                task=task,
                repo_root=repo_root,
                repeat=r,
                tier=tier,
                max_turns=max_turns,
                timeout=timeout,
                db_path=db_path,
            )
            results.append(run_res)

            status_str = "PASS" if run_res.passed else "FAIL"
            row = (
                f"{run_res.task_id:<20} "
                f"{f'{r}/{repeats}':<8} "
                f"{status_str:<8} "
                f"{run_res.exit_code:<6} "
                f"{f'{run_res.wall_time:.2f}s':<10} "
                f"{run_res.tool_calls:<7} "
                f"{run_res.total_tokens:<8} "
                f"${run_res.cost_usd:<10.6f}"
            )
            print(row)

    total_bench_time = time.time() - bench_start
    total_runs = len(results)
    passed_runs = sum(1 for r in results if r.passed)
    failed_runs = total_runs - passed_runs
    pass_rate = (passed_runs / total_runs * 100.0) if total_runs > 0 else 0.0
    total_tokens = sum(r.total_tokens for r in results)
    total_cost = sum(r.cost_usd for r in results)

    print("-" * 80)
    print(" Benchmark Summary:")
    print(f"   Total Tasks:   {len(tasks)}")
    print(f"   Total Runs:    {total_runs}")
    print(f"   Passed Runs:   {passed_runs}")
    print(f"   Failed Runs:   {failed_runs}")
    print(f"   Pass Rate:     {pass_rate:.1f}%")
    print(f"   Total Tokens:  {total_tokens}")
    print(f"   Total Spend:   $ {total_cost:.6f} USD")
    print(f"   Total Time:    {total_bench_time:.2f}s")
    print("=" * 80)

    return BenchmarkSummary(
        total_tasks=len(tasks),
        total_runs=total_runs,
        passed_runs=passed_runs,
        failed_runs=failed_runs,
        pass_rate=round(pass_rate, 2),
        total_tokens=total_tokens,
        total_cost_usd=round(total_cost, 6),
        total_wall_time=round(total_bench_time, 4),
        results=results,
    )

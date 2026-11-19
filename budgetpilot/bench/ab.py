"""
Paired A/B test of BudgetPilot on real Claude Code sessions (`bp ab`).

For every (task, repeat) the same prompt is run headless twice, in random
order, on a fresh copy of the fixture repository:

    observe  - hooks installed, full token ledger, zero intervention (baseline)
    enforce  - hooks installed and acting

Same model in both arms, a fresh session per run, `BP_HITL=0`. Success is
decided by a hidden test the agent never sees. The cost of a run is its
billing units, computed from the session transcript (see core/cc_ledger.py).

The report gives the paired difference with a bootstrap 95 % confidence
interval next to the pass rate of both arms: a saving that lowers the pass
rate is not a saving. This is the only source of a measured savings figure
in BudgetPilot.

Not related to the legacy Ollama benchmark runners in this package.
"""

import glob
import json
import os
from pathlib import Path
import random
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional

from budgetpilot import installer
from budgetpilot.bench.analysis import bootstrap_ci, compute_paired_cost_difference
from budgetpilot.core import cc_ledger

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TASKS = REPO_ROOT / "bench" / "ab_tasks.yaml"
DEFAULT_FIXTURE = REPO_ROOT / "bench" / "fixture_repo"
ALLOWED_TOOLS = "Read,Edit,Write,Grep,Glob,Bash(pytest *),Bash(python -m pytest *)"
ARMS = ("observe", "enforce")
BASELINE = "observe"


# ------------------------------------------------------------------ tasks
def load_tasks(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    import yaml

    source = Path(path) if path else DEFAULT_TASKS
    if not source.is_file():
        raise FileNotFoundError(
            f"task file not found: {source}. Run from a BudgetPilot checkout, or pass --tasks and --fixture."
        )
    tasks = yaml.safe_load(source.read_text(encoding="utf-8")) or []
    for task in tasks:
        for key in ("id", "prompt", "verify_command"):
            if not task.get(key):
                raise ValueError(f"task is missing '{key}': {task}")
        task["_dir"] = str(source.parent)
    return tasks


def select_tasks(tasks: List[Dict[str, Any]], ids: Optional[List[str]] = None, smoke: bool = False,
                 held_out: Optional[bool] = None) -> List[Dict[str, Any]]:
    chosen = tasks
    if ids:
        missing = set(ids) - {t["id"] for t in tasks}
        if missing:
            raise ValueError(f"unknown task id(s): {sorted(missing)}")
        chosen = [t for t in chosen if t["id"] in ids]
    if smoke:
        chosen = [t for t in chosen if t.get("smoke")]
    if held_out is not None:
        chosen = [t for t in chosen if bool(t.get("held_out")) == held_out]
    return chosen


# ------------------------------------------------------------------ one run
def prepare_workdir(fixture: Path, dest: Path) -> Path:
    """Fresh copy of the fixture as its own git repository, with project-scope hooks."""
    shutil.copytree(fixture, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", "_hidden"))
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "bp-ab", "GIT_AUTHOR_EMAIL": "bp-ab@localhost",
               "GIT_COMMITTER_NAME": "bp-ab", "GIT_COMMITTER_EMAIL": "bp-ab@localhost"}
    for command in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "fixture"]):
        subprocess.run(command, cwd=dest, check=True, capture_output=True, env=git_env)
    installer.register(installer.HOOK_MODULE, installer.HOOK_EVENTS, scope="project", repo=dest)
    return dest


def child_env(mode: str, bp_home: Path) -> Dict[str, str]:
    """Environment for the headless session: plan login, no API key, no parent-session variables."""
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CLAUDE") and k != "CLAUDE_CONFIG_DIR") and not k.startswith("BP_")
           and k != "ANTHROPIC_API_KEY"}
    env.update(BP_MODE=mode, BP_HITL="0", BP_HOME=str(bp_home))
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    return env


def run_claude(claude: str, prompt: str, cwd: Path, env: Dict[str, str], model: str, timeout: int) -> Dict[str, Any]:
    command = [claude, "-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits",
               "--allowedTools", ALLOWED_TOOLS, "--model", model]
    started = time.time()
    try:
        proc = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
        wall = time.time() - started
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            return {"error": f"no JSON result (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-300:]}",
                    "wall_s": wall}
        data["wall_s"] = wall
        return data
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {timeout}s", "wall_s": time.time() - started}
    except OSError as exc:
        return {"error": str(exc), "wall_s": time.time() - started}


def find_transcript(session_id: str) -> Optional[str]:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    found = glob.glob(str(base / "*" / f"{session_id}.jsonl"))
    return found[0] if found else None


def verify(task: Dict[str, Any], workdir: Path, timeout: int = 180) -> bool:
    """Copy the hidden test in, then run the verify command. True when it exits 0."""
    hidden = task.get("hidden_test")
    if hidden:
        source = Path(task["_dir"]) / hidden
        target = workdir / "_hidden"
        target.mkdir(exist_ok=True)
        shutil.copy(source, target / source.name)
    command = shlex.split(task["verify_command"])
    if command and command[0] in ("python", "python3"):
        command[0] = sys.executable
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BP_", "CLAUDE"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(command, cwd=workdir, capture_output=True, text=True, timeout=timeout, env=env)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def hook_stats(bp_home: Path) -> Dict[str, Any]:
    """What the hooks recorded during one run (its own temporary database)."""
    stats: Dict[str, Any] = {"gate": {}, "overhead_tokens_est": 0, "avoided_tokens_est": 0, "trimmed_tokens_est": 0,
                             "scope": None, "hook_errors": False}
    db = bp_home / "bp.db"
    stats["hook_errors"] = (bp_home / "hook_errors.log").exists()
    if not db.exists():
        return stats
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        for rule, verdict, count in conn.execute("SELECT rule, verdict, COUNT(*) FROM gate_events GROUP BY rule, verdict"):
            if verdict != "allow":
                stats["gate"][f"{verdict}:{rule}"] = count
        stats["avoided_tokens_est"] = conn.execute(
            "SELECT COALESCE(SUM(tokens_avoided_est),0) FROM gate_events WHERE verdict='rewrite'").fetchone()[0]
        stats["trimmed_tokens_est"] = conn.execute(
            "SELECT COALESCE(SUM(trimmed_tokens_est),0) FROM tool_obs WHERE truncated=1").fetchone()[0]
        row = conn.execute("SELECT COALESCE(SUM(overhead_tokens_est),0), MAX(scope) FROM tasks_bp").fetchone()
        stats["overhead_tokens_est"], stats["scope"] = row[0], row[1]
        conn.close()
    except sqlite3.Error:
        pass
    return stats


def run_one(claude: str, task: Dict[str, Any], arm: str, repeat: int, fixture: Path, model: str,
            timeout: int, keep_dir: Optional[Path] = None) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="bp-ab-") as tmp:
        workdir = prepare_workdir(fixture, Path(tmp) / "repo")
        bp_home = Path(tmp) / "bp_home"
        bp_home.mkdir()
        result = run_claude(claude, task["prompt"], workdir, child_env(arm, bp_home), model, timeout)
        run: Dict[str, Any] = {
            "task_id": task["id"], "arm": arm, "repeat": repeat, "expected_scope": task.get("expected_scope"),
            "held_out": bool(task.get("held_out")), "wall_s": round(result.get("wall_s", 0.0), 1),
            "error": result.get("error"), "session_id": result.get("session_id"),
            "total_cost_usd_reported": result.get("total_cost_usd"), "reported_turns": result.get("num_turns"),
        }
        run["passed"] = False if result.get("error") else verify(task, workdir)
        transcript = find_transcript(run["session_id"]) if run["session_id"] else None
        if transcript:
            totals = cc_ledger.summarize_transcript(transcript)
            run.update(
                billing_units=round(totals["billing_units"], 1), ref_usd=round(totals["ref_usd"], 6),
                in_tok=totals["input_tokens"], cache_w=totals["cache_write"], cache_r=totals["cache_read"],
                out_tok=totals["output_tokens"], thinking_tok=totals["thinking_tokens"], turns=totals["turns"],
                cache_hit_ratio=totals["cache_hit_ratio"], price_guess=totals["price_guess"],
            )
            usage = result.get("usage") or {}
            reported = {"in_tok": usage.get("input_tokens"), "cache_w": usage.get("cache_creation_input_tokens"),
                        "cache_r": usage.get("cache_read_input_tokens"), "out_tok": usage.get("output_tokens")}
            run["usage_discrepancy"] = {k: (run[k] - v) for k, v in reported.items() if isinstance(v, (int, float))}
        elif not run["error"]:
            run["error"] = "transcript not found"
        run["hooks"] = hook_stats(bp_home)
        if keep_dir is not None:
            dest = keep_dir / f"{task['id']}_{arm}_{repeat}"
            shutil.copytree(bp_home, dest, dirs_exist_ok=True)
        return run


# ------------------------------------------------------------------ experiment
def run_experiment(tasks: List[Dict[str, Any]], claude: str, repeats: int = 2, model: str = "haiku",
                   arms: Optional[List[str]] = None, fixture: Optional[Path] = None, seed: int = 0,
                   timeout: int = 600, out: Callable[[str], None] = print,
                   runner: Optional[Callable[..., Dict[str, Any]]] = None) -> Dict[str, Any]:
    arms = list(arms or ARMS)
    fixture = Path(fixture) if fixture else DEFAULT_FIXTURE
    experiment = time.strftime("ab_%Y%m%d_%H%M%S")
    plan = [(task, repeat, arm) for task in tasks for repeat in range(repeats) for arm in arms]
    random.Random(seed).shuffle(plan)
    runner = runner or run_one
    runs = []
    out(f"{experiment}: {len(plan)} runs ({len(tasks)} tasks x {repeats} repeats x {len(arms)} arms), model {model}")
    for index, (task, repeat, arm) in enumerate(plan, start=1):
        run = runner(claude, task, arm, repeat, fixture, model, timeout)
        run["pair_id"] = f"{experiment}:{task['id']}:{repeat}"
        run["run_id"] = f"{run['pair_id']}:{arm}"
        runs.append(run)
        status = "error: " + str(run["error"])[:80] if run.get("error") else ("pass" if run["passed"] else "FAIL")
        out(f"  [{index}/{len(plan)}] {task['id']:<26} {arm:<8} rep {repeat}  {status:<6} "
            f"{run.get('billing_units', 0):>10,.0f} units  {run.get('turns', 0):>3} turns  {run['wall_s']:.0f}s")
    result = {
        "experiment": experiment, "model": model, "repeats": repeats, "arms": arms, "seed": seed,
        "tasks": [{k: v for k, v in t.items() if not k.startswith("_")} for t in tasks],
        "claude_version": _claude_version(claude), "allowed_tools": ALLOWED_TOOLS,
        "runs": runs, "scope_limits": _scope_limits(), "billing": run_billing(),
    }
    result["analysis"] = analyse(runs)
    return result


def _claude_version(claude: str) -> str:
    try:
        return subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _scope_limits() -> Dict[str, Any]:
    from budgetpilot.core.scope import limits_table

    return limits_table()


def store_runs(result: Dict[str, Any]) -> None:
    """Save runs to `ab_runs` so the dashboard can show the measured figure."""
    from budgetpilot.core.store import init_db

    conn = init_db()
    try:
        for run in result["runs"]:
            if run.get("billing_units") is None:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO ab_runs (run_id, pair_id, arm, task_id, repeat, passed, billing_units, ref_usd, "
                "total_cost_usd_reported, in_tok, cache_w, cache_r, out_tok, turns, wall_s, ts, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run["run_id"], run["pair_id"], run["arm"], run["task_id"], run["repeat"], int(run["passed"]),
                 run["billing_units"], run.get("ref_usd"), run.get("total_cost_usd_reported"), run.get("in_tok"),
                 run.get("cache_w"), run.get("cache_r"), run.get("out_tok"), run.get("turns"), run.get("wall_s"),
                 time.time(), json.dumps({"model": result["model"], "hooks": run.get("hooks")})),
            )
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ analysis
def _mean(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def analyse(runs: List[Dict[str, Any]], treatment: str = "enforce") -> Dict[str, Any]:
    usable = [r for r in runs if r.get("billing_units") is not None]
    pairs: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for run in usable:
        pairs.setdefault(run["pair_id"], {})[run["arm"]] = run
    complete = {pid: p for pid, p in pairs.items() if BASELINE in p and treatment in p and p[BASELINE]["billing_units"]}

    absolute = [p[treatment]["billing_units"] - p[BASELINE]["billing_units"] for p in complete.values()]
    relative = [(p[treatment]["billing_units"] - p[BASELINE]["billing_units"]) / p[BASELINE]["billing_units"]
                for p in complete.values()]
    mean_abs, abs_low, abs_high = bootstrap_ci(absolute, n_resamples=5000, seed=0)
    mean_rel, rel_low, rel_high = bootstrap_ci(relative, n_resamples=5000, seed=0)

    def arm_stats(arm: str, subset: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        rows = [r for r in (subset if subset is not None else runs) if r["arm"] == arm]
        measured = [r for r in rows if r.get("billing_units") is not None]
        return {
            "runs": len(rows), "passed": sum(1 for r in rows if r.get("passed")),
            "errors": sum(1 for r in rows if r.get("error")),
            "mean_billing_units": _mean([r["billing_units"] for r in measured]),
            "mean_turns": _mean([r["turns"] for r in measured]),
            "mean_output_tokens": _mean([r["out_tok"] for r in measured]),
            "mean_cache_hit_ratio": _mean([r["cache_hit_ratio"] for r in measured if r.get("cache_hit_ratio") is not None]),
            "mean_overhead_tokens_est": _mean([r["hooks"]["overhead_tokens_est"] for r in measured if r.get("hooks")]),
            "mean_wall_s": _mean([r["wall_s"] for r in rows]),
        }

    arms = sorted({r["arm"] for r in runs})
    per_task = {}
    for task_id in sorted({r["task_id"] for r in runs}):
        subset = [r for r in runs if r["task_id"] == task_id]
        task_pairs = [p for pid, p in complete.items() if p[BASELINE]["task_id"] == task_id]
        rel = [(p[treatment]["billing_units"] - p[BASELINE]["billing_units"]) / p[BASELINE]["billing_units"]
               for p in task_pairs]
        per_task[task_id] = {
            "held_out": any(r.get("held_out") for r in subset), "pairs": len(task_pairs),
            "mean_change_pct": round(100 * _mean(rel), 1) if rel else None,
            **{arm: arm_stats(arm, subset) for arm in arms},
        }

    gate: Dict[str, int] = {}
    baseline_would: Dict[str, int] = {}
    for run in runs:
        target = gate if run["arm"] == treatment else baseline_would if run["arm"] == BASELINE else None
        if target is not None:
            for key, count in ((run.get("hooks") or {}).get("gate") or {}).items():
                target[key] = target.get(key, 0) + count

    equal_status = compute_paired_cost_difference(
        [{"task_id": r["task_id"], "repeat": r["repeat"], "arm": r["arm"], "passed": r["passed"],
          "cost_usd": r["billing_units"]} for r in usable],
        baseline_arm=BASELINE, n_resamples=5000, seed=0,
    ).get(treatment)

    base, treat = arm_stats(BASELINE), arm_stats(treatment)
    n = len(complete)
    if n < 2:
        verdict = "inconclusive: fewer than two complete pairs"
    elif treat["passed"] < base["passed"]:
        verdict = "not a saving: the pass rate is lower with BudgetPilot on"
    elif rel_high < 0:
        verdict = "saving: the whole 95% interval is below zero and the pass rate did not drop"
    elif rel_low > 0:
        verdict = "no saving: BudgetPilot increased cost (the whole 95% interval is above zero)"
    else:
        verdict = "inconclusive: the 95% interval includes zero"

    discrepancies = [abs(v) for r in usable for v in (r.get("usage_discrepancy") or {}).values()]
    return {
        "pairs": n, "treatment": treatment, "baseline": BASELINE, "verdict": verdict,
        "mean_change_pct": round(100 * mean_rel, 1) if n else None,
        "ci_low_pct": round(100 * rel_low, 1) if n else None, "ci_high_pct": round(100 * rel_high, 1) if n else None,
        "mean_change_units": round(mean_abs) if n else None,
        "ci_low_units": round(abs_low) if n else None, "ci_high_units": round(abs_high) if n else None,
        "arms": {arm: arm_stats(arm) for arm in arms}, "per_task": per_task, "gate_rules": gate,
        "baseline_would_rules": baseline_would,
        "equal_status_pairs": equal_status.n_equal_status if equal_status else 0,
        "equal_status_mean_saving_units": round(equal_status.mean_paired_savings_usd) if equal_status else None,
        "equal_status_ci_units": [round(equal_status.ci_lower), round(equal_status.ci_upper)] if equal_status else None,
        "max_usage_discrepancy_tokens": max(discrepancies) if discrepancies else None,
        "hook_errors": sum(1 for r in runs if (r.get("hooks") or {}).get("hook_errors")),
    }


# ------------------------------------------------------------------ report
def _n(value: Any, digits: int = 0) -> str:
    if value is None:
        return "–"
    return f"{value:,.{digits}f}"


def ab_policy(result: Dict[str, Any]) -> Dict[str, Any]:
    """Display policy for an experiment, from the billing recorded with it (unknown for older results)."""
    from budgetpilot.hosts import get_adapter
    from budgetpilot.hosts.base import BillingContext
    from budgetpilot.hosts.policy import report_policy

    from budgetpilot.core.canary import compare_runs

    b = result.get("billing") or {}
    billing = BillingContext(b.get("mode", "unknown"), b.get("provider", "unknown"), b.get("confidence", "low"),
                             tuple(b.get("evidence") or ()), b.get("source", "detected"))
    return report_policy(billing, get_adapter("claude_code").capabilities(),
                         reconciled=compare_runs(result.get("runs") or [])["reconciled"])


def run_billing(mode: str = "observe") -> Dict[str, Any]:
    """Billing of the test sessions, detected from the environment they are started with (presence only)."""
    import tempfile

    from budgetpilot.hosts.billing import detect_claude_code

    with tempfile.TemporaryDirectory() as tmp:
        b = detect_claude_code(child_env(mode, Path(tmp)))
    return {"mode": b.mode, "provider": b.provider, "confidence": b.confidence, "evidence": list(b.evidence),
            "source": b.source}


def _dollar_check_line(result: Dict[str, Any]) -> str:
    from budgetpilot.core.canary import TOLERANCE, compare_runs

    check = compare_runs(result.get("runs") or [])
    if not check["runs"]:
        return "- Price check: no run reported a cost to compare with."
    verdict = "within" if check["reconciled"] else "OUTSIDE"
    return (f"- Price check: BudgetPilot's API-equivalent cost against Claude Code's own `total_cost_usd`: largest "
            f"difference {100 * check['max_diff_pct']:.2f}% over {check['runs']} runs ({verdict} the "
            f"{100 * TOLERANCE:.0f}% tolerance).")


def render_results(result: Dict[str, Any]) -> str:
    a = result["analysis"]
    policy = ab_policy(result)
    base, treat = a["arms"].get(BASELINE, {}), a["arms"].get(a["treatment"], {})
    held_out = [t for t, row in a["per_task"].items() if row["held_out"]]
    lines = [
        "# BudgetPilot paired A/B results",
        "",
        f"Experiment `{result['experiment']}`. Written by `bp ab`; do not edit by hand. "
        f"Raw data: `docs/results/{result['experiment']}.json` (when archived) or `bench_results/`.",
        "",
        "## Result",
        "",
        f"**{a['verdict'].capitalize()}.**",
        "",
    ]
    if a["pairs"]:
        lines += [
            f"- Billing units, enforce compared with observe: **{a['mean_change_pct']:+.1f}%** per pair on average "
            f"(95% bootstrap CI {a['ci_low_pct']:+.1f}% to {a['ci_high_pct']:+.1f}%, {a['pairs']} pairs).",
            f"- In units: {a['mean_change_units']:+,} per run (95% CI {a['ci_low_units']:+,} to {a['ci_high_units']:+,}).",
            f"- Tasks passed: observe {base.get('passed')}/{base.get('runs')}, "
            f"enforce {treat.get('passed')}/{treat.get('runs')}.",
            "",
            "A negative change means BudgetPilot used fewer billing units. A saving only counts if the pass rate "
            "does not drop.",
        ]
    lines += [
        "",
        "## Setup",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| Host | {result.get('claude_version', 'unknown')}, headless (`claude -p`), `--permission-mode acceptEdits` |",
        f"| Model | `{result['model']}` in both arms, default effort, a fresh session per run |",
        f"| Arms | `observe` (hooks measure, never intervene) and `enforce` (hooks act), `BP_HITL=0` |",
        f"| Runs | {len(result['runs'])} = {len(result['tasks'])} tasks x {result['repeats']} repeats x "
        f"{len(result['arms'])} arms, in random order (seed {result['seed']}) |",
        f"| Repository | `bench/fixture_repo` (synthetic, about 3,300 lines; one 3,155-line file) |",
        f"| Success | a hidden test per task, copied in after the agent stops |",
        f"| Allowed tools | `{result.get('allowed_tools', ALLOWED_TOOLS)}` |",
        f"| Billing | {policy['billing_mode']} ({policy['billing_confidence']} confidence) |",
        f"| Cost measure | {policy['units_label']} from the session transcript: input + 1.25 x 5-minute cache "
        "writes + 2 x 1-hour cache writes + r x cache reads + 5 x output |",
        "",
        "## Per arm",
        "",
        "| Arm | Passed | Mean billing units | Mean turns | Mean output tokens | Cache-hit ratio | "
        "BudgetPilot text added (est. tokens) | Mean wall time (s) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for arm, row in a["arms"].items():
        lines.append(
            f"| {arm} | {row['passed']}/{row['runs']} | {_n(row['mean_billing_units'])} | {_n(row['mean_turns'], 1)} | "
            f"{_n(row['mean_output_tokens'])} | {_n(row['mean_cache_hit_ratio'], 3)} | "
            f"{_n(row['mean_overhead_tokens_est'])} | {_n(row['mean_wall_s'])} |"
        )
    lines += ["", "## Per task", "",
              "| Task | Scope | Held out | Pairs | Change in billing units | Passed (observe) | Passed (enforce) |",
              "|---|---|---|---|---|---|---|"]
    scopes = {t["id"]: t.get("expected_scope") for t in result["tasks"]}
    for task_id, row in a["per_task"].items():
        change = "–" if row["mean_change_pct"] is None else f"{row['mean_change_pct']:+.1f}%"
        obs, enf = row.get(BASELINE, {}), row.get(a["treatment"], {})
        lines.append(f"| {task_id} | {scopes.get(task_id, '–')} | {'yes' if row['held_out'] else 'no'} | {row['pairs']} | "
                     f"{change} | {obs.get('passed', 0)}/{obs.get('runs', 0)} | {enf.get('passed', 0)}/{enf.get('runs', 0)} |")
    lines += ["", "## Where the difference came from", ""]
    lines += _mechanism(a, base, treat)
    lines += ["", "### Gate decisions in the enforce arm (all runs)", ""]
    if a["gate_rules"]:
        lines += ["| Decision : rule | Count |", "|---|---|"]
        lines += [f"| `{key}` | {count} |" for key, count in sorted(a["gate_rules"].items(), key=lambda kv: -kv[1])]
    else:
        lines.append("The gate did not rewrite or deny any call.")
    would = a.get("baseline_would_rules") or {}
    lines += ["", "### What the gate would have done in the observe arm (logged, not applied)", ""]
    if would:
        lines += ["| Decision : rule | Count |", "|---|---|"]
        lines += [f"| `{key}` | {count} |" for key, count in sorted(would.items(), key=lambda kv: -kv[1])]
    else:
        lines.append("Nothing: no call in the observe arm matched a gate rule.")
    lines += [
        "",
        "## Checks",
        "",
        f"- Transcript totals compared with the `usage` block of `claude -p --output-format json`: largest difference "
        f"{_n(a['max_usage_discrepancy_tokens'])} tokens in any field of any run.",
        f"- Runs in which a hook logged an internal error: {a['hook_errors']}.",
        f"- Pairs where both arms had the same pass/fail status: {a['equal_status_pairs']}; mean saving on those "
        f"{_n(a['equal_status_mean_saving_units'])} units"
        + (f" (95% CI {a['equal_status_ci_units'][0]:,} to {a['equal_status_ci_units'][1]:,})."
           if a["equal_status_ci_units"] else "."),
        "",
        _dollar_check_line(result),
        "",
        "## Caveats",
        "",
        f"- Small sample: {a['pairs']} pairs. Treat the interval, not the point estimate, as the result.",
        "- One synthetic repository and one model. Results on real repositories and other models can differ.",
        "- Headless runs: the human-in-the-loop recovery path is not exercised (`BP_HITL=0`).",
        "- Billing units are API-equivalent weights. A Pro or Max plan is not billed per token, and the plan's own "
        "usage weights are not published. " + policy["note"],
        "- The observe arm still runs the hook processes, so it measures the effect of BudgetPilot's interventions, "
        "not of installing it. Latency added by the hooks is not part of billing units.",
        f"- Held-out tasks ({', '.join(held_out) if held_out else 'none in this run'}) must not be used to tune "
        "scope limits.",
        "",
        "## Scope limits used",
        "",
        "```json",
        json.dumps(result.get("scope_limits", {}), indent=1),
        "```",
        "",
        "## Reproduce",
        "",
        "```bash",
        "bp ab smoke                 # 3 tasks x 2 arms x 2 repeats",
        "bp ab run --repeats 3       # all tasks",
        "```",
        "",
    ]
    return "\n".join(lines)


def _mechanism(a: Dict[str, Any], base: Dict[str, Any], treat: Dict[str, Any]) -> List[str]:
    """Plain statement of what differed between the arms, from the recorded data only."""
    if not a["pairs"] or not base.get("mean_turns") or not treat.get("mean_turns"):
        return ["Not enough complete pairs to say."]
    denies = sum(c for k, c in a["gate_rules"].items() if k.startswith(("deny:", "ask:")))
    rewrites = sum(c for k, c in a["gate_rules"].items() if k.startswith("rewrite:"))
    lines = [
        f"- Model turns per run: {treat['mean_turns']:.1f} with BudgetPilot on, {base['mean_turns']:.1f} observing.",
        f"- Output tokens per run: {_n(treat['mean_output_tokens'])} on, {_n(base['mean_output_tokens'])} observing.",
        f"- Calls the gate changed in the enforce arm: {rewrites} rewritten, {denies} denied or sent to a dialog.",
        f"- Text BudgetPilot added per run: about {_n(treat.get('mean_overhead_tokens_est'))} tokens (estimated).",
    ]
    if denies == 0 and rewrites <= a["pairs"]:
        lines += [
            "",
            "The gate changed few calls, so in this run the difference did **not** come from blocked or narrowed "
            "tool calls. It came from the short notes BudgetPilot adds at the start (the scope limits and the "
            "terse-output contract): the model made fewer tool calls and wrote less. The observe-arm table below "
            "shows calls the baseline agent made that the enforce-arm agent did not attempt. Fewer verification "
            "runs can cost correctness on harder tasks than these; the pass rate above is the check.",
        ]
    return lines


def write_outputs(result: Dict[str, Any], out_dir: Optional[Path] = None, results_md: Optional[Path] = None) -> Dict[str, str]:
    out_dir = Path(out_dir) if out_dir else Path.cwd() / "bench_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{result['experiment']}.json"
    json_path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    written = {"json": str(json_path)}
    if results_md is not None:
        Path(results_md).parent.mkdir(parents=True, exist_ok=True)
        Path(results_md).write_text(render_results(result), encoding="utf-8")
        written["markdown"] = str(results_md)
    return written


# ------------------------------------------------------------------ CLI entry
def main_ab(command: str, tasks_path: Optional[str] = None, fixture: Optional[str] = None, repeats: Optional[int] = None,
            model: str = "haiku", task_ids: Optional[List[str]] = None, arms: Optional[List[str]] = None,
            out_dir: Optional[str] = None, results_md: Optional[str] = None, seed: int = 0, timeout: int = 600,
            report_json: Optional[str] = None, yes: bool = False, out: Callable[[str], None] = print) -> int:
    default_md = REPO_ROOT / "docs" / "RESULTS.md" if (REPO_ROOT / "docs").is_dir() else None
    md_path = Path(results_md) if results_md else default_md
    if command == "report":
        result = json.loads(Path(report_json).read_text(encoding="utf-8"))
        result["analysis"] = analyse(result["runs"])
        text = render_results(result)
        if md_path:
            md_path.write_text(text, encoding="utf-8")
            out(f"Wrote {md_path}")
        else:
            out(text)
        return 0

    from budgetpilot.cc_cli import find_claude

    claude = find_claude()
    if not claude:
        out("The `claude` executable was not found. Install Claude Code or set BP_CLAUDE_BIN.")
        return 1
    tasks = select_tasks(load_tasks(Path(tasks_path) if tasks_path else None), ids=task_ids, smoke=command == "smoke")
    if not tasks:
        out("No tasks selected.")
        return 1
    repeats = repeats or 2
    arm_list = arms or list(ARMS)
    total = len(tasks) * repeats * len(arm_list)
    out(f"This starts {total} real Claude Code sessions with model '{model}' and uses your plan's quota.")
    if os.environ.get("ANTHROPIC_API_KEY"):
        out("Note: ANTHROPIC_API_KEY is set in this shell; it is removed for the test sessions so the plan login is used.")
    if not yes:
        if not sys.stdin.isatty() or input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            out("Not confirmed. Re-run with --yes.")
            return 1
    result = run_experiment(tasks, claude, repeats=repeats, model=model, arms=arm_list,
                            fixture=Path(fixture) if fixture else None, seed=seed, timeout=timeout, out=out)
    store_runs(result)
    written = write_outputs(result, Path(out_dir) if out_dir else None, md_path)
    a = result["analysis"]
    out("")
    out(f"Result: {a['verdict']}")
    if a["pairs"]:
        out(f"  billing units, enforce vs observe: {a['mean_change_pct']:+.1f}% "
            f"(95% CI {a['ci_low_pct']:+.1f}% to {a['ci_high_pct']:+.1f}%, {a['pairs']} pairs)")
    for arm, row in a["arms"].items():
        out(f"  {arm}: passed {row['passed']}/{row['runs']}, mean {_n(row['mean_billing_units'])} units")
    for kind, path in written.items():
        out(f"  {kind}: {path}")
    return 0

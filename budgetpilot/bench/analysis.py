"""
BudgetPilot Benchmark Analysis Engine.
Phase L: Statistical evaluation of multi-arm benchmark evaluations.

Implements:
1. Paired difference in reference USD at equal solve status across arms.
2. Non-parametric bootstrap confidence intervals (e.g. 95% CI).
3. Expand-scope win rates (where expand-scope succeeded cheaper than escalate).
4. Full formatted reports: Terminal, Markdown, JSON, HTML.

Strict Rule: This module must NOT import ollama, mcp, fastapi, or uvicorn.
"""

from dataclasses import asdict, dataclass, field
import html
import json
import logging
import math
from pathlib import Path
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from budgetpilot.core.store import get_connection, init_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PairedDiffResult:
    """
    Paired difference metrics comparing an evaluation arm against a baseline arm
    restricted to trials with equal solve status (or both passed).
    """
    arm: str
    baseline_arm: str
    n_pairs_total: int
    n_equal_status: int
    n_both_passed: int
    mean_paired_cost_diff_usd: float    # cost_arm - cost_baseline (negative = saved money)
    mean_paired_savings_usd: float      # cost_baseline - cost_arm (positive = saved money)
    ci_lower: float                     # lower bound of bootstrap CI on savings
    ci_upper: float                     # upper bound of bootstrap CI on savings
    ci_level: float = 0.95
    bootstrap_iterations: int = 1000
    paired_differences: List[float] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "arm": self.arm,
            "baseline_arm": self.baseline_arm,
            "n_pairs_total": self.n_pairs_total,
            "n_equal_status": self.n_equal_status,
            "n_both_passed": self.n_both_passed,
            "mean_paired_cost_diff_usd": round(self.mean_paired_cost_diff_usd, 6),
            "mean_paired_savings_usd": round(self.mean_paired_savings_usd, 6),
            "ci_lower": round(self.ci_lower, 6),
            "ci_upper": round(self.ci_upper, 6),
            "ci_level": self.ci_level,
            "bootstrap_iterations": self.bootstrap_iterations,
        }


@dataclass
class ExpandScopeWinRate:
    """
    Metrics measuring how frequently 'expand_scope' succeeded cheaper than escalation.
    """
    total_expand_scope_attempts: int
    successful_expand_scope_attempts: int
    win_count: int                       # succeeded AND cost < escalate cost
    win_rate: float                      # win_count / total_expand_scope_attempts
    success_rate: float                  # successful / total
    avg_expand_scope_cost: float
    avg_escalate_cost: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_expand_scope_attempts": self.total_expand_scope_attempts,
            "successful_expand_scope_attempts": self.successful_expand_scope_attempts,
            "win_count": self.win_count,
            "win_rate": round(self.win_rate, 4),
            "success_rate": round(self.success_rate, 4),
            "avg_expand_scope_cost": round(self.avg_expand_scope_cost, 6),
            "avg_escalate_cost": round(self.avg_escalate_cost, 6),
        }


@dataclass
class ArmSummary:
    """
    Aggregated performance metrics for a single benchmark arm.
    """
    arm: str
    total_runs: int
    passed_runs: int
    failed_runs: int
    pass_rate: float                     # percentage 0.0 - 100.0
    total_cost_usd: float
    avg_cost_usd: float
    total_tokens: int
    avg_tokens: float
    avg_wall_time: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "arm": self.arm,
            "total_runs": self.total_runs,
            "passed_runs": self.passed_runs,
            "failed_runs": self.failed_runs,
            "pass_rate": round(self.pass_rate, 2),
            "total_cost_usd": round(self.total_cost_usd, 6),
            "avg_cost_usd": round(self.avg_cost_usd, 6),
            "total_tokens": self.total_tokens,
            "avg_tokens": round(self.avg_tokens, 1),
            "avg_wall_time": round(self.avg_wall_time, 2),
        }


@dataclass
class BenchAnalysisReport:
    """
    Complete benchmark analysis report across all evaluated arms.
    """
    task_count: int
    repeats: int
    arms: List[str]
    arm_summaries: Dict[str, ArmSummary]
    paired_differences: Dict[str, PairedDiffResult]
    expand_scope_win_rate: ExpandScopeWinRate
    baseline_arm: str = "off"
    raw_results_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_count": self.task_count,
            "repeats": self.repeats,
            "arms": self.arms,
            "baseline_arm": self.baseline_arm,
            "raw_results_count": self.raw_results_count,
            "arm_summaries": {k: v.to_dict() for k, v in self.arm_summaries.items()},
            "paired_differences": {k: v.to_dict() for k, v in self.paired_differences.items()},
            "expand_scope_win_rate": self.expand_scope_win_rate.to_dict(),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def to_terminal(self) -> str:
        lines: List[str] = []
        lines.append("=" * 78)
        lines.append(" BudgetPilot Benchmark Multi-Arm Analysis (Phase L)")
        lines.append("=" * 78)
        lines.append(f"  Tasks: {self.task_count} | Repeats: {self.repeats} | Baseline Arm: '{self.baseline_arm}'")
        lines.append("")

        # 1. Arm performance comparison
        lines.append("-" * 25 + " Arm Performance Overview " + "-" * 27)
        header = f"  {'Arm':<12} {'Runs':<7} {'Pass %':<9} {'Avg Cost ($)':<14} {'Total Cost ($)':<16} {'Avg Tokens':<12}"
        lines.append(header)
        lines.append("  " + "-" * 72)
        for arm in self.arms:
            s = self.arm_summaries.get(arm)
            if s:
                lines.append(
                    f"  {s.arm:<12} {s.total_runs:<7} {s.pass_rate:>6.1f}%  "
                    f"${s.avg_cost_usd:<13.6f} ${s.total_cost_usd:<15.6f} {int(s.avg_tokens):<12,}"
                )
        lines.append("")

        # 2. Paired difference at equal solve status
        lines.append("-" * 20 + " Paired Savings at Equal Solve Status " + "-" * 20)
        lines.append(f"  Comparing evaluated arms against baseline '{self.baseline_arm}':")
        diff_hdr = f"  {'Arm vs Base':<16} {'Pairs':<8} {'Equal Stat':<12} {'Mean Savings ($)':<18} {'95% Bootstrap CI ($)':<22}"
        lines.append(diff_hdr)
        lines.append("  " + "-" * 74)
        for arm, p in self.paired_differences.items():
            ci_str = f"[${p.ci_lower:.6f}, ${p.ci_upper:.6f}]"
            lines.append(
                f"  {arm + ' vs ' + self.baseline_arm:<16} {p.n_pairs_total:<8} {p.n_equal_status:<12} "
                f"${p.mean_paired_savings_usd:<17.6f} {ci_str:<22}"
            )
        lines.append("")

        # 3. Expand-scope win rate
        es = self.expand_scope_win_rate
        lines.append("-" * 23 + " Expand-Scope Win Rate " + "-" * 28)
        lines.append(f"  Total Expand-Scope Attempts:      {es.total_expand_scope_attempts}")
        lines.append(f"  Successful Attempts:             {es.successful_expand_scope_attempts} ({es.success_rate * 100:.1f}%)")
        lines.append(f"  Wins (Succeeded Cheaper than Esc): {es.win_count} ({es.win_rate * 100:.1f}%)")
        lines.append(f"  Avg Expand-Scope Cost:           ${es.avg_expand_scope_cost:.6f} USD")
        lines.append(f"  Avg Escalate Cost (Reference):   ${es.avg_escalate_cost:.6f} USD")
        lines.append("=" * 78)
        return "\n".join(lines)

    def to_markdown(self) -> str:
        lines: List[str] = []
        lines.append("# BudgetPilot Benchmark Multi-Arm Analysis (Phase L)\n")
        lines.append(f"- **Tasks**: `{self.task_count}`")
        lines.append(f"- **Repeats**: `{self.repeats}`")
        lines.append(f"- **Baseline Arm**: `{self.baseline_arm}`")
        lines.append(f"- **Total Executions**: `{self.raw_results_count}`\n")

        lines.append("## Arm Performance Overview\n")
        lines.append("| Arm | Total Runs | Passed | Pass Rate | Avg Cost (USD) | Total Cost (USD) | Avg Tokens | Avg Wall Time |")
        lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
        for arm in self.arms:
            s = self.arm_summaries.get(arm)
            if s:
                lines.append(
                    f"| `{s.arm}` | {s.total_runs} | {s.passed_runs} | {s.pass_rate:.1f}% | "
                    f"${s.avg_cost_usd:.6f} | ${s.total_cost_usd:.6f} | {int(s.avg_tokens):,} | {s.avg_wall_time:.2f}s |"
                )
        lines.append("\n## Paired Savings at Equal Solve Status\n")
        lines.append(f"_Restricted to task-repeat pairs where the arm and `{self.baseline_arm}` achieved the same solve verdict._\n")
        lines.append("| Comparison | Total Pairs | Equal Status Pairs | Mean Savings (USD) | 95% Bootstrap CI (USD) |")
        lines.append("| :--- | :---: | :---: | :---: | :---: |")
        for arm, p in self.paired_differences.items():
            ci_str = f"[`${p.ci_lower:.6f}`, `${p.ci_upper:.6f}`]"
            lines.append(
                f"| `{arm}` vs `{self.baseline_arm}` | {p.n_pairs_total} | {p.n_equal_status} | "
                f"**${p.mean_paired_savings_usd:.6f}** | {ci_str} |"
            )

        es = self.expand_scope_win_rate
        lines.append("\n## Expand-Scope Recovery Efficiency\n")
        lines.append("| Metric | Value |")
        lines.append("| :--- | :---: |")
        lines.append(f"| Total Expand-Scope Attempts | {es.total_expand_scope_attempts} |")
        lines.append(f"| Successful Attempts | {es.successful_expand_scope_attempts} ({es.success_rate * 100:.1f}%) |")
        lines.append(f"| **Win Count** (Succeeded Cheaper than Escalate) | **{es.win_count}** |")
        lines.append(f"| **Win Rate** | **{es.win_rate * 100:.1f}%** |")
        lines.append(f"| Avg Expand-Scope Cost | ${es.avg_expand_scope_cost:.6f} USD |")
        lines.append(f"| Avg Escalate Cost (Reference) | ${es.avg_escalate_cost:.6f} USD |\n")

        return "\n".join(lines)

    def to_html(self) -> str:
        es = self.expand_scope_win_rate
        arm_rows = []
        for arm in self.arms:
            s = self.arm_summaries.get(arm)
            if s:
                badge_class = "badge-success" if s.pass_rate >= 80 else ("badge-warning" if s.pass_rate >= 50 else "badge-danger")
                arm_rows.append(f"""
                <tr>
                    <td><span class="arm-tag">{html.escape(s.arm)}</span></td>
                    <td>{s.total_runs}</td>
                    <td><span class="badge {badge_class}">{s.pass_rate:.1f}%</span></td>
                    <td>${s.avg_cost_usd:.6f}</td>
                    <td><strong>${s.total_cost_usd:.6f}</strong></td>
                    <td>{int(s.avg_tokens):,}</td>
                    <td>{s.avg_wall_time:.2f}s</td>
                </tr>
                """)

        paired_rows = []
        for arm, p in self.paired_differences.items():
            savings_class = "positive-val" if p.mean_paired_savings_usd > 0 else "neutral-val"
            paired_rows.append(f"""
            <tr>
                <td><strong>{html.escape(arm)}</strong> vs <em>{html.escape(self.baseline_arm)}</em></td>
                <td>{p.n_pairs_total}</td>
                <td>{p.n_equal_status}</td>
                <td class="{savings_class}">+${p.mean_paired_savings_usd:.6f} USD</td>
                <td><span class="ci-chip">[${p.ci_lower:.6f}, ${p.ci_upper:.6f}]</span></td>
            </tr>
            """)

        html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BudgetPilot Benchmark Report — Phase L</title>
    <style>
        :root {{
            --bg: #0f172a;
            --surface: #1e293b;
            --surface-border: #334155;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --primary: #38bdf8;
            --success: #4ade80;
            --warning: #facc15;
            --danger: #f87171;
            --font: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        }}
        body {{
            background-color: var(--bg);
            color: var(--text-main);
            font-family: var(--font);
            margin: 0;
            padding: 32px 20px;
            display: flex;
            justify-content: center;
        }}
        .container {{
            max-width: 1040px;
            width: 100%;
        }}
        header {{
            margin-bottom: 28px;
            border-bottom: 1px solid var(--surface-border);
            padding-bottom: 20px;
        }}
        h1 {{
            font-size: 28px;
            font-weight: 700;
            margin: 0 0 8px 0;
            color: var(--text-main);
            letter-spacing: -0.5px;
        }}
        .subtitle {{
            color: var(--text-muted);
            font-size: 14px;
        }}
        .card {{
            background: var(--surface);
            border: 1px solid var(--surface-border);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 24px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
        }}
        h2 {{
            font-size: 18px;
            margin-top: 0;
            margin-bottom: 16px;
            color: var(--primary);
            font-weight: 600;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }}
        th, td {{
            padding: 12px 14px;
            text-align: left;
            border-bottom: 1px solid var(--surface-border);
        }}
        th {{
            color: var(--text-muted);
            font-weight: 600;
            text-transform: uppercase;
            font-size: 12px;
            letter-spacing: 0.5px;
        }}
        tr:last-child td {{
            border-bottom: none;
        }}
        .badge {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 9999px;
            font-size: 12px;
            font-weight: 600;
        }}
        .badge-success {{ background: rgba(74, 222, 128, 0.15); color: var(--success); }}
        .badge-warning {{ background: rgba(250, 204, 21, 0.15); color: var(--warning); }}
        .badge-danger  {{ background: rgba(248, 113, 113, 0.15); color: var(--danger); }}
        .arm-tag {{
            background: #0f172a;
            border: 1px solid var(--surface-border);
            padding: 3px 8px;
            border-radius: 6px;
            font-family: monospace;
            font-weight: 600;
        }}
        .ci-chip {{
            background: #0f172a;
            padding: 3px 8px;
            border-radius: 4px;
            font-family: monospace;
            font-size: 13px;
            color: var(--text-muted);
        }}
        .positive-val {{
            color: var(--success);
            font-weight: 600;
        }}
        .neutral-val {{
            color: var(--text-muted);
        }}
        .grid-stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-top: 12px;
        }}
        .stat-box {{
            background: #0f172a;
            border: 1px solid var(--surface-border);
            border-radius: 8px;
            padding: 16px;
        }}
        .stat-label {{
            font-size: 12px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 6px;
        }}
        .stat-value {{
            font-size: 24px;
            font-weight: 700;
            color: var(--text-main);
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>BudgetPilot Multi-Arm Benchmark Analysis</h1>
            <div class="subtitle">Phase L &bull; Tasks: {self.task_count} &bull; Repeats: {self.repeats} &bull; Baseline: {self.baseline_arm}</div>
        </header>

        <div class="card">
            <h2>Arm Performance Comparison</h2>
            <table>
                <thead>
                    <tr>
                        <th>Arm</th>
                        <th>Runs</th>
                        <th>Pass Rate</th>
                        <th>Avg Cost</th>
                        <th>Total Cost</th>
                        <th>Avg Tokens</th>
                        <th>Avg Time</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(arm_rows)}
                </tbody>
            </table>
        </div>

        <div class="card">
            <h2>Paired Savings at Equal Solve Status</h2>
            <p style="color: var(--text-muted); font-size: 13px; margin-top: 0;">
                Calculated strictly across matched (task, repeat) runs where the evaluated arm and baseline achieved the same solve verdict.
            </p>
            <table>
                <thead>
                    <tr>
                        <th>Comparison</th>
                        <th>Matched Pairs</th>
                        <th>Equal Status</th>
                        <th>Mean Savings (USD)</th>
                        <th>95% Bootstrap CI</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(paired_rows)}
                </tbody>
            </table>
        </div>

        <div class="card">
            <h2>Expand-Scope Win Rates</h2>
            <div class="grid-stats">
                <div class="stat-box">
                    <div class="stat-label">Win Rate</div>
                    <div class="stat-value" style="color: var(--success);">{es.win_rate * 100:.1f}%</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Wins / Total</div>
                    <div class="stat-value">{es.win_count} / {es.total_expand_scope_attempts}</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Avg Expand-Scope Cost</div>
                    <div class="stat-value">${es.avg_expand_scope_cost:.5f}</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Ref Escalate Cost</div>
                    <div class="stat-value">${es.avg_escalate_cost:.5f}</div>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""
        return html_out


# ---------------------------------------------------------------------------
# Bootstrap computation
# ---------------------------------------------------------------------------

def bootstrap_ci(
    data: Sequence[float],
    ci: float = 0.95,
    n_resamples: int = 1000,
    seed: Optional[int] = None,
) -> Tuple[float, float, float]:
    """
    Compute non-parametric bootstrap confidence interval for the sample mean.

    Args:
        data: sequence of scalar values (e.g. paired differences).
        ci: confidence level in (0, 1), default 0.95.
        n_resamples: number of bootstrap resamples with replacement (default 1000).
        seed: optional random seed for reproducibility.

    Returns:
        (point_estimate_mean, ci_lower, ci_upper)
    """
    if len(data) == 0:
        return 0.0, 0.0, 0.0

    point_estimate = sum(data) / len(data)
    if len(data) == 1:
        return point_estimate, point_estimate, point_estimate

    rng = random.Random(seed) if seed is not None else random.Random()
    n = len(data)
    bootstrap_means: List[float] = []

    for _ in range(n_resamples):
        sample = [data[rng.randint(0, n - 1)] for _ in range(n)]
        bootstrap_means.append(sum(sample) / n)

    bootstrap_means.sort()

    alpha = 1.0 - ci
    lower_idx = int(math.floor((alpha / 2.0) * n_resamples))
    upper_idx = int(math.ceil((1.0 - (alpha / 2.0)) * n_resamples)) - 1

    lower_idx = max(0, min(lower_idx, n_resamples - 1))
    upper_idx = max(0, min(upper_idx, n_resamples - 1))

    return point_estimate, bootstrap_means[lower_idx], bootstrap_means[upper_idx]


# ---------------------------------------------------------------------------
# Paired difference computation
# ---------------------------------------------------------------------------

def compute_paired_cost_difference(
    run_results: Sequence[Any],
    baseline_arm: str = "off",
    ci: float = 0.95,
    n_resamples: int = 1000,
    seed: Optional[int] = None,
) -> Dict[str, PairedDiffResult]:
    """
    Compute paired difference in reference USD at equal solve status for each arm
    relative to baseline_arm.

    Matching key: (task_id, repeat).
    Condition: run_arm.passed == run_baseline.passed.
    Savings definition: cost_baseline - cost_arm (positive means arm was cheaper).
    Cost diff definition: cost_arm - cost_baseline (negative means arm was cheaper).

    Returns:
        Dict[arm_name -> PairedDiffResult]
    """
    # Group results by (task_id, repeat) -> {arm -> result}
    runs_by_key: Dict[Tuple[str, int], Dict[str, Any]] = {}
    arms_seen: List[str] = []

    for r in run_results:
        # Support dicts or dataclasses
        task_id = r.get("task_id") if isinstance(r, dict) else getattr(r, "task_id", "")
        repeat = r.get("repeat") if isinstance(r, dict) else getattr(r, "repeat", 1)
        arm = str(r.get("arm") if isinstance(r, dict) else getattr(r, "arm", "")).strip().lower()

        if arm not in arms_seen:
            arms_seen.append(arm)

        key = (task_id, repeat)
        if key not in runs_by_key:
            runs_by_key[key] = {}
        runs_by_key[key][arm] = r

    results: Dict[str, PairedDiffResult] = {}

    for arm in arms_seen:
        if arm == baseline_arm:
            continue

        n_pairs_total = 0
        n_equal_status = 0
        n_both_passed = 0
        paired_savings_list: List[float] = []
        paired_cost_diff_list: List[float] = []

        for key, arm_map in runs_by_key.items():
            if baseline_arm in arm_map and arm in arm_map:
                n_pairs_total += 1
                base_run = arm_map[baseline_arm]
                arm_run = arm_map[arm]

                base_passed = base_run.get("passed") if isinstance(base_run, dict) else getattr(base_run, "passed", False)
                arm_passed = arm_run.get("passed") if isinstance(arm_run, dict) else getattr(arm_run, "passed", False)

                base_cost = float(base_run.get("cost_usd") if isinstance(base_run, dict) else getattr(base_run, "cost_usd", 0.0))
                arm_cost = float(arm_run.get("cost_usd") if isinstance(arm_run, dict) else getattr(arm_run, "cost_usd", 0.0))

                if bool(base_passed) and bool(arm_passed):
                    n_both_passed += 1

                # EQUAL SOLVE STATUS: both passed or both failed
                if bool(base_passed) == bool(arm_passed):
                    n_equal_status += 1
                    savings = base_cost - arm_cost
                    cost_diff = arm_cost - base_cost
                    paired_savings_list.append(savings)
                    paired_cost_diff_list.append(cost_diff)

        mean_savings, ci_lower, ci_upper = bootstrap_ci(
            data=paired_savings_list,
            ci=ci,
            n_resamples=n_resamples,
            seed=seed,
        )

        mean_cost_diff = (
            sum(paired_cost_diff_list) / len(paired_cost_diff_list)
            if paired_cost_diff_list else 0.0
        )

        results[arm] = PairedDiffResult(
            arm=arm,
            baseline_arm=baseline_arm,
            n_pairs_total=n_pairs_total,
            n_equal_status=n_equal_status,
            n_both_passed=n_both_passed,
            mean_paired_cost_diff_usd=mean_cost_diff,
            mean_paired_savings_usd=mean_savings,
            ci_lower=ci_lower,
            ci_upper=ci_upper,
            ci_level=ci,
            bootstrap_iterations=n_resamples,
            paired_differences=paired_savings_list,
        )

    return results


# ---------------------------------------------------------------------------
# Expand-scope win rate computation
# ---------------------------------------------------------------------------

def compute_expand_scope_win_rate(
    db_path: Optional[Union[Path, str]] = None,
    session_ids: Optional[Sequence[str]] = None,
    reference_escalate_cost: float = 0.005,
) -> ExpandScopeWinRate:
    """
    Compute expand-scope win rates:
    where 'expand_scope' was chosen, succeeded, and cost less than escalation.

    If session_ids is provided, restricts to those sessions.
    Otherwise reads all historical recovery outcomes from SQLite.
    """
    conn = get_connection(db_path)
    if conn is None:
        return ExpandScopeWinRate(
            total_expand_scope_attempts=0,
            successful_expand_scope_attempts=0,
            win_count=0,
            win_rate=0.0,
            success_rate=0.0,
            avg_expand_scope_cost=0.0,
            avg_escalate_cost=reference_escalate_cost,
        )

    try:
        # 1. Determine average escalate cost
        esc_query = "SELECT AVG(cost_usd) as avg_cost, COUNT(*) as cnt FROM recovery_outcomes WHERE action = 'escalate'"
        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            esc_query += f" AND session_id IN ({placeholders})"
            esc_row = conn.execute(esc_query, tuple(session_ids)).fetchone()
        else:
            esc_row = conn.execute(esc_query).fetchone()

        avg_esc_cost = reference_escalate_cost
        if esc_row and esc_row["avg_cost"] is not None and esc_row["cnt"] > 0:
            avg_esc_cost = float(esc_row["avg_cost"])

        # 2. Query expand_scope outcomes
        es_query = """
        SELECT succeeded, cost_usd
        FROM recovery_outcomes
        WHERE action = 'expand_scope'
        """
        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            es_query += f" AND session_id IN ({placeholders})"
            rows = conn.execute(es_query, tuple(session_ids)).fetchall()
        else:
            rows = conn.execute(es_query).fetchall()

        total_attempts = len(rows)
        if total_attempts == 0:
            return ExpandScopeWinRate(
                total_expand_scope_attempts=0,
                successful_expand_scope_attempts=0,
                win_count=0,
                win_rate=0.0,
                success_rate=0.0,
                avg_expand_scope_cost=0.0,
                avg_escalate_cost=avg_esc_cost,
            )

        successes = 0
        wins = 0
        total_es_cost = 0.0

        for r in rows:
            succ = bool(r["succeeded"])
            cost = float(r["cost_usd"] or 0.0)
            total_es_cost += cost

            if succ:
                successes += 1
                if cost < avg_esc_cost:
                    wins += 1

        avg_es_cost = total_es_cost / total_attempts
        win_rate = wins / total_attempts if total_attempts > 0 else 0.0
        success_rate = successes / total_attempts if total_attempts > 0 else 0.0

        return ExpandScopeWinRate(
            total_expand_scope_attempts=total_attempts,
            successful_expand_scope_attempts=successes,
            win_count=wins,
            win_rate=win_rate,
            success_rate=success_rate,
            avg_expand_scope_cost=avg_es_cost,
            avg_escalate_cost=avg_esc_cost,
        )

    except Exception as exc:
        logger.debug("Failed to compute expand-scope win rate: %s", exc)
        return ExpandScopeWinRate(
            total_expand_scope_attempts=0,
            successful_expand_scope_attempts=0,
            win_count=0,
            win_rate=0.0,
            success_rate=0.0,
            avg_expand_scope_cost=0.0,
            avg_escalate_cost=reference_escalate_cost,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Comprehensive analysis aggregator
# ---------------------------------------------------------------------------

def analyze_bench_results(
    results: Sequence[Any],
    baseline_arm: str = "off",
    db_path: Optional[Union[Path, str]] = None,
    ci: float = 0.95,
    seed: Optional[int] = 42,
) -> BenchAnalysisReport:
    """
    Build a comprehensive BenchAnalysisReport from a collection of TaskRunResults/ArmRunResults.
    """
    # Group results by arm and extract distinct tasks and repeats
    by_arm: Dict[str, List[Any]] = {}
    tasks_seen = set()
    repeats_seen = set()
    session_ids: List[str] = []

    for r in results:
        arm = str(r.get("arm") if isinstance(r, dict) else getattr(r, "arm", "")).strip().lower()
        task_id = r.get("task_id") if isinstance(r, dict) else getattr(r, "task_id", "")
        repeat = r.get("repeat") if isinstance(r, dict) else getattr(r, "repeat", 1)
        sess_id = r.get("session_id") if isinstance(r, dict) else getattr(r, "session_id", "")

        if task_id:
            tasks_seen.add(task_id)
        if repeat:
            repeats_seen.add(repeat)
        if sess_id:
            session_ids.append(sess_id)

        if arm not in by_arm:
            by_arm[arm] = []
        by_arm[arm].append(r)

    arms_list = list(by_arm.keys())

    # Build per-arm summaries
    arm_summaries: Dict[str, ArmSummary] = {}
    for arm, arm_runs in by_arm.items():
        total_runs = len(arm_runs)
        passed = sum(
            1 for x in arm_runs
            if (x.get("passed") if isinstance(x, dict) else getattr(x, "passed", False))
        )
        failed = total_runs - passed
        pass_rate = (passed / total_runs * 100.0) if total_runs > 0 else 0.0

        total_cost = sum(
            float(x.get("cost_usd") if isinstance(x, dict) else getattr(x, "cost_usd", 0.0))
            for x in arm_runs
        )
        avg_cost = total_cost / total_runs if total_runs > 0 else 0.0

        total_toks = sum(
            int(x.get("total_tokens") if isinstance(x, dict) else getattr(x, "total_tokens", 0))
            for x in arm_runs
        )
        avg_toks = total_toks / total_runs if total_runs > 0 else 0.0

        total_wall = sum(
            float(x.get("wall_time") if isinstance(x, dict) else getattr(x, "wall_time", 0.0))
            for x in arm_runs
        )
        avg_wall = total_wall / total_runs if total_runs > 0 else 0.0

        arm_summaries[arm] = ArmSummary(
            arm=arm,
            total_runs=total_runs,
            passed_runs=passed,
            failed_runs=failed,
            pass_rate=pass_rate,
            total_cost_usd=total_cost,
            avg_cost_usd=avg_cost,
            total_tokens=total_toks,
            avg_tokens=avg_toks,
            avg_wall_time=avg_wall,
        )

    # Paired differences at equal solve status
    paired_diffs = compute_paired_cost_difference(
        run_results=results,
        baseline_arm=baseline_arm,
        ci=ci,
        seed=seed,
    )

    # Expand-scope win rates
    expand_scope_win_rate = compute_expand_scope_win_rate(
        db_path=db_path,
        session_ids=session_ids if session_ids else None,
    )

    return BenchAnalysisReport(
        task_count=len(tasks_seen),
        repeats=len(repeats_seen) if repeats_seen else 1,
        arms=arms_list,
        arm_summaries=arm_summaries,
        paired_differences=paired_diffs,
        expand_scope_win_rate=expand_scope_win_rate,
        baseline_arm=baseline_arm,
        raw_results_count=len(results),
    )

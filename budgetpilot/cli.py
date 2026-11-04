"""
BudgetPilot Command-Line Interface.
Phase A: CLI entrypoint, diagnostics (bp doctor), interactive chat (bp chat), and configuration inspection.
"""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Union

import budgetpilot
from budgetpilot.config import get_bp_home, load_config, save_config
from budgetpilot.core.store import CORE_TABLES, get_connection, init_db

# Known model families with native tool-calling support
TOOL_SUPPORTED_FAMILIES = (
    "qwen2.5",
    "qwen3",
    "llama3.1",
    "llama3.2",
    "llama3.3",
    "mistral",
    "mixtral",
    "command-r",
    "hermes",
    "firefunction",
)


def run_doctor() -> int:
    """
    Execute environment, runtime, and dependency diagnostics.

    Checks:
    1. Ollama server reachability and available models.
    2. Tool calling support in pulled models.
    3. Git and ripgrep (rg) presence on PATH.
    4. BP_HOME write access and SQLite database health.

    Returns:
        0 if all critical checks pass, 1 if any check fails.
    """
    print("=" * 55)
    print(" BudgetPilot Doctor — System Diagnostics")
    print("=" * 55)
    print()

    total_checks = 0
    passed_checks = 0

    # 1. Check Ollama reachability & model list
    total_checks += 1
    pulled_model_names: List[str] = []
    try:
        import ollama
        models_resp = ollama.list()
        # Handle both object attributes and dict responses
        models_list = getattr(models_resp, "models", None)
        if models_list is None and isinstance(models_resp, dict):
            models_list = models_resp.get("models", [])
        if models_list is None:
            models_list = []

        for m in models_list:
            name = getattr(m, "model", None) or (m.get("model") if isinstance(m, dict) else str(m))
            if name:
                pulled_model_names.append(name)

        print(f"[PASS] Ollama Server: Reachable (found {len(pulled_model_names)} models)")
        if pulled_model_names:
            print(f"       Models: {', '.join(pulled_model_names)}")
        passed_checks += 1
    except Exception as exc:
        print(f"[FAIL] Ollama Server: Unreachable on localhost:11434 ({exc})")
        print("       Action: Start Ollama with 'ollama serve'")

    # 2. Check tool calling support in available models
    total_checks += 1
    tool_capable_models = [
        m for m in pulled_model_names
        if any(f in m.lower() for f in TOOL_SUPPORTED_FAMILIES)
    ]
    if tool_capable_models:
        print(f"[PASS] Tool-Calling Models: {', '.join(tool_capable_models)}")
        passed_checks += 1
    elif pulled_model_names:
        print(f"[WARN] Tool-Calling Models: None of {pulled_model_names} matched known tool-calling families.")
        print(f"       Recommendation: Run 'ollama pull qwen2.5:3b'")
        passed_checks += 1
    else:
        print("[FAIL] Tool-Calling Models: No models pulled in Ollama.")
        print("       Action: Run 'ollama pull qwen2.5:3b'")

    # 3. Check git on PATH
    total_checks += 1
    git_path = shutil.which("git")
    if git_path:
        print(f"[PASS] Git: Available ({git_path})")
        passed_checks += 1
    else:
        print("[FAIL] Git: Not found on PATH. Git is required for repository diffs.")

    # 4. Check ripgrep (rg) on PATH
    total_checks += 1
    rg_path = shutil.which("rg")
    if rg_path:
        print(f"[PASS] Ripgrep (rg): Available ({rg_path})")
        passed_checks += 1
    else:
        print("[FAIL] Ripgrep (rg): Not found on PATH.")
        print("       Action: Install ripgrep (e.g. 'sudo apt install ripgrep' or 'conda install ripgrep')")

    # 5. Check BP_HOME write access and database health
    total_checks += 1
    bp_home = get_bp_home()
    write_ok = False
    try:
        test_file = bp_home / f".write_test_{int(time.time())}"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        write_ok = True
    except Exception as exc:
        print(f"[FAIL] Storage: Cannot write to BP_HOME ({bp_home}): {exc}")

    if write_ok:
        try:
            conn = init_db()
            cursor = conn.execute("PRAGMA integrity_check;")
            integrity_res = cursor.fetchone()[0]

            cursor = conn.execute("PRAGMA journal_mode;")
            journal_mode = cursor.fetchone()[0]

            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = {row[0] for row in cursor.fetchall()}
            missing_tables = [t for t in CORE_TABLES if t not in tables]

            conn.close()

            if integrity_res == "ok" and not missing_tables and journal_mode.lower() == "wal":
                print(f"[PASS] Database & Storage: Healthy at {bp_home} (14 core tables, WAL active, integrity OK)")
                passed_checks += 1
            else:
                print(f"[FAIL] Database: Integrity={integrity_res}, JournalMode={journal_mode}, Missing tables={missing_tables}")
        except Exception as exc:
            print(f"[FAIL] Database initialization failed: {exc}")

    # 6. Check prices.json configuration validity
    total_checks += 1
    try:
        from budgetpilot.core.prices import load_prices, resolve_prices_path
        prices_path = resolve_prices_path()
        prices = load_prices(prices_path)
        if "cheap" in prices and "strong" in prices:
            cheap = prices["cheap"]
            strong = prices["strong"]
            print(f"[PASS] Prices Configuration: Valid ({prices_path})")
            print(f"       Tiers: cheap ($ {cheap.input_per_million:.2f}/M in, $ {cheap.output_per_million:.2f}/M out), strong ($ {strong.input_per_million:.2f}/M in, $ {strong.output_per_million:.2f}/M out)")
            passed_checks += 1
        else:
            print(f"[FAIL] Prices Configuration: Missing required tiers in {prices_path}")
    except Exception as exc:
        print(f"[FAIL] Prices Configuration: Invalid prices file ({exc})")

    # 7. Check Gate Interception Latency SLA (p95 gate response check < 60ms)
    total_checks += 1
    try:
        from budgetpilot.core.gate import check as gate_check
        from budgetpilot.core.tracker import TaskState
        from budgetpilot.core.types import BpEvent

        sample_state = TaskState(session_id="doctor_latency_bench")
        sample_event = BpEvent(
            event_type="read_file",
            payload={"tool_name": "read_file", "arguments": {"path": "budgetpilot/cli.py", "start": 1, "end": 100}},
        )

        latencies_ms = []
        for _ in range(5):
            _ = gate_check(sample_event, sample_state)

        for _ in range(50):
            t0 = time.perf_counter()
            _ = gate_check(sample_event, sample_state)
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)

        latencies_ms.sort()
        p50 = latencies_ms[int(len(latencies_ms) * 0.50)]
        p95 = latencies_ms[int(len(latencies_ms) * 0.95)]
        p99 = latencies_ms[int(len(latencies_ms) * 0.99)]

        TARGET_P95_MS = 60.0
        if p95 < TARGET_P95_MS:
            print(f"[PASS] Gate Interception Latency: SLA satisfied (p95={p95:.2f}ms < {TARGET_P95_MS:.0f}ms target)")
            print(f"       Percentiles: p50={p50:.2f}ms, p95={p95:.2f}ms, p99={p99:.2f}ms (50 samples)")
            passed_checks += 1
        else:
            print(f"[FAIL] Gate Interception Latency: p95={p95:.2f}ms exceeded target {TARGET_P95_MS:.0f}ms")
    except Exception as exc:
        print(f"[FAIL] Gate Interception Latency Audit: Failed ({exc})")

    print()
    print("-" * 55)
    print(f" Diagnostics Result: {passed_checks}/{total_checks} checks passed.")
    print("-" * 55)

    return 0 if passed_checks == total_checks else 1


def run_chat_loop(tier: str = "cheap", session_id: Optional[str] = None) -> int:
    """
    Run the interactive BudgetPilot chat loop replacing legacy main.py.
    Provides live telemetry, pricing, and advisory anomaly tracking.
    """
    from budgetpilot.agent.model import chat
    from budgetpilot.core.ledger import CostLedger, detect_anomalies, record_model_call

    init_db()
    ledger = CostLedger()
    active_session = session_id or f"chat_{int(time.time())}"

    print("=" * 45)
    print(" BudgetPilot Interactive Chat")
    print(f" Session ID: {active_session}")
    print(f" Model Tier: {tier}")
    print(" Type 'exit' to quit.")
    print("=" * 45)
    print()

    messages: List[Dict[str, str]] = []

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break

        if not user_input:
            continue

        if user_input.lower() == "exit":
            print("Goodbye!")
            break

        messages.append({"role": "user", "content": user_input})

        # Fetch recent session history for anomaly baseline
        history = ledger.get_session_history(limit=50)

        # Call model
        try:
            result = chat(messages=messages, tier=tier)
        except Exception as exc:
            print(f"\n[Error calling model]: {exc}\n")
            continue

        # Record telemetry & compute reference cost
        record = record_model_call(
            model=result.model,
            prompt=user_input,
            response=result.content,
            prompt_eval_tokens=result.prompt_tokens,
            output_tokens=result.output_tokens,
            tier=tier,
            session_id=active_session,
            total_time=result.total_time,
            load_time=result.load_time,
            prompt_time=result.prompt_time,
            generation_time=result.generation_time,
        )

        # Advisory anomaly check (mean + 2σ)
        anomaly_report = detect_anomalies(
            history=history,
            current={
                "total_time": result.total_time,
                "input_tokens": result.prompt_tokens,
                "output_tokens": result.output_tokens,
            },
        )

        # Display advisory status banner
        print()
        print("===== BudgetPilot Status =====")
        print(f"Status: {anomaly_report.status}")
        print(anomaly_report.message)
        print(f"Latency anomaly: {anomaly_report.latency_anomaly}")
        print(f"Input token anomaly: {anomaly_report.input_token_anomaly}")
        print(f"Output token anomaly: {anomaly_report.output_token_anomaly}")
        print()

        # Print model response and telemetry breakdown
        print(f"Assistant: {result.content}\n")
        print(f"Input tokens: {record['billable_input_tokens']}")
        print(f"Output tokens: {result.output_tokens}")
        print(f"Total tokens: {record['total_tokens']}")
        print(f"Estimated cloud cost: $ {record['estimated_cost']:.6f}")
        print(f"Total time: {result.total_time:.2f} seconds")
        print(f"Load time: {result.load_time:.2f} seconds")
        print(f"Prompt processing: {result.prompt_time:.2f} seconds")
        print(f"Generation time: {result.generation_time:.2f} seconds\n")

        # Save to conversation history
        messages.append({"role": "assistant", "content": result.content})

    return 0


def run_cli_task(
    task: str,
    repo: str = ".",
    mode: str = "enforce",
    tier: str = "cheap",
    max_turns: int = 20,
    session_id: Optional[str] = None,
) -> int:
    """Execute an autonomous task and display summary metrics."""
    from budgetpilot.agent.loop import run_agent_loop

    print("=" * 60)
    print(" BudgetPilot Autonomous Agent Run")
    print(f" Task: {task}")
    print(f" Repo: {repo}")
    print(f" Mode: {mode.upper()}")
    print(f" Tier: {tier}")
    print("=" * 60)
    print()

    result = run_agent_loop(
        prompt=task,
        repo_path=repo,
        mode=mode,
        max_turns=max_turns,
        session_id=session_id,
        tier=tier,
    )

    print()
    print("-" * 23 + " Final Answer " + "-" * 23)
    print(result.answer)
    print()
    print("-" * 21 + " Execution Summary " + "-" * 20)
    print(f"Status:          {result.status}")
    print(f"Turns:           {result.turns}")
    print(f"Tool Calls:      {result.tool_calls_count}")
    print(f"Files Read:      {len(result.read_files)} ({', '.join(result.read_files) if result.read_files else 'none'})")
    print(f"Files Modified:  {len(result.touched_files)} ({', '.join(result.touched_files) if result.touched_files else 'none'})")
    print()
    print("Tokens:")
    print(f"  Prompt Tokens: {result.prompt_tokens}")
    print(f"  Output Tokens: {result.output_tokens}")
    print(f"  Total Tokens:  {result.total_tokens}")
    print()
    print(f"Total Reference Cost: $ {result.total_cost_usd:.6f} USD")
    print("=" * 60)

    return 0 if result.status == "completed" else 1


def _print_single_session_terminal(data: Dict[str, Any]) -> None:
    """Format single session metrics for terminal output."""
    print("=" * 72)
    print(f" BudgetPilot Session Report: {data['session_id']}")
    print("=" * 72)
    print(f"  Status:          {data['status']}")
    print(f"  Mode:            {data['mode'].upper()}")
    print(f"  Pass/Fail:       {data['pass_fail']}")
    print(f"  Created At:      {data['created_at']}")
    print()

    # ── Metric 1: Measured Spend (exact tokens & reference USD) ─────────────
    print("-" * 23 + " [1] Measured Spend (Exact) " + "-" * 21)
    header = f"  {'Source':<14} {'Calls':<8} {'In Tokens':<12} {'Out Tokens':<12} {'Total Tokens':<14} {'Cost (USD)':<10}"
    print(header)
    print("  " + "-" * 68)
    for src in ("agent", "escalation"):
        item = data["cost_split"][src]
        print(f"  {src:<14} {item['calls']:<8} {item['input_tokens']:<12} {item['output_tokens']:<12} {item['total_tokens']:<14} ${item['cost_usd']:<10.6f}")
    print("  " + "-" * 68)
    tot = data["cost_split"]["total"]
    print(f"  {'Total':<14} {tot['calls']:<8} {tot['input_tokens']:<12} {tot['output_tokens']:<12} {tot['total_tokens']:<14} ${tot['cost_usd']:<10.6f}")
    print()

    # ── Metric 2: Modeled Avoided Spend (with 25th–75th percentile range) ───
    savings = data.get("avoided_estimate")
    print("-" * 20 + " [2] Modeled Avoided Spend (Never Merged) " + "-" * 10)
    if savings:
        print(f"  Gate Denies:       {savings['deny_count']}  (non-overridden: {savings['deny_count'] - savings['override_count'] - savings['late_read_count']})")
        print(f"  Gate Rewrites:     {savings['rewrite_count']}")
        print(f"  Override Deducted: {savings['override_count']}   Late-Read Deducted: {savings['late_read_count']}")
        print(f"  Net Tokens Avoided:{savings['net_tokens_avoided']:>10,}")
        print(f"  Turns Distribution: p25={savings['turns_p25']:.1f}  p50={savings['turns_p50']:.1f}  p75={savings['turns_p75']:.1f}")
        print(f"  Avoided Estimate:   Low  ${savings['avoided_low_usd']:.6f} USD")
        print(f"                      Mid  ${savings['avoided_mid_usd']:.6f} USD  ← point estimate")
        print(f"                      High ${savings['avoided_high_usd']:.6f} USD")
    else:
        print("  No gate activity recorded for this session. Avoided estimate: $0.000000")
    print()

    # ── Metric 3: Paired-Run Savings (benchmark harness only) ───────────────
    print("-" * 20 + " [3] Paired-Run Savings (Benchmark Harness Only) " + "-" * 2)
    paired = savings["paired_savings_usd"] if savings else 0.0
    if paired and paired != 0.0:
        print(f"  Paired-Run Savings: ${paired:.6f} USD  (ungated_cost - gated_cost)")
    else:
        print("  Paired-run savings not available (requires benchmark harness run).")
    print()

    # ── Tool Call Breakdown ──────────────────────────────────────────────────
    print("-" * 25 + " Tool Call Breakdown " + "-" * 26)
    if data["tool_calls"]:
        t_header = f"  {'Tool Name':<16} {'Total Calls':<14} {'Success':<10} {'Errors':<10} {'Avg Latency':<12}"
        print(t_header)
        print("  " + "-" * 68)
        for t in data["tool_calls"]:
            print(f"  {t['tool_name']:<16} {t['total_calls']:<14} {t['success_calls']:<10} {t['error_calls']:<10} {t['avg_duration']:.4f}s")
    else:
        print("  No tool calls recorded for this session.")
    print()

    # ── Gate Counts (Phase L) ────────────────────────────────────────────────
    gc = data.get("gate_counts", {})
    if gc:
        print("-" * 23 + " Gate Interception Counts " + "-" * 23)
        print(f"  Allow:    {gc.get('allow', 0):<8}  Rewrite: {gc.get('rewrite', 0):<8}")
        print(f"  Deny:     {gc.get('deny', 0):<8}  Overrides Deducted: {gc.get('override', 0):<8}")
        print()

    # ── Recovery Outcomes (Phase L) ──────────────────────────────────────────
    ro = data.get("recovery_outcomes", [])
    if ro:
        print("-" * 27 + " Recovery Outcomes " + "-" * 26)
        r_hdr = f"  {'Action':<16} {'Attempts':<10} {'Success':<10} {'Success %':<11} {'Avg Cost ($)':<12}"
        print(r_hdr)
        print("  " + "-" * 68)
        for r in ro:
            print(f"  {r['action']:<16} {r['total_attempts']:<10} {r['successes']:<10} {r['success_rate']:>6.1f}%     ${r['avg_cost_usd']:<10.6f}")
        print()

    # ── Cost Comparison (Phase L) ────────────────────────────────────────────
    cc = data.get("cost_comparison", {})
    if cc:
        print("-" * 28 + " Cost Comparison " + "-" * 27)
        print(f"  Measured Spend:           ${cc.get('measured_spend_usd', 0.0):.6f} USD")
        print(f"  Modeled Avoided Spend:    ${cc.get('modeled_avoided_mid_usd', 0.0):.6f} USD")
        print(f"  Baseline Equivalent Cost: ${cc.get('baseline_equivalent_usd', 0.0):.6f} USD")
        print(f"  Savings Ratio:            {cc.get('savings_percentage', 0.0):.1f}%")
        print()

    print("=" * 72)


def _print_single_session_md(data: Dict[str, Any]) -> None:
    """Format single session metrics for markdown output."""
    print(f"# BudgetPilot Session Report: `{data['session_id']}`\n")
    print(f"- **Status**: `{data['status']}`")
    print(f"- **Mode**: `{data['mode']}`")
    print(f"- **Pass/Fail**: `{data['pass_fail']}`")
    print(f"- **Created At**: `{data['created_at']}`")
    print(f"- **Total Spend**: `${data['cost_split']['total']['cost_usd']:.6f} USD`")
    print(f"- **Total Tokens**: `{data['tokens']['total_tokens']:,}`\n")

    # Metric 1: Measured Spend
    print("## [1] Measured Spend (Exact — Agent & Escalation)\n")
    print("| Source | Calls | Input Tokens | Output Tokens | Total Tokens | Reference Cost (USD) |")
    print("| :--- | :---: | :---: | :---: | :---: | :---: |")
    for src in ("agent", "escalation"):
        item = data["cost_split"][src]
        print(f"| {src.title()} | {item['calls']} | {item['input_tokens']:,} | {item['output_tokens']:,} | {item['total_tokens']:,} | ${item['cost_usd']:.6f} |")
    tot = data["cost_split"]["total"]
    print(f"| **Total** | **{tot['calls']}** | **{tot['input_tokens']:,}** | **{tot['output_tokens']:,}** | **{tot['total_tokens']:,}** | **${tot['cost_usd']:.6f}** |\n")

    # Metric 2: Modeled Avoided Spend
    print("## [2] Modeled Avoided Spend (Never Merged With Measured Spend)\n")
    savings = data.get("avoided_estimate")
    if savings:
        eff_denies = savings['deny_count'] - savings['override_count'] - savings['late_read_count']
        print("| Metric | Value |")
        print("| :--- | :---: |")
        print(f"| Gate Denies (total) | {savings['deny_count']} |")
        print(f"| Override Deducted | {savings['override_count']} |")
        print(f"| Late-Read Deducted | {savings['late_read_count']} |")
        print(f"| Effective Denies | {eff_denies} |")
        print(f"| Gate Rewrites | {savings['rewrite_count']} |")
        print(f"| Net Tokens Avoided | {savings['net_tokens_avoided']:,} |")
        print(f"| Turns p25/p50/p75 | {savings['turns_p25']:.1f} / {savings['turns_p50']:.1f} / {savings['turns_p75']:.1f} |")
        print(f"| **Avoided Low** (p25) | **${savings['avoided_low_usd']:.6f}** |")
        print(f"| **Avoided Mid** (p50) | **${savings['avoided_mid_usd']:.6f}** |")
        print(f"| **Avoided High** (p75) | **${savings['avoided_high_usd']:.6f}** |\n")
    else:
        print("_No gate activity recorded. Avoided estimate: $0.000000_\n")

    # Metric 3: Paired-Run Savings
    print("## [3] Paired-Run Savings (Benchmark Harness Only)\n")
    paired = savings["paired_savings_usd"] if savings else 0.0
    if paired and paired != 0.0:
        print(f"- **Paired-Run Savings**: `${paired:.6f} USD` _(ungated_cost − gated_cost)_\n")
    else:
        print("_Not available. Requires a matched benchmark harness run (bp bench baseline)._\n")

    # Gate Counts (Phase L)
    gc = data.get("gate_counts", {})
    if gc:
        print("## Gate Interception Counts\n")
        print("| Verdict | Count |")
        print("| :--- | :---: |")
        print(f"| Allow | {gc.get('allow', 0)} |")
        print(f"| Rewrite | {gc.get('rewrite', 0)} |")
        print(f"| Deny | {gc.get('deny', 0)} |")
        print(f"| Overrides Deducted | {gc.get('override', 0)} |\n")

    # Recovery Outcomes (Phase L)
    ro = data.get("recovery_outcomes", [])
    if ro:
        print("## Recovery Outcomes\n")
        print("| Action | Attempts | Successes | Success Rate | Avg Cost (USD) |")
        print("| :--- | :---: | :---: | :---: | :---: |")
        for r in ro:
            print(f"| `{r['action']}` | {r['total_attempts']} | {r['successes']} | {r['success_rate']:.1f}% | ${r['avg_cost_usd']:.6f} |")
        print()

    # Cost Comparison (Phase L)
    cc = data.get("cost_comparison", {})
    if cc:
        print("## Cost Comparison\n")
        print("| Metric | Amount (USD) |")
        print("| :--- | :---: |")
        print(f"| Measured Spend | ${cc.get('measured_spend_usd', 0.0):.6f} |")
        print(f"| Modeled Avoided Spend (Mid) | ${cc.get('modeled_avoided_mid_usd', 0.0):.6f} |")
        print(f"| Baseline Equivalent Cost | ${cc.get('baseline_equivalent_usd', 0.0):.6f} |")
        print(f"| **Savings Ratio** | **{cc.get('savings_percentage', 0.0):.1f}%** |\n")

    print("## Tool Call Breakdown\n")
    if data["tool_calls"]:
        print("| Tool Name | Total Calls | Success | Errors | Avg Latency |")
        print("| :--- | :---: | :---: | :---: | :---: |")
        for t in data["tool_calls"]:
            print(f"| `{t['tool_name']}` | {t['total_calls']} | {t['success_calls']} | {t['error_calls']} | {t['avg_duration']:.4f}s |")
    else:
        print("_No tool calls recorded._")
    print()


def _print_single_session_html(data: Dict[str, Any]) -> None:
    """Format single session metrics for HTML output."""
    import html
    sess_id = html.escape(str(data.get("session_id", "")))
    status = html.escape(str(data.get("status", "")))
    mode = html.escape(str(data.get("mode", "")))
    pass_fail = html.escape(str(data.get("pass_fail", "")))
    created_at = html.escape(str(data.get("created_at", "")))

    tot = data["cost_split"]["total"]
    agent = data["cost_split"]["agent"]
    esc = data["cost_split"]["escalation"]
    savings = data.get("avoided_estimate") or {}
    gc = data.get("gate_counts") or {}
    ro = data.get("recovery_outcomes") or []
    cc = data.get("cost_comparison") or {}
    tool_calls = data.get("tool_calls") or []

    pass_badge = "badge-success" if pass_fail == "PASSED" else ("badge-danger" if pass_fail == "FAILED" else "badge-warning")

    tool_rows = "".join(
        f"<tr><td><code>{html.escape(t['tool_name'])}</code></td><td>{t['total_calls']}</td>"
        f"<td>{t['success_calls']}</td><td>{t['error_calls']}</td><td>{t['avg_duration']:.4f}s</td></tr>"
        for t in tool_calls
    )

    ro_rows = "".join(
        f"<tr><td><code>{html.escape(r['action'])}</code></td><td>{r['total_attempts']}</td>"
        f"<td>{r['successes']}</td><td>{r['success_rate']:.1f}%</td><td>${r['avg_cost_usd']:.6f}</td></tr>"
        for r in ro
    )

    print(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BudgetPilot Report: {sess_id}</title>
    <style>
        :root {{
            --bg: #0f172a; --surface: #1e293b; --surface-border: #334155;
            --text-main: #f8fafc; --text-muted: #94a3b8; --primary: #38bdf8;
            --success: #4ade80; --warning: #facc15; --danger: #f87171;
            --font: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        }}
        body {{ background: var(--bg); color: var(--text-main); font-family: var(--font); margin: 0; padding: 32px 20px; display: flex; justify-content: center; }}
        .container {{ max-width: 980px; width: 100%; }}
        header {{ margin-bottom: 24px; border-bottom: 1px solid var(--surface-border); padding-bottom: 16px; }}
        h1 {{ font-size: 24px; margin: 0 0 6px 0; }}
        .card {{ background: var(--surface); border: 1px solid var(--surface-border); border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
        h2 {{ font-size: 16px; margin-top: 0; color: var(--primary); }}
        table {{ width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 8px; }}
        th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--surface-border); }}
        th {{ color: var(--text-muted); font-size: 12px; text-transform: uppercase; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin-bottom: 20px; }}
        .stat {{ background: #0f172a; border: 1px solid var(--surface-border); border-radius: 8px; padding: 14px; }}
        .stat-label {{ font-size: 11px; color: var(--text-muted); text-transform: uppercase; }}
        .stat-value {{ font-size: 20px; font-weight: 700; margin-top: 4px; }}
        .badge {{ display: inline-block; padding: 2px 8px; border-radius: 9999px; font-size: 12px; font-weight: 600; }}
        .badge-success {{ background: rgba(74, 222, 128, 0.15); color: var(--success); }}
        .badge-danger {{ background: rgba(248, 113, 113, 0.15); color: var(--danger); }}
        .badge-warning {{ background: rgba(250, 204, 21, 0.15); color: var(--warning); }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Session Report: <code>{sess_id}</code></h1>
            <div style="color: var(--text-muted); font-size: 13px;">Created: {created_at} &bull; Mode: <strong>{mode.upper()}</strong> &bull; Status: <span class="badge {pass_badge}">{pass_fail}</span></div>
        </header>

        <div class="grid">
            <div class="stat"><div class="stat-label">Measured Spend</div><div class="stat-value">${tot['cost_usd']:.6f}</div></div>
            <div class="stat"><div class="stat-label">Avoided Spend (Mid)</div><div class="stat-value" style="color: var(--success);">${savings.get('avoided_mid_usd', 0.0):.6f}</div></div>
            <div class="stat"><div class="stat-label">Savings Ratio</div><div class="stat-value" style="color: var(--primary);">{cc.get('savings_percentage', 0.0):.1f}%</div></div>
            <div class="stat"><div class="stat-label">Total Tokens</div><div class="stat-value">{data['tokens']['total_tokens']:,}</div></div>
        </div>

        <div class="card">
            <h2>[1] Measured Spend Breakdown</h2>
            <table>
                <thead><tr><th>Source</th><th>Calls</th><th>In Tokens</th><th>Out Tokens</th><th>Total Tokens</th><th>Cost (USD)</th></tr></thead>
                <tbody>
                    <tr><td>Agent</td><td>{agent['calls']}</td><td>{agent['input_tokens']:,}</td><td>{agent['output_tokens']:,}</td><td>{agent['total_tokens']:,}</td><td>${agent['cost_usd']:.6f}</td></tr>
                    <tr><td>Escalation</td><td>{esc['calls']}</td><td>{esc['input_tokens']:,}</td><td>{esc['output_tokens']:,}</td><td>{esc['total_tokens']:,}</td><td>${esc['cost_usd']:.6f}</td></tr>
                    <tr><td><strong>Total</strong></td><td><strong>{tot['calls']}</strong></td><td><strong>{tot['input_tokens']:,}</strong></td><td><strong>{tot['output_tokens']:,}</strong></td><td><strong>{tot['total_tokens']:,}</strong></td><td><strong>${tot['cost_usd']:.6f}</strong></td></tr>
                </tbody>
            </table>
        </div>

        <div class="card">
            <h2>Gate Interception Counts & Recovery Outcomes</h2>
            <div style="display: flex; gap: 12px; margin-bottom: 12px;">
                <span class="badge badge-success">Allow: {gc.get('allow', 0)}</span>
                <span class="badge badge-warning">Rewrite: {gc.get('rewrite', 0)}</span>
                <span class="badge badge-danger">Deny: {gc.get('deny', 0)}</span>
                <span class="badge" style="background: rgba(148, 163, 184, 0.15); color: var(--text-muted);">Overrides Deducted: {gc.get('override', 0)}</span>
            </div>
            {'<table><thead><tr><th>Action</th><th>Attempts</th><th>Successes</th><th>Success Rate</th><th>Avg Cost</th></tr></thead><tbody>' + ro_rows + '</tbody></table>' if ro else '<p style="color: var(--text-muted); font-size: 13px;">No recovery outcomes recorded.</p>'}
        </div>

        <div class="card">
            <h2>Tool Calls Breakdown</h2>
            {'<table><thead><tr><th>Tool</th><th>Calls</th><th>Success</th><th>Errors</th><th>Avg Duration</th></tr></thead><tbody>' + tool_rows + '</tbody></table>' if tool_calls else '<p style="color: var(--text-muted); font-size: 13px;">No tool calls recorded.</p>'}
        </div>
    </div>
</body>
</html>""")


def _print_all_sessions_terminal(data: Dict[str, Any]) -> None:
    """Format all sessions summary for terminal output."""
    print("=" * 76)
    print(" BudgetPilot All Sessions Report")
    print("=" * 76)
    print(f"  Total Sessions:   {data['total_sessions']}")
    print(f"  Passed / Done:    {data['passed_sessions']}")
    print(f"  Failed:           {data['failed_sessions']}")
    print(f"  Pass Rate:        {data['pass_rate']:.1f}%")
    print()

    # Metric 1: Measured Spend
    print("-" * 22 + " [1] Measured Spend (Exact) " + "-" * 22)
    print(f"  Agent Spend:      $ {data['cost_split']['agent']['cost_usd']:.6f} USD ({data['cost_split']['agent']['calls']} calls)")
    print(f"  Escalation Spend: $ {data['cost_split']['escalation']['cost_usd']:.6f} USD ({data['cost_split']['escalation']['calls']} calls)")
    print(f"  Total Spend:      $ {data['cost_split']['total']['cost_usd']:.6f} USD")
    print(f"  Total Tokens:     {data['tokens']['total_tokens']:,}")
    print()

    # Metric 2: Modeled Avoided Spend (aggregate across all sessions)
    agg = data.get("avoided_aggregate", {})
    print("-" * 18 + " [2] Modeled Avoided Spend (Never Merged) " + "-" * 14)
    if agg:
        print(f"  Total Gate Denies:  {agg.get('total_deny_count', 0)}  (overrides deducted: {agg.get('total_override_count', 0)})")
        print(f"  Total Gate Rewrites:{agg.get('total_rewrite_count', 0):<6}  Late-reads deducted: {agg.get('total_late_read_count', 0)}")
        print(f"  Net Tokens Avoided: {agg.get('total_net_tokens_avoided', 0):,}")
        print(f"  Avoided Estimate:   Low  ${agg.get('total_avoided_low_usd', 0.0):.6f} USD")
        print(f"                      Mid  ${agg.get('total_avoided_mid_usd', 0.0):.6f} USD  ← point estimate")
        print(f"                      High ${agg.get('total_avoided_high_usd', 0.0):.6f} USD")
    else:
        print("  No gate activity across sessions. Avoided estimate: $0.000000")
    print()

    # Metric 3: Paired-Run Savings
    print("-" * 18 + " [3] Paired-Run Savings (Benchmark Harness Only) " + "-" * 7)
    paired = agg.get("total_paired_savings_usd", 0.0) if agg else 0.0
    if paired and paired != 0.0:
        print(f"  Paired-Run Savings: ${paired:.6f} USD")
    else:
        print("  Not available (requires bp bench baseline paired runs).")
    print()

    if data["tool_calls"]:
        print("-" * 25 + " Tool Call Breakdown " + "-" * 26)
        t_header = f"  {'Tool Name':<16} {'Total Calls':<14} {'Success':<10} {'Errors':<10} {'Avg Latency':<12}"
        print(t_header)
        print("  " + "-" * 68)
        for t in data["tool_calls"]:
            print(f"  {t['tool_name']:<16} {t['total_calls']:<14} {t['success_calls']:<10} {t['error_calls']:<10} {t['avg_duration']:.4f}s")
        print()

    # ── Gate Counts (Phase L) ────────────────────────────────────────────────
    gc = data.get("gate_counts", {})
    if gc:
        print("-" * 23 + " Gate Interception Counts " + "-" * 27)
        print(f"  Total Allows:    {gc.get('allow', 0):<8}  Total Rewrites: {gc.get('rewrite', 0):<8}")
        print(f"  Total Denies:    {gc.get('deny', 0):<8}  Overrides Deducted: {gc.get('override', 0):<8}")
        print()

    # ── Recovery Outcomes (Phase L) ──────────────────────────────────────────
    ro = data.get("recovery_outcomes", [])
    if ro:
        print("-" * 25 + " Recovery Outcomes " + "-" * 30)
        r_hdr = f"  {'Action':<16} {'Attempts':<10} {'Success':<10} {'Success %':<11} {'Avg Cost ($)':<12}"
        print(r_hdr)
        print("  " + "-" * 68)
        for r in ro:
            print(f"  {r['action']:<16} {r['total_attempts']:<10} {r['successes']:<10} {r['success_rate']:>6.1f}%     ${r['avg_cost_usd']:<10.6f}")
        print()

    # ── Cost Comparison (Phase L) ────────────────────────────────────────────
    cc = data.get("cost_comparison", {})
    if cc:
        print("-" * 28 + " Cost Comparison " + "-" * 31)
        print(f"  Measured Spend:           ${cc.get('measured_spend_usd', 0.0):.6f} USD")
        print(f"  Modeled Avoided Spend:    ${cc.get('modeled_avoided_mid_usd', 0.0):.6f} USD")
        print(f"  Baseline Equivalent Cost: ${cc.get('baseline_equivalent_usd', 0.0):.6f} USD")
        print(f"  Savings Ratio:            {cc.get('savings_percentage', 0.0):.1f}%")
        print()

    print("-" * 27 + " Recent Sessions " + "-" * 28)
    s_header = f"  {'Session ID':<26} {'Mode':<10} {'Status':<10} {'Tokens':<10} {'Cost (USD)':<10}"
    print(s_header)
    print("  " + "-" * 70)
    for s in data["sessions"][:15]:
        print(f"  {s['session_id']:<26} {s['mode']:<10} {s['status']:<10} {s['tokens']:<10} ${s['cost_usd']:<10.6f}")
    print("=" * 76)


def _print_all_sessions_md(data: Dict[str, Any]) -> None:
    """Format all sessions summary for markdown output."""
    print("# BudgetPilot All Sessions Report\n")
    print(f"- **Total Sessions**: `{data['total_sessions']}`")
    print(f"- **Passed / Completed**: `{data['passed_sessions']}`")
    print(f"- **Failed**: `{data['failed_sessions']}`")
    print(f"- **Pass Rate**: `{data['pass_rate']:.1f}%`")
    print(f"- **Total Spend**: `${data['cost_split']['total']['cost_usd']:.6f} USD`")
    print(f"- **Agent Spend**: `${data['cost_split']['agent']['cost_usd']:.6f} USD`")
    print(f"- **Escalation Spend**: `${data['cost_split']['escalation']['cost_usd']:.6f} USD`")
    print(f"- **Total Tokens**: `{data['tokens']['total_tokens']:,}`\n")

    # Gate Counts (Phase L)
    gc = data.get("gate_counts", {})
    if gc:
        print("### Gate Interception Counts\n")
        print("| Verdict | Count |")
        print("| :--- | :---: |")
        print(f"| Total Allows | {gc.get('allow', 0)} |")
        print(f"| Total Rewrites | {gc.get('rewrite', 0)} |")
        print(f"| Total Denies | {gc.get('deny', 0)} |")
        print(f"| Overrides Deducted | {gc.get('override', 0)} |\n")

    # Recovery Outcomes (Phase L)
    ro = data.get("recovery_outcomes", [])
    if ro:
        print("### Recovery Outcomes\n")
        print("| Action | Attempts | Successes | Success Rate | Avg Cost (USD) |")
        print("| :--- | :---: | :---: | :---: | :---: |")
        for r in ro:
            print(f"| `{r['action']}` | {r['total_attempts']} | {r['successes']} | {r['success_rate']:.1f}% | ${r['avg_cost_usd']:.6f} |")
        print()

    # Cost Comparison (Phase L)
    cc = data.get("cost_comparison", {})
    if cc:
        print("### Cost Comparison\n")
        print("| Metric | Amount (USD) |")
        print("| :--- | :---: |")
        print(f"| Measured Spend | ${cc.get('measured_spend_usd', 0.0):.6f} |")
        print(f"| Modeled Avoided Spend (Mid) | ${cc.get('modeled_avoided_mid_usd', 0.0):.6f} |")
        print(f"| Baseline Equivalent Cost | ${cc.get('baseline_equivalent_usd', 0.0):.6f} |")
        print(f"| **Savings Ratio** | **{cc.get('savings_percentage', 0.0):.1f}%** |\n")

    print("### Tool Call Breakdown\n")
    if data["tool_calls"]:
        print("| Tool Name | Total Calls | Success | Errors | Avg Latency |")
        print("| :--- | :---: | :---: | :---: | :---: |")
        for t in data["tool_calls"]:
            print(f"| `{t['tool_name']}` | {t['total_calls']} | {t['success_calls']} | {t['error_calls']} | {t['avg_duration']:.4f}s |")
    else:
        print("No tool calls recorded.")
    print("\n### Recent Sessions\n")
    print("| Session ID | Mode | Status | Tokens | Reference Cost (USD) |")
    print("| :--- | :---: | :---: | :---: | :---: |")
    for s in data["sessions"][:20]:
        print(f"| `{s['session_id']}` | {s['mode']} | {s['status']} | {s['tokens']:,} | ${s['cost_usd']:.6f} |")
    print()


def _print_all_sessions_html(data: Dict[str, Any]) -> None:
    """Format all sessions summary as self-contained HTML page."""
    import html
    tot = data["cost_split"]["total"]
    agent = data["cost_split"]["agent"]
    esc = data["cost_split"]["escalation"]
    agg = data.get("avoided_aggregate") or {}
    gc = data.get("gate_counts") or {}
    ro = data.get("recovery_outcomes") or []
    cc = data.get("cost_comparison") or {}
    tool_calls = data.get("tool_calls") or []
    sessions = data.get("sessions") or []

    tool_rows = "".join(
        f"<tr><td><code>{html.escape(t['tool_name'])}</code></td><td>{t['total_calls']}</td>"
        f"<td>{t['success_calls']}</td><td>{t['error_calls']}</td><td>{t['avg_duration']:.4f}s</td></tr>"
        for t in tool_calls
    )

    ro_rows = "".join(
        f"<tr><td><code>{html.escape(r['action'])}</code></td><td>{r['total_attempts']}</td>"
        f"<td>{r['successes']}</td><td>{r['success_rate']:.1f}%</td><td>${r['avg_cost_usd']:.6f}</td></tr>"
        for r in ro
    )

    sess_rows = "".join(
        f"<tr><td><code>{html.escape(s['session_id'])}</code></td><td>{html.escape(s['mode'])}</td>"
        f"<td><span class=\"badge {'badge-success' if s['status'] in ('done', 'completed') else 'badge-danger'}\">{html.escape(s['status'])}</span></td>"
        f"<td>{s['tokens']:,}</td><td><strong>${s['cost_usd']:.6f}</strong></td></tr>"
        for s in sessions[:25]
    )

    print(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BudgetPilot All Sessions Report</title>
    <style>
        :root {{
            --bg: #0f172a; --surface: #1e293b; --surface-border: #334155;
            --text-main: #f8fafc; --text-muted: #94a3b8; --primary: #38bdf8;
            --success: #4ade80; --warning: #facc15; --danger: #f87171;
            --font: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        }}
        body {{ background: var(--bg); color: var(--text-main); font-family: var(--font); margin: 0; padding: 32px 20px; display: flex; justify-content: center; }}
        .container {{ max-width: 1040px; width: 100%; }}
        header {{ margin-bottom: 24px; border-bottom: 1px solid var(--surface-border); padding-bottom: 16px; }}
        h1 {{ font-size: 26px; margin: 0 0 6px 0; }}
        .card {{ background: var(--surface); border: 1px solid var(--surface-border); border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
        h2 {{ font-size: 16px; margin-top: 0; color: var(--primary); }}
        table {{ width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 8px; }}
        th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--surface-border); }}
        th {{ color: var(--text-muted); font-size: 12px; text-transform: uppercase; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin-bottom: 20px; }}
        .stat {{ background: #0f172a; border: 1px solid var(--surface-border); border-radius: 8px; padding: 14px; }}
        .stat-label {{ font-size: 11px; color: var(--text-muted); text-transform: uppercase; }}
        .stat-value {{ font-size: 20px; font-weight: 700; margin-top: 4px; }}
        .badge {{ display: inline-block; padding: 2px 8px; border-radius: 9999px; font-size: 12px; font-weight: 600; }}
        .badge-success {{ background: rgba(74, 222, 128, 0.15); color: var(--success); }}
        .badge-danger {{ background: rgba(248, 113, 113, 0.15); color: var(--danger); }}
        .badge-warning {{ background: rgba(250, 204, 21, 0.15); color: var(--warning); }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>BudgetPilot All Sessions Report</h1>
            <div style="color: var(--text-muted); font-size: 13px;">Total Sessions: <strong>{data['total_sessions']}</strong> &bull; Pass Rate: <strong>{data['pass_rate']:.1f}%</strong></div>
        </header>

        <div class="grid">
            <div class="stat"><div class="stat-label">Total Measured Spend</div><div class="stat-value">${tot['cost_usd']:.6f}</div></div>
            <div class="stat"><div class="stat-label">Total Avoided Spend (Mid)</div><div class="stat-value" style="color: var(--success);">${agg.get('total_avoided_mid_usd', 0.0):.6f}</div></div>
            <div class="stat"><div class="stat-label">Historical Savings Ratio</div><div class="stat-value" style="color: var(--primary);">{cc.get('savings_percentage', 0.0):.1f}%</div></div>
            <div class="stat"><div class="stat-label">Total Tokens</div><div class="stat-value">{data['tokens']['total_tokens']:,}</div></div>
        </div>

        <div class="card">
            <h2>Measured Spend Breakdown</h2>
            <table>
                <thead><tr><th>Source</th><th>Calls</th><th>Cost (USD)</th></tr></thead>
                <tbody>
                    <tr><td>Agent</td><td>{agent['calls']}</td><td>${agent['cost_usd']:.6f}</td></tr>
                    <tr><td>Escalation</td><td>{esc['calls']}</td><td>${esc['cost_usd']:.6f}</td></tr>
                    <tr><td><strong>Total</strong></td><td><strong>{tot['calls']}</strong></td><td><strong>${tot['cost_usd']:.6f}</strong></td></tr>
                </tbody>
            </table>
        </div>

        <div class="card">
            <h2>Gate Interception Counts & Recovery Outcomes</h2>
            <div style="display: flex; gap: 12px; margin-bottom: 12px;">
                <span class="badge badge-success">Allows: {gc.get('allow', 0)}</span>
                <span class="badge badge-warning">Rewrites: {gc.get('rewrite', 0)}</span>
                <span class="badge badge-danger">Denies: {gc.get('deny', 0)}</span>
                <span class="badge" style="background: rgba(148, 163, 184, 0.15); color: var(--text-muted);">Overrides Deducted: {gc.get('override', 0)}</span>
            </div>
            {'<table><thead><tr><th>Action</th><th>Attempts</th><th>Successes</th><th>Success Rate</th><th>Avg Cost</th></tr></thead><tbody>' + ro_rows + '</tbody></table>' if ro else '<p style="color: var(--text-muted); font-size: 13px;">No recovery outcomes recorded.</p>'}
        </div>

        <div class="card">
            <h2>Recent Sessions</h2>
            {'<table><thead><tr><th>Session ID</th><th>Mode</th><th>Status</th><th>Tokens</th><th>Cost</th></tr></thead><tbody>' + sess_rows + '</tbody></table>' if sessions else '<p style="color: var(--text-muted); font-size: 13px;">No sessions logged.</p>'}
        </div>
    </div>
</body>
</html>""")


def _build_avoided_estimate_data(
    session_id: str,
    tier: str = "cheap",
    db_path: Optional[Union[Path, str]] = None,
) -> Optional[Dict[str, Any]]:
    """Build the avoided_estimate data dict for report rendering."""
    try:
        from budgetpilot.core.savings import compute_avoided_estimate
        est = compute_avoided_estimate(session_id=session_id, tier=tier, db_path=db_path, persist=True)
        return {
            "deny_count": est.deny_count,
            "rewrite_count": est.rewrite_count,
            "override_count": est.override_count,
            "late_read_count": est.late_read_count,
            "net_tokens_avoided": est.net_tokens_avoided,
            "turns_p25": est.turns_p25,
            "turns_p50": est.turns_p50,
            "turns_p75": est.turns_p75,
            "avoided_low_usd": est.avoided_low_usd,
            "avoided_mid_usd": est.avoided_mid_usd,
            "avoided_high_usd": est.avoided_high_usd,
            "paired_savings_usd": est.paired_savings_usd,
            "measured_spend_usd": est.measured_spend_usd,
        }
    except Exception as exc:
        import logging
        logging.getLogger(__name__).debug("Could not build avoided estimate: %s", exc)
        return None


def run_report(
    session_id: Optional[str] = None,
    format_type: str = "terminal",
    db_path: Optional[Union[Path, str]] = None,
) -> int:
    """
    Generate measured metrics reports from SQLite database.
    Supports single session or all sessions, with terminal, markdown, or JSON formats.
    """
    fmt = (format_type or "terminal").lower()
    init_db(db_path)
    conn = get_connection(db_path)

    try:
        cursor = conn.execute("SELECT COUNT(*) FROM sessions;")
        total_sessions = cursor.fetchone()[0]
        if total_sessions == 0:
            if fmt == "json":
                print(json.dumps({"sessions": [], "message": "No sessions logged"}, indent=2))
            else:
                print("No sessions logged.")
            return 0

        if session_id:
            cursor = conn.execute(
                """
                SELECT session_id, created_at, updated_at, mode, budget_slider, total_cost, total_tokens, status, metadata
                FROM sessions WHERE session_id = ?;
                """,
                (session_id,),
            )
            sess_row = cursor.fetchone()
            if not sess_row:
                if fmt == "json":
                    print(json.dumps({"session_id": session_id, "found": False, "message": f"No session found for ID '{session_id}'"}, indent=2))
                else:
                    print(f"No session found for ID '{session_id}'.")
                return 0

            # Model calls split by source (agent vs escalation)
            cursor = conn.execute(
                """
                SELECT 
                    CASE 
                        WHEN source IN ('escalation', 'recovery') THEN 'escalation'
                        ELSE 'agent'
                    END AS cost_source,
                    COUNT(*) AS call_count,
                    COALESCE(SUM(input_tokens), 0) AS in_tokens,
                    COALESCE(SUM(output_tokens), 0) AS out_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(estimated_cost), 0.0) AS cost_usd
                FROM model_calls
                WHERE session_id = ?
                GROUP BY cost_source;
                """,
                (session_id,),
            )
            model_rows = {r["cost_source"]: dict(r) for r in cursor.fetchall()}

            # Tool events breakdown
            cursor = conn.execute(
                """
                SELECT 
                    tool_name,
                    COUNT(*) AS total_calls,
                    SUM(CASE WHEN is_error = 0 THEN 1 ELSE 0 END) AS success_calls,
                    SUM(CASE WHEN is_error != 0 THEN 1 ELSE 0 END) AS error_calls,
                    COALESCE(AVG(duration), 0.0) AS avg_duration
                FROM tool_events
                WHERE session_id = ?
                GROUP BY tool_name
                ORDER BY total_calls DESC;
                """,
                (session_id,),
            )
            tool_rows = [dict(r) for r in cursor.fetchall()]

            # Report table row
            cursor = conn.execute(
                """
                SELECT report_type, summary, total_cost_usd, savings_usd, metadata
                FROM reports WHERE session_id = ? ORDER BY id DESC LIMIT 1;
                """,
                (session_id,),
            )
            rep_row = cursor.fetchone()

            sess_meta = {}
            if sess_row["metadata"]:
                try:
                    sess_meta = json.loads(sess_row["metadata"])
                except Exception:
                    pass

            rep_meta = {}
            if rep_row and rep_row["metadata"]:
                try:
                    rep_meta = json.loads(rep_row["metadata"])
                except Exception:
                    pass

            passed_val = None
            if "passed" in rep_meta:
                passed_val = bool(rep_meta["passed"])
            elif "passed" in sess_meta:
                passed_val = bool(sess_meta["passed"])
            elif sess_row["status"] in ("done", "completed"):
                passed_val = True
            elif sess_row["status"] == "failed":
                passed_val = False

            pass_fail_label = "PASSED" if passed_val is True else ("FAILED" if passed_val is False else "N/A")

            agent_data = model_rows.get("agent", {"call_count": 0, "in_tokens": 0, "out_tokens": 0, "total_tokens": 0, "cost_usd": 0.0})
            escalation_data = model_rows.get("escalation", {"call_count": 0, "in_tokens": 0, "out_tokens": 0, "total_tokens": 0, "cost_usd": 0.0})
            total_calls = agent_data["call_count"] + escalation_data["call_count"]
            total_in = agent_data["in_tokens"] + escalation_data["in_tokens"]
            total_out = agent_data["out_tokens"] + escalation_data["out_tokens"]
            total_toks = agent_data["total_tokens"] + escalation_data["total_tokens"]
            total_cost = agent_data["cost_usd"] + escalation_data["cost_usd"]

            if total_cost == 0.0 and sess_row["total_cost"]:
                total_cost = float(sess_row["total_cost"])
            if total_toks == 0 and sess_row["total_tokens"]:
                total_toks = int(sess_row["total_tokens"])

            # Build avoided estimate (Phase I) – never merged with measured spend
            avoided_data = _build_avoided_estimate_data(
                session_id=session_id, db_path=db_path
            )

            # Gate decisions count (Phase L)
            cursor = conn.execute(
                """
                SELECT verdict, COUNT(*) as cnt
                FROM gate_decisions
                WHERE session_id = ?
                GROUP BY verdict;
                """,
                (session_id,),
            )
            gate_counts = {"allow": 0, "rewrite": 0, "deny": 0, "override": 0}
            for gr in cursor.fetchall():
                v = str(gr["verdict"]).lower()
                if v in gate_counts:
                    gate_counts[v] = int(gr["cnt"])
            if avoided_data and "override_count" in avoided_data:
                gate_counts["override"] = int(avoided_data["override_count"])

            # Recovery outcomes (Phase L)
            cursor = conn.execute(
                """
                SELECT 
                    action,
                    COUNT(*) as total_attempts,
                    SUM(CASE WHEN succeeded = 1 THEN 1 ELSE 0 END) as successes,
                    COALESCE(AVG(cost_usd), 0.0) as avg_cost,
                    COALESCE(SUM(cost_usd), 0.0) as total_cost
                FROM recovery_outcomes
                WHERE session_id = ?
                GROUP BY action
                ORDER BY total_attempts DESC;
                """,
                (session_id,),
            )
            recovery_outcomes = [
                {
                    "action": r["action"],
                    "total_attempts": int(r["total_attempts"]),
                    "successes": int(r["successes"] or 0),
                    "success_rate": round(int(r["successes"] or 0) / int(r["total_attempts"]) * 100.0, 1) if int(r["total_attempts"]) > 0 else 0.0,
                    "avg_cost_usd": round(float(r["avg_cost"] or 0.0), 6),
                    "total_cost_usd": round(float(r["total_cost"] or 0.0), 6),
                }
                for r in cursor.fetchall()
            ]

            # Cost comparisons (Phase L)
            meas_cost = float(total_cost)
            avoid_mid = float(avoided_data["avoided_mid_usd"]) if avoided_data else 0.0
            paired_sav = float(avoided_data["paired_savings_usd"]) if (avoided_data and avoided_data.get("paired_savings_usd")) else 0.0
            base_equiv = meas_cost + avoid_mid
            savings_pct = (avoid_mid / base_equiv * 100.0) if base_equiv > 0 else 0.0

            cost_comparison = {
                "measured_spend_usd": round(meas_cost, 6),
                "modeled_avoided_mid_usd": round(avoid_mid, 6),
                "modeled_avoided_low_usd": round(float(avoided_data["avoided_low_usd"]), 6) if avoided_data else 0.0,
                "modeled_avoided_high_usd": round(float(avoided_data["avoided_high_usd"]), 6) if avoided_data else 0.0,
                "baseline_equivalent_usd": round(base_equiv, 6),
                "savings_percentage": round(savings_pct, 2),
                "paired_savings_usd": round(paired_sav, 6),
            }

            report_data = {
                "session_id": session_id,
                "created_at": str(sess_row["created_at"]),
                "status": sess_row["status"],
                "mode": sess_row["mode"],
                "pass_fail": pass_fail_label,
                "passed": passed_val,
                "tokens": {
                    "input_tokens": total_in,
                    "output_tokens": total_out,
                    "total_tokens": total_toks,
                },
                "avoided_estimate": avoided_data,
                "gate_counts": gate_counts,
                "recovery_outcomes": recovery_outcomes,
                "cost_comparison": cost_comparison,
                "cost_split": {
                    "agent": {
                        "calls": agent_data["call_count"],
                        "input_tokens": agent_data["in_tokens"],
                        "output_tokens": agent_data["out_tokens"],
                        "total_tokens": agent_data["total_tokens"],
                        "cost_usd": round(agent_data["cost_usd"], 6),
                    },
                    "escalation": {
                        "calls": escalation_data["call_count"],
                        "input_tokens": escalation_data["in_tokens"],
                        "output_tokens": escalation_data["out_tokens"],
                        "total_tokens": escalation_data["total_tokens"],
                        "cost_usd": round(escalation_data["cost_usd"], 6),
                    },
                    "total": {
                        "calls": total_calls,
                        "input_tokens": total_in,
                        "output_tokens": total_out,
                        "total_tokens": total_toks,
                        "cost_usd": round(total_cost, 6),
                    },
                },
                "tool_calls": [
                    {
                        "tool_name": r["tool_name"],
                        "total_calls": r["total_calls"],
                        "success_calls": r["success_calls"],
                        "error_calls": r["error_calls"],
                        "avg_duration": round(r["avg_duration"], 4),
                    }
                    for r in tool_rows
                ],
            }

            if fmt == "json":
                print(json.dumps(report_data, indent=2))
            elif fmt in ("md", "markdown"):
                _print_single_session_md(report_data)
            elif fmt == "html":
                _print_single_session_html(report_data)
            else:
                _print_single_session_terminal(report_data)

            return 0

        else:
            cursor = conn.execute(
                """
                SELECT session_id, created_at, mode, total_cost, total_tokens, status, metadata
                FROM sessions
                ORDER BY created_at DESC;
                """
            )
            sessions_list = [dict(r) for r in cursor.fetchall()]

            cursor = conn.execute(
                """
                SELECT 
                    CASE 
                        WHEN source IN ('escalation', 'recovery') THEN 'escalation'
                        ELSE 'agent'
                    END AS cost_source,
                    COUNT(*) AS call_count,
                    COALESCE(SUM(input_tokens), 0) AS in_tokens,
                    COALESCE(SUM(output_tokens), 0) AS out_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(estimated_cost), 0.0) AS cost_usd
                FROM model_calls
                GROUP BY cost_source;
                """
            )
            model_rows = {r["cost_source"]: dict(r) for r in cursor.fetchall()}

            cursor = conn.execute(
                """
                SELECT 
                    tool_name,
                    COUNT(*) AS total_calls,
                    SUM(CASE WHEN is_error = 0 THEN 1 ELSE 0 END) AS success_calls,
                    SUM(CASE WHEN is_error != 0 THEN 1 ELSE 0 END) AS error_calls,
                    COALESCE(AVG(duration), 0.0) AS avg_duration
                FROM tool_events
                GROUP BY tool_name
                ORDER BY total_calls DESC;
                """
            )
            tool_rows = [dict(r) for r in cursor.fetchall()]

            passed_count = sum(1 for s in sessions_list if s["status"] in ("done", "completed"))
            failed_count = sum(1 for s in sessions_list if s["status"] == "failed")
            pass_rate = (passed_count / len(sessions_list) * 100.0) if sessions_list else 0.0

            agent_data = model_rows.get("agent", {"call_count": 0, "in_tokens": 0, "out_tokens": 0, "total_tokens": 0, "cost_usd": 0.0})
            escalation_data = model_rows.get("escalation", {"call_count": 0, "in_tokens": 0, "out_tokens": 0, "total_tokens": 0, "cost_usd": 0.0})
            total_calls = agent_data["call_count"] + escalation_data["call_count"]
            total_in = agent_data["in_tokens"] + escalation_data["in_tokens"]
            total_out = agent_data["out_tokens"] + escalation_data["out_tokens"]
            total_toks = agent_data["total_tokens"] + escalation_data["total_tokens"]
            total_cost = agent_data["cost_usd"] + escalation_data["cost_usd"]

            # Build aggregate avoided estimates across all sessions
            avoided_aggregate: Dict[str, Any] = {
                "total_deny_count": 0,
                "total_rewrite_count": 0,
                "total_override_count": 0,
                "total_late_read_count": 0,
                "total_net_tokens_avoided": 0,
                "total_avoided_low_usd": 0.0,
                "total_avoided_mid_usd": 0.0,
                "total_avoided_high_usd": 0.0,
                "total_paired_savings_usd": 0.0,
            }
            for s in sessions_list:
                sid = s["session_id"]
                s_avoided = _build_avoided_estimate_data(session_id=sid, db_path=db_path)
                if s_avoided:
                    avoided_aggregate["total_deny_count"] += s_avoided["deny_count"]
                    avoided_aggregate["total_rewrite_count"] += s_avoided["rewrite_count"]
                    avoided_aggregate["total_override_count"] += s_avoided["override_count"]
                    avoided_aggregate["total_late_read_count"] += s_avoided["late_read_count"]
                    avoided_aggregate["total_net_tokens_avoided"] += s_avoided["net_tokens_avoided"]
                    avoided_aggregate["total_avoided_low_usd"] += s_avoided["avoided_low_usd"]
                    avoided_aggregate["total_avoided_mid_usd"] += s_avoided["avoided_mid_usd"]
                    avoided_aggregate["total_avoided_high_usd"] += s_avoided["avoided_high_usd"]
                    avoided_aggregate["total_paired_savings_usd"] += s_avoided["paired_savings_usd"]

            # Gate decisions count across all sessions (Phase L)
            cursor = conn.execute(
                """
                SELECT verdict, COUNT(*) as cnt
                FROM gate_decisions
                GROUP BY verdict;
                """
            )
            gate_counts = {"allow": 0, "rewrite": 0, "deny": 0, "override": 0}
            for gr in cursor.fetchall():
                v = str(gr["verdict"]).lower()
                if v in gate_counts:
                    gate_counts[v] = int(gr["cnt"])
            gate_counts["override"] = int(avoided_aggregate.get("total_override_count", 0))

            # Recovery outcomes across all sessions (Phase L)
            cursor = conn.execute(
                """
                SELECT 
                    action,
                    COUNT(*) as total_attempts,
                    SUM(CASE WHEN succeeded = 1 THEN 1 ELSE 0 END) as successes,
                    COALESCE(AVG(cost_usd), 0.0) as avg_cost,
                    COALESCE(SUM(cost_usd), 0.0) as total_cost
                FROM recovery_outcomes
                GROUP BY action
                ORDER BY total_attempts DESC;
                """
            )
            recovery_outcomes = [
                {
                    "action": r["action"],
                    "total_attempts": int(r["total_attempts"]),
                    "successes": int(r["successes"] or 0),
                    "success_rate": round(int(r["successes"] or 0) / int(r["total_attempts"]) * 100.0, 1) if int(r["total_attempts"]) > 0 else 0.0,
                    "avg_cost_usd": round(float(r["avg_cost"] or 0.0), 6),
                    "total_cost_usd": round(float(r["total_cost"] or 0.0), 6),
                }
                for r in cursor.fetchall()
            ]

            # Cost comparisons across all sessions (Phase L)
            meas_cost = float(total_cost)
            avoid_mid = float(avoided_aggregate.get("total_avoided_mid_usd", 0.0))
            paired_sav = float(avoided_aggregate.get("total_paired_savings_usd", 0.0))
            base_equiv = meas_cost + avoid_mid
            savings_pct = (avoid_mid / base_equiv * 100.0) if base_equiv > 0 else 0.0

            cost_comparison = {
                "measured_spend_usd": round(meas_cost, 6),
                "modeled_avoided_mid_usd": round(avoid_mid, 6),
                "modeled_avoided_low_usd": round(float(avoided_aggregate.get("total_avoided_low_usd", 0.0)), 6),
                "modeled_avoided_high_usd": round(float(avoided_aggregate.get("total_avoided_high_usd", 0.0)), 6),
                "baseline_equivalent_usd": round(base_equiv, 6),
                "savings_percentage": round(savings_pct, 2),
                "paired_savings_usd": round(paired_sav, 6),
            }

            summary_data = {
                "total_sessions": len(sessions_list),
                "passed_sessions": passed_count,
                "failed_sessions": failed_count,
                "pass_rate": round(pass_rate, 2),
                "tokens": {
                    "input_tokens": total_in,
                    "output_tokens": total_out,
                    "total_tokens": total_toks,
                },
                "cost_split": {
                    "agent": {
                        "calls": agent_data["call_count"],
                        "cost_usd": round(agent_data["cost_usd"], 6),
                    },
                    "escalation": {
                        "calls": escalation_data["call_count"],
                        "cost_usd": round(escalation_data["cost_usd"], 6),
                    },
                    "total": {
                        "calls": total_calls,
                        "cost_usd": round(total_cost, 6),
                    },
                },
                "gate_counts": gate_counts,
                "recovery_outcomes": recovery_outcomes,
                "cost_comparison": cost_comparison,
                "tool_calls": [
                    {
                        "tool_name": r["tool_name"],
                        "total_calls": r["total_calls"],
                        "success_calls": r["success_calls"],
                        "error_calls": r["error_calls"],
                        "avg_duration": round(r["avg_duration"], 4),
                    }
                    for r in tool_rows
                ],
                "avoided_aggregate": avoided_aggregate,
                "sessions": [
                    {
                        "session_id": s["session_id"],
                        "created_at": str(s["created_at"]),
                        "mode": s["mode"],
                        "status": s["status"],
                        "tokens": s["total_tokens"] or 0,
                        "cost_usd": round(float(s["total_cost"] or 0.0), 6),
                    }
                    for s in sessions_list
                ],
            }

            if fmt == "json":
                print(json.dumps(summary_data, indent=2))
            elif fmt in ("md", "markdown"):
                _print_all_sessions_md(summary_data)
            elif fmt == "html":
                _print_all_sessions_html(summary_data)
            else:
                _print_all_sessions_terminal(summary_data)

            return 0

    finally:
        conn.close()


def run_estimator_eval(
    repo_path: str = ".",
    limit: int = 20,
    threshold: float = 0.35,
    format_type: str = "terminal",
) -> int:
    """
    Evaluate scope predictions against target repo commit history.
    Extracts commit messages as task prompts and files changed as ground truth:
    - 1 file changed -> Scope 1
    - 2-5 files changed -> Scope 2
    - >5 files changed -> Scope 3
    """
    from budgetpilot.core.estimator import estimate

    target_dir = Path(repo_path).expanduser().resolve()
    if not target_dir.exists() or not target_dir.is_dir():
        print(f"Error: Directory '{repo_path}' does not exist.")
        return 1

    # Verify target_dir is a git repository
    try:
        check_git = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if check_git.returncode != 0:
            print(f"Error: '{repo_path}' is not a git repository.")
            return 1
    except Exception as exc:
        print(f"Error checking git repository: {exc}")
        return 1

    # Get recent commits
    try:
        log_res = subprocess.run(
            ["git", "log", f"-n{max(1, limit)}", "--format=%H%x00%s"],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if log_res.returncode != 0 or not log_res.stdout.strip():
            if format_type == "json":
                print(json.dumps({"error": "No commits found in repository", "commits_evaluated": 0}, indent=2))
            else:
                print(f"No commits found in repository '{repo_path}'.")
            return 0
    except Exception as exc:
        print(f"Error reading git commits: {exc}")
        return 1

    commit_lines = [line.strip() for line in log_res.stdout.splitlines() if line.strip()]
    results: List[Dict[str, Any]] = []

    for line in commit_lines:
        parts = line.split("\x00", 1)
        commit_hash = parts[0].strip()
        commit_subject = parts[1].strip() if len(parts) > 1 else ""

        if not commit_subject:
            continue

        # Get changed files for commit
        show_res = subprocess.run(
            ["git", "show", "--format=", "--name-only", commit_hash],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if show_res.returncode != 0:
            continue

        changed_files = [f.strip() for f in show_res.stdout.splitlines() if f.strip()]
        file_count = len(changed_files)
        if file_count == 0:
            continue

        # Ground truth mapping: 1 -> Level 1; 2-5 -> Level 2; >5 -> Level 3
        if file_count == 1:
            ground_truth = 1
        elif 2 <= file_count <= 5:
            ground_truth = 2
        else:
            ground_truth = 3

        prediction = estimate(
            prompt=commit_subject,
            repo_path=str(target_dir),
            threshold=threshold,
        )

        pred_scope = prediction.scope
        confidence = prediction.confidence
        latency_ms = prediction.metadata.get("latency_ms", 0.0)
        round_up = prediction.metadata.get("low_confidence_round_up", False)

        if pred_scope == ground_truth:
            status = "MATCH"
        elif pred_scope < ground_truth:
            status = "UNDER"
        else:
            status = "OVER"

        results.append({
            "commit": commit_hash[:8],
            "full_hash": commit_hash,
            "message": commit_subject,
            "files_count": file_count,
            "changed_files": changed_files[:5],
            "ground_truth": ground_truth,
            "predicted": pred_scope,
            "confidence": round(confidence, 2),
            "low_confidence_round_up": round_up,
            "latency_ms": round(latency_ms, 2),
            "status": status,
        })

    total_evaluated = len(results)
    if total_evaluated == 0:
        if format_type == "json":
            print(json.dumps({"error": "No non-empty commits evaluated", "commits_evaluated": 0}, indent=2))
        else:
            print(f"No non-empty commits found to evaluate in '{repo_path}'.")
        return 0

    matches = sum(1 for r in results if r["status"] == "MATCH")
    under_scoped = sum(1 for r in results if r["status"] == "UNDER")
    over_scoped = sum(1 for r in results if r["status"] == "OVER")
    accuracy_pct = (matches / total_evaluated) * 100.0
    under_rate_pct = (under_scoped / total_evaluated) * 100.0
    over_rate_pct = (over_scoped / total_evaluated) * 100.0
    avg_latency_ms = sum(r["latency_ms"] for r in results) / total_evaluated
    max_latency_ms = max(r["latency_ms"] for r in results)

    if format_type == "json":
        output_payload = {
            "repo": str(target_dir),
            "commits_evaluated": total_evaluated,
            "exact_matches": matches,
            "accuracy_pct": round(accuracy_pct, 2),
            "under_scoped_count": under_scoped,
            "under_scoped_rate_pct": round(under_rate_pct, 2),
            "over_scoped_count": over_scoped,
            "over_scoped_rate_pct": round(over_rate_pct, 2),
            "avg_latency_ms": round(avg_latency_ms, 2),
            "max_latency_ms": round(max_latency_ms, 2),
            "threshold": threshold,
            "results": results,
        }
        print(json.dumps(output_payload, indent=2))
        return 0

    # Terminal output
    print("=" * 95)
    print(" BudgetPilot Scope Estimator Evaluation (Phase D)")
    print(f" Repository: {target_dir.name} | Commits: {total_evaluated} | Threshold: {threshold}")
    print("=" * 95)
    print(f" {'COMMIT':<8}  {'FILES':>5}  {'ACTUAL':>6}  {'PRED':>5}  {'CONF':>5}  {'ROUND':>6}  {'LATENCY':>8}  {'STATUS':<6}  {'MESSAGE'}")
    print("-" * 95)
    for r in results:
        msg_snip = r["message"][:34] + ".." if len(r["message"]) > 36 else r["message"]
        round_str = "Yes" if r["low_confidence_round_up"] else "No"
        print(
            f" {r['commit']:<8}  {r['files_count']:>5}  {r['ground_truth']:>6}  {r['predicted']:>5}  "
            f"{r['confidence']:>5.2f}  {round_str:>6}  {r['latency_ms']:>6.1f}ms  {r['status']:<6}  {msg_snip}"
        )
    print("=" * 95)
    print(" Summary Metrics:")
    print(f" - Commits Evaluated:  {total_evaluated}")
    print(f" - Exact Accuracy:     {accuracy_pct:.1f}% ({matches}/{total_evaluated})")
    print(f" - Under-Scoped:       {under_rate_pct:.1f}% ({under_scoped}/{total_evaluated}) [Risk: task failure]")
    print(f" - Over-Scoped:        {over_rate_pct:.1f}% ({over_scoped}/{total_evaluated}) [Risk: extra token spend]")
    print(f" - Mean Latency:       {avg_latency_ms:.2f} ms (Target: <100ms)")
    print(f" - Max Latency:        {max_latency_ms:.2f} ms")
    print("=" * 95)

    return 0


def run_budget(
    level: Optional[str] = None,
    format_type: str = "terminal",
) -> int:
    """
    Phase K: View or update the budget slider position without retraining models.

    - Without arguments: displays current budget level, lambda values, and calibration status.
    - With a level argument: updates config.budget_slider and displays the new state.
    """
    cfg = load_config()

    # If a level is provided, update config
    if level:
        cfg.budget_slider = level
        save_config(cfg)

    current_level = cfg.budget_slider

    # Get lambda and source for each level
    from budgetpilot.core.router import CrcLambda, HandSetLambda
    levels = ["tight", "balanced", "loose"]
    level_info = {}
    for lv in levels:
        lam_val, source = CrcLambda.get(lv)
        hand_val = HandSetLambda.get(lv)
        level_info[lv] = {
            "lambda": round(lam_val, 6),
            "source": source,
            "hand_coded_lambda": hand_val,
        }

    # Get calibration summary if available
    try:
        from budgetpilot.core.calibration.crc import get_calibration_summary
        cal_summary = get_calibration_summary()
    except Exception:
        cal_summary = []

    if format_type == "json":
        output = {
            "active_level": current_level,
            "levels": level_info,
            "calibration_history": cal_summary,
        }
        if level:
            output["updated"] = True
        print(json.dumps(output, indent=2, default=str))
        return 0

    # Terminal output
    print("=" * 60)
    print(" BudgetPilot Budget Slider (Phase K)")
    print("=" * 60)
    if level:
        print(f"  Updated budget slider to: {current_level}")
    else:
        print(f"  Active budget level: {current_level}")
    print()

    print(f"  {'Level':<12} {'Lambda':<12} {'Source':<16} {'Hand-Coded'}")
    print("  " + "-" * 54)
    for lv in levels:
        info = level_info[lv]
        marker = " <-- active" if lv == current_level else ""
        print(
            f"  {lv:<12} {info['lambda']:<12.4f} {info['source']:<16} {info['hand_coded_lambda']:.1f}{marker}"
        )

    if cal_summary:
        print()
        print("-" * 20 + " CRC Calibration History " + "-" * 16)
        for entry in cal_summary[:6]:  # Show latest 6 entries max
            bs = entry.get("budget_level", "?")
            lam = entry.get("lambda_calibrated", 0.0)
            alpha = entry.get("alpha_risk", 0.0)
            emp_risk = entry.get("empirical_risk", "—")
            n = entry.get("n_rollouts", "—")
            sat = "✓" if entry.get("bound_satisfied", True) else "✗"
            cal_at = entry.get("calibrated_at", "—")
            print(f"  [{bs:<9}] λ={lam:.4f}  α={alpha:.2f}  R̂={emp_risk}  n={n}  bound={sat}  at={cal_at}")
    else:
        print()
        print("  No CRC calibration data found. Using hand-coded λ values.")
        print("  Run `bp rollout collect` + `bp calibrate` to enable data-driven calibration.")
    print("=" * 60)

    return 0


def run_rollout_collect(
    failures_dir: str,
    seeds: int = 3,
    tier: str = "cheap",
    max_turns: int = 8,
    format_type: str = "terminal",
) -> int:
    """
    Phase J: Execute all 4 recovery actions against each FailureState for `seeds` seeds.
    Loads failure states from `failures_dir/failure_states.json`.
    Records results in SQLite rollouts table and prints a summary.
    """
    from budgetpilot.core.calibration.failures import load_failure_states
    from budgetpilot.core.calibration.rollouts import collect_rollouts

    failures_path = Path(failures_dir)
    if not failures_path.exists():
        print(f"[Error] Failures directory not found: {failures_dir}")
        return 1

    states = load_failure_states(failures_path)
    if not states:
        print(f"[Error] No failure states found in {failures_dir}/failure_states.json")
        return 1

    print(f"Loaded {len(states)} failure state(s) from {failures_dir}")
    print(f"Running {len(states)} × 4 actions × {seeds} seeds = {len(states) * 4 * seeds} rollouts")
    print()

    records = collect_rollouts(
        failure_states=states,
        seeds=seeds,
        tier=tier,
        max_turns=max_turns,
    )

    if format_type == "json":
        print(json.dumps([r.to_dict() for r in records], indent=2))
        return 0

    # Terminal summary
    total = len(records)
    passed = sum(1 for r in records if r.succeeded)
    failed = total - passed
    total_tokens = sum(r.tokens_spent for r in records)
    total_cost = sum(r.cost_usd for r in records)
    total_time = sum(r.wall_time_sec for r in records)

    print("=" * 72)
    print(" BudgetPilot Rollout Collection Summary (Phase J)")
    print("=" * 72)
    print(f"  Failure States: {len(states)}")
    print(f"  Seeds:          {seeds}")
    print(f"  Total Rollouts: {total}  (Pass: {passed} | Fail: {failed})")
    print(f"  Total Tokens:   {total_tokens:,}")
    print(f"  Total Cost:     ${total_cost:.6f} USD")
    print(f"  Total Time:     {total_time:.1f}s")
    print()

    # Per-action summary
    print("-" * 30 + " Per-Action Results " + "-" * 23)
    header = f"  {'Action':<16} {'Trials':<8} {'Pass':<6} {'Fail':<6} {'Pass%':<8} {'Avg Tokens':<12} {'Avg Cost'}"
    print(header)
    print("  " + "-" * 68)
    from budgetpilot.core.types import RecoveryAction
    for action in RecoveryAction:
        action_recs = [r for r in records if r.action == action.value]
        if not action_recs:
            continue
        n = len(action_recs)
        n_pass = sum(1 for r in action_recs if r.succeeded)
        n_fail = n - n_pass
        pct = (n_pass / n * 100) if n > 0 else 0.0
        avg_tok = sum(r.tokens_spent for r in action_recs) / n
        avg_cost = sum(r.cost_usd for r in action_recs) / n
        print(f"  {action.value:<16} {n:<8} {n_pass:<6} {n_fail:<6} {pct:<8.1f} {avg_tok:<12.0f} ${avg_cost:.6f}")

    print()
    # Per-failure-class summary
    print("-" * 26 + " Per Failure Class Results " + "-" * 20)
    classes_seen = sorted({r.failure_class for r in records})
    for fc in classes_seen:
        fc_recs = [r for r in records if r.failure_class == fc]
        n = len(fc_recs)
        n_pass = sum(1 for r in fc_recs if r.succeeded)
        pct = (n_pass / n * 100) if n > 0 else 0.0
        print(f"  {fc:<30} {n} rollouts, {n_pass} passed ({pct:.0f}%)")
    print("=" * 72)

    return 0


def run_bench_run(
    tasks: str,
    arms: str = "off,observe,tight,balanced,loose",
    repeats: int = 3,
    repo: str = ".",
    max_turns: int = 20,
    cost_cap: float = 1.0,
    timeout: int = 60,
    format_type: str = "terminal",
    output_path: Optional[str] = None,
    db_path: Optional[Union[Path, str]] = None,
) -> int:
    """
    Execute multi-arm benchmark suite with worktree isolation and statistical analysis (Phase L).
    """
    from budgetpilot.bench.runner import run_bench_suite

    raw_results, report = run_bench_suite(
        tasks_path=tasks,
        arms=arms,
        repeats=repeats,
        repo_root=repo,
        max_turns=max_turns,
        cost_cap_usd=cost_cap,
        timeout=timeout,
        db_path=db_path,
    )

    fmt = (format_type or "terminal").lower()
    if fmt == "json":
        out_text = report.to_json()
    elif fmt in ("markdown", "md"):
        out_text = report.to_markdown()
    elif fmt == "html":
        out_text = report.to_html()
    else:
        out_text = report.to_terminal()

    if output_path:
        out_file = Path(output_path).expanduser().resolve()
        out_file.parent.mkdir(parents=True, exist_ok=True)
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(out_text)
        print(f"Benchmark report written to: {out_file}")

    print(out_text)
    return 0


def run_status(
    output_json: bool = False,
    db_path: Optional[Union[Path, str]] = None,
) -> int:
    """
    bp status: Show current calibration guarantees and historical savings ratio (Phase L).
    """
    import logging
    from budgetpilot.config import get_bp_home, load_config
    from budgetpilot.core.calibration.crc import get_calibration_summary
    from budgetpilot.core.router import CrcLambda

    logger = logging.getLogger(__name__)
    cfg = load_config()
    bp_home = get_bp_home()
    init_db(db_path)
    conn = get_connection(db_path)

    # 1. Calibration Guarantees
    cal_entries = get_calibration_summary(db_path=db_path)
    has_calibration = len(cal_entries) > 0

    cal_by_level: Dict[str, Any] = {}
    for entry in cal_entries:
        level = entry.get("budget_level")
        if level:
            cal_by_level[level] = entry

    target_alphas = {"tight": 0.30, "balanced": 0.20, "loose": 0.10}
    calibration_status: Dict[str, Any] = {}
    for level in ("tight", "balanced", "loose"):
        lam, source = CrcLambda.get(level, db_path=db_path)
        alpha = target_alphas[level]
        cal_rec = cal_by_level.get(level)
        if cal_rec:
            emp_risk = float(cal_rec.get("empirical_risk", 0.0) if "empirical_risk" in cal_rec else cal_rec.get("alpha_risk", 0.0))
            bound_satisfied = emp_risk <= (alpha + 1e-9)
            cal_at = cal_rec.get("calibrated_at", "")
        else:
            emp_risk = None
            bound_satisfied = None
            cal_at = None

        calibration_status[level] = {
            "budget_level": level,
            "lambda": round(lam, 4),
            "target_alpha": alpha,
            "source": source,
            "calibrated": (source != "uncalibrated"),
            "empirical_risk": round(emp_risk, 4) if emp_risk is not None else None,
            "bound_satisfied": bound_satisfied,
            "calibrated_at": cal_at,
        }

    # 2. Historical Spend and Savings
    total_sessions = 0
    total_measured_cost = 0.0
    total_avoided_low = 0.0
    total_avoided_mid = 0.0
    total_avoided_high = 0.0
    total_paired_savings = 0.0
    gate_counts = {"allow": 0, "rewrite": 0, "deny": 0, "override": 0}
    recovery_outcomes: List[Dict[str, Any]] = []

    if conn:
        try:
            s_row = conn.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(total_cost), 0.0) as cost FROM sessions;").fetchone()
            if s_row:
                total_sessions = int(s_row["cnt"])
                total_measured_cost = float(s_row["cost"])

            # Model calls spend
            mc_row = conn.execute("SELECT COALESCE(SUM(estimated_cost), 0.0) as cost FROM model_calls;").fetchone()
            if mc_row and mc_row["cost"] > 0:
                total_measured_cost = max(total_measured_cost, float(mc_row["cost"]))

            # Avoided estimates
            av_rows = conn.execute("SELECT actual_cost_usd, saved_cost_usd, metadata FROM avoided_estimates;").fetchall()
            for r in av_rows:
                saved = float(r["saved_cost_usd"] or 0.0)
                meta_json = r["metadata"]
                if meta_json:
                    try:
                        m = json.loads(meta_json)
                        total_avoided_low += float(m.get("avoided_low_usd", 0.0))
                        total_avoided_mid += float(m.get("avoided_mid_usd", saved))
                        total_avoided_high += float(m.get("avoided_high_usd", 0.0))
                        gate_counts["override"] += int(m.get("override_count", 0))
                    except Exception:
                        total_avoided_mid += saved
                else:
                    total_avoided_mid += saved

            # Gate decisions
            gd_rows = conn.execute("SELECT verdict, COUNT(*) as cnt FROM gate_decisions GROUP BY verdict;").fetchall()
            for r in gd_rows:
                v = str(r["verdict"]).lower()
                if v in gate_counts:
                    gate_counts[v] = int(r["cnt"])

            # Reports for paired savings
            rep_rows = conn.execute("SELECT savings_usd FROM reports WHERE savings_usd > 0;").fetchall()
            total_paired_savings = sum(float(r["savings_usd"]) for r in rep_rows)

            # Recovery outcomes
            ro_rows = conn.execute(
                """
                SELECT action, COUNT(*) as total_attempts, SUM(CASE WHEN succeeded = 1 THEN 1 ELSE 0 END) as successes,
                       COALESCE(AVG(cost_usd), 0.0) as avg_cost, COALESCE(SUM(cost_usd), 0.0) as total_cost
                FROM recovery_outcomes GROUP BY action ORDER BY total_attempts DESC;
                """
            ).fetchall()
            for ro in ro_rows:
                tot = int(ro["total_attempts"])
                succ = int(ro["successes"] or 0)
                recovery_outcomes.append({
                    "action": ro["action"],
                    "total_attempts": tot,
                    "successes": succ,
                    "success_rate": round(succ / tot * 100.0, 1) if tot > 0 else 0.0,
                    "avg_cost_usd": round(float(ro["avg_cost"] or 0.0), 6),
                    "total_cost_usd": round(float(ro["total_cost"] or 0.0), 6),
                })

        except Exception as exc:
            logger.debug("Error computing status stats: %s", exc)
        finally:
            conn.close()

    baseline_spend = total_measured_cost + total_avoided_mid
    savings_ratio = (total_avoided_mid / baseline_spend * 100.0) if baseline_spend > 0 else 0.0

    status_data = {
        "bp_home": str(bp_home),
        "active_mode": cfg.mode,
        "active_budget_slider": cfg.budget_slider,
        "calibration_active": has_calibration,
        "calibration_guarantees": calibration_status,
        "historical_spend": {
            "total_sessions": total_sessions,
            "measured_spend_usd": round(total_measured_cost, 6),
            "avoided_spend_low_usd": round(total_avoided_low, 6),
            "avoided_spend_mid_usd": round(total_avoided_mid, 6),
            "avoided_spend_high_usd": round(total_avoided_high, 6),
            "paired_run_savings_usd": round(total_paired_savings, 6),
            "baseline_equivalent_spend_usd": round(baseline_spend, 6),
            "savings_ratio_pct": round(savings_ratio, 2),
        },
        "gate_counts": gate_counts,
        "recovery_outcomes": recovery_outcomes,
    }

    if output_json:
        print(json.dumps(status_data, indent=2))
        return 0

    print("=" * 76)
    print(" BudgetPilot System Status & Calibration Guarantees (Phase L)")
    print("=" * 76)
    print(f"  BP Home:           {bp_home}")
    print(f"  Runtime Mode:      {cfg.mode.upper()}")
    print(f"  Active Slider:     {cfg.budget_slider}")
    print(f"  CRC Calibration:   {'ACTIVE (data-driven)' if has_calibration else 'UNAVAILABLE (using hand-set lambda)'}")
    print()

    print("-" * 23 + " Conformal Calibration Guarantees " + "-" * 21)
    hdr = f"  {'Level':<12} {'Risk Bound (α)':<16} {'Lambda (λ)':<12} {'Empirical Risk':<16} {'Status':<12}"
    print(hdr)
    print("  " + "-" * 70)
    for level, g in calibration_status.items():
        is_active = " <-- active" if level == cfg.budget_slider else ""
        emp_str = f"{g['empirical_risk']:.4f}" if g["empirical_risk"] is not None else "N/A"
        sat_str = "GUARANTEED" if g["bound_satisfied"] else ("UNBOUNDED" if g["bound_satisfied"] is False else "uncalibrated")
        print(f"  {level:<12} {g['target_alpha']:<16.2f} {g['lambda']:<12.4f} {emp_str:<16} {sat_str}{is_active}")
    print()

    print("-" * 23 + " Historical Savings & Financials " + "-" * 22)
    hs = status_data["historical_spend"]
    print(f"  Total Sessions:           {hs['total_sessions']}")
    print(f"  Measured Spend (Exact):   ${hs['measured_spend_usd']:.6f} USD")
    print(f"  Avoided Spend (Point):    ${hs['avoided_spend_mid_usd']:.6f} USD  (Range: [${hs['avoided_spend_low_usd']:.6f}, ${hs['avoided_spend_high_usd']:.6f}])")
    if hs["paired_run_savings_usd"] > 0:
        print(f"  Paired-Run Savings:       ${hs['paired_run_savings_usd']:.6f} USD (Benchmark Harness)")
    print(f"  Baseline Equivalent Cost: ${hs['baseline_equivalent_spend_usd']:.6f} USD")
    print(f"  Historical Savings Ratio: {hs['savings_ratio_pct']:.1f}%  (avoided / [measured + avoided])")
    print()

    print("-" * 27 + " Gate Activity " + "-" * 34)
    print(f"  Allow: {gate_counts['allow']} | Rewrite: {gate_counts['rewrite']} | Deny: {gate_counts['deny']} | Overrides Deducted: {gate_counts['override']}")
    if recovery_outcomes:
        print()
        print("-" * 25 + " Recovery Outcomes " + "-" * 32)
        r_hdr = f"  {'Action':<16} {'Attempts':<10} {'Success':<10} {'Success %':<11} {'Avg Cost ($)':<12}"
        print(r_hdr)
        print("  " + "-" * 68)
        for ro in recovery_outcomes:
            print(f"  {ro['action']:<16} {ro['total_attempts']:<10} {ro['successes']:<10} {ro['success_rate']:>6.1f}%     ${ro['avg_cost_usd']:<10.6f}")
    print("=" * 76)
    return 0


def create_parser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(
        prog="bp",
        description="BudgetPilot: A budget-aware decision engine for AI coding agents.",
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"budgetpilot {budgetpilot.__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # run command
    run_parser = subparsers.add_parser("run", help="Run an autonomous agent task on a repository")
    run_parser.add_argument(
        "--task", "-t",
        required=True,
        help="Task description or instructions for the agent",
    )
    run_parser.add_argument(
        "--repo", "-r",
        default=".",
        help="Target repository path (default: '.')",
    )
    run_parser.add_argument(
        "--mode", "-m",
        choices=["enforce", "observe", "off"],
        default="enforce",
        help="Execution mode (default: enforce)",
    )
    run_parser.add_argument(
        "--tier",
        choices=["cheap", "strong"],
        default="cheap",
        help="Model tier to use (default: cheap)",
    )
    run_parser.add_argument(
        "--max-turns",
        type=int,
        default=20,
        help="Maximum interaction turns (default: 20)",
    )
    run_parser.add_argument(
        "--session",
        dest="session_id",
        default=None,
        help="Custom session ID",
    )

    # init command
    init_parser = subparsers.add_parser("init", help="Initialize BudgetPilot configuration directory")
    init_parser.add_argument(
        "--mode",
        choices=["enforce", "observe", "off"],
        default="observe",
        help="Initial execution mode (default: observe)",
    )

    # config command
    config_parser = subparsers.add_parser("config", help="View or inspect configuration")
    config_parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON configuration",
    )

    # doctor command
    subparsers.add_parser("doctor", help="Run system diagnostics and verify dependencies")

    # chat command
    chat_parser = subparsers.add_parser("chat", help="Start an interactive chat session")
    chat_parser.add_argument(
        "--tier",
        choices=["cheap", "strong"],
        default="cheap",
        help="Model tier to use (default: cheap)",
    )
    chat_parser.add_argument(
        "--session",
        dest="session_id",
        default=None,
        help="Custom session ID",
    )

    # serve command
    serve_parser = subparsers.add_parser("serve", help="Run the BudgetPilot MCP server")
    serve_parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport layer protocol (default: stdio)",
    )

    # bench command (Phase C)
    bench_parser = subparsers.add_parser("bench", help="BudgetPilot benchmark commands (Phase C)")
    bench_subparsers = bench_parser.add_subparsers(dest="bench_command", help="Benchmark subcommands")

    baseline_parser = bench_subparsers.add_parser("baseline", help="Run ungated baseline benchmark (Phase C)")
    baseline_parser.add_argument(
        "--repo", "-r",
        default=".",
        help="Target repository path (default: '.')",
    )
    baseline_parser.add_argument(
        "--tasks", "-t",
        required=True,
        help="Path to tasks.yaml benchmark specification",
    )
    baseline_parser.add_argument(
        "--repeats", "-n",
        type=int,
        default=1,
        help="Number of repetitions per task (default: 1)",
    )
    baseline_parser.add_argument(
        "--tier",
        choices=["cheap", "strong"],
        default="cheap",
        help="Model tier to use (default: cheap)",
    )
    baseline_parser.add_argument(
        "--max-turns",
        type=int,
        default=20,
        help="Maximum interaction turns per task (default: 20)",
    )
    baseline_parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Timeout in seconds for verification command (default: 60)",
    )

    # bench run command (Phase L)
    run_bench_parser = bench_subparsers.add_parser(
        "run",
        help="Run multi-arm benchmark suite with worktree isolation and statistical analysis (Phase L)",
    )
    run_bench_parser.add_argument(
        "--tasks", "-t",
        required=True,
        help="Path to tasks.yaml benchmark specification",
    )
    run_bench_parser.add_argument(
        "--arms", "-a",
        default="off,observe,tight,balanced,loose",
        help="Comma-separated arms to evaluate (default: 'off,observe,tight,balanced,loose')",
    )
    run_bench_parser.add_argument(
        "--repeats", "-n",
        type=int,
        default=3,
        help="Number of repetitions per task per arm (default: 3)",
    )
    run_bench_parser.add_argument(
        "--repo", "-r",
        default=".",
        help="Target repository path (default: '.')",
    )
    run_bench_parser.add_argument(
        "--max-turns",
        type=int,
        default=20,
        help="Maximum interaction turns per task run (default: 20)",
    )
    run_bench_parser.add_argument(
        "--cost-cap",
        type=float,
        default=1.0,
        help="Maximum cost cap in USD per task run (default: 1.0)",
    )
    run_bench_parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Timeout in seconds for verification command (default: 60)",
    )
    run_bench_parser.add_argument(
        "--format", "-f",
        choices=["terminal", "markdown", "md", "json", "html"],
        default="terminal",
        help="Output format (default: terminal)",
    )
    run_bench_parser.add_argument(
        "--output", "-o",
        default=None,
        help="File path to save the generated report",
    )
    run_bench_parser.add_argument(
        "--db",
        default=None,
        help="SQLite database path",
    )

    # report command (Phase C & L)
    report_parser = subparsers.add_parser("report", help="Generate cost, token, and tool breakdown reports")
    report_parser.add_argument(
        "--session", "-s",
        dest="session_id",
        default=None,
        help="Session ID to report on (defaults to all sessions)",
    )
    report_parser.add_argument(
        "--format", "-f",
        choices=["terminal", "markdown", "md", "json", "html"],
        default="terminal",
        help="Report output format (default: terminal)",
    )

    # estimator command (Phase D)
    estimator_parser = subparsers.add_parser("estimator", help="Scope estimator utilities (Phase D)")
    estimator_subparsers = estimator_parser.add_subparsers(dest="estimator_command", help="Estimator subcommands")

    eval_parser = estimator_subparsers.add_parser("eval", help="Evaluate scope predictions against target repo commit history")
    eval_parser.add_argument(
        "--repo", "-r",
        default=".",
        help="Target repository path (default: '.')",
    )
    eval_parser.add_argument(
        "--limit", "-n",
        type=int,
        default=20,
        help="Number of recent commits to evaluate (default: 20)",
    )
    eval_parser.add_argument(
        "--threshold",
        type=float,
        default=0.35,
        help="Low confidence round-up threshold (default: 0.35)",
    )
    eval_parser.add_argument(
        "--format", "-f",
        choices=["terminal", "json"],
        default="terminal",
        help="Output format (default: terminal)",
    )

    # calibrate command (future phase)
    subparsers.add_parser("calibrate", help="Run CRC budget calibration (Phase D)")

    # rollout command (Phase J)
    rollout_parser = subparsers.add_parser("rollout", help="Rollout data collection for prior calibration (Phase J)")
    rollout_subparsers = rollout_parser.add_subparsers(dest="rollout_command", help="Rollout subcommands")

    collect_parser = rollout_subparsers.add_parser(
        "collect",
        help="Execute all 4 recovery actions against generated failure states and record results",
    )
    collect_parser.add_argument(
        "--failures",
        required=True,
        help="Directory containing failure_states.json (generated by failure state generators)",
    )
    collect_parser.add_argument(
        "--seeds", "-n",
        type=int,
        default=3,
        help="Number of random seeds per (failure_state, action) pair (default: 3)",
    )
    collect_parser.add_argument(
        "--repo", "-r",
        default=".",
        help="Target repository path (default: '.')",
    )
    collect_parser.add_argument(
        "--tier",
        choices=["cheap", "strong"],
        default="cheap",
        help="Model tier for recovery actions (default: cheap)",
    )
    collect_parser.add_argument(
        "--max-turns",
        type=int,
        default=8,
        help="Maximum turns per recovery action attempt (default: 8)",
    )
    collect_parser.add_argument(
        "--format", "-f",
        choices=["terminal", "json"],
        default="terminal",
        help="Output format (default: terminal)",
    )

    # budget command (Phase K)
    budget_parser = subparsers.add_parser(
        "budget",
        help="View or update budget slider position without retraining (Phase K)",
    )
    budget_parser.add_argument(
        "level",
        nargs="?",
        choices=["tight", "balanced", "loose"],
        default=None,
        help="Budget level to set (omit to view current status)",
    )
    budget_parser.add_argument(
        "--format", "-f",
        choices=["terminal", "json"],
        default="terminal",
        help="Output format (default: terminal)",
    )

    # status command (Phase L)
    status_parser = subparsers.add_parser(
        "status",
        help="Show current calibration guarantees and historical savings ratio (Phase L)",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Output status in JSON format",
    )
    status_parser.add_argument(
        "--db",
        default=None,
        help="SQLite database path",
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "run":
        return run_cli_task(
            task=args.task,
            repo=args.repo,
            mode=args.mode,
            tier=args.tier,
            max_turns=args.max_turns,
            session_id=args.session_id,
        )

    elif args.command == "doctor":
        return run_doctor()

    elif args.command == "chat":
        return run_chat_loop(tier=args.tier, session_id=args.session_id)

    elif args.command == "init":
        from budgetpilot.config import ExecutionMode
        bp_home = get_bp_home()
        cfg = load_config()
        cfg.mode = ExecutionMode(args.mode)
        save_config(cfg)
        print(f"Initialized BudgetPilot in {bp_home}")
        print(f"Active mode: {cfg.mode.value}")
        return 0

    elif args.command == "config":
        bp_home = get_bp_home()
        cfg = load_config()
        if args.json:
            print(cfg.model_dump_json(indent=2))
        else:
            print(f"BP_HOME: {bp_home}")
            print(f"Mode: {cfg.mode.value}")
            print(f"Budget Slider: {cfg.budget_slider}")
            print(f"Cheap Tier: {cfg.tiers.cheap.model} ({cfg.tiers.cheap.provider})")
            print(f"Strong Tier: {cfg.tiers.strong.model} ({cfg.tiers.strong.provider})")
            print(f"Default Test Command: {cfg.test_commands.default}")
        return 0

    elif args.command == "serve":
        from budgetpilot.mcp.server import run_server
        run_server(transport=args.transport)
        return 0

    elif args.command == "bench":
        if args.bench_command == "baseline":
            from budgetpilot.bench.baseline import run_baseline_benchmark
            summary = run_baseline_benchmark(
                repo_path=args.repo,
                tasks_file=args.tasks,
                repeats=args.repeats,
                tier=args.tier,
                max_turns=args.max_turns,
                timeout=args.timeout,
            )
            return 0 if summary.failed_runs == 0 else 1
        elif args.bench_command == "run":
            return run_bench_run(
                tasks=args.tasks,
                arms=args.arms,
                repeats=args.repeats,
                repo=args.repo,
                max_turns=args.max_turns,
                cost_cap=args.cost_cap,
                timeout=args.timeout,
                format_type=args.format,
                output_path=args.output,
                db_path=args.db if hasattr(args, "db") else None,
            )
        else:
            parser.parse_args(["bench", "--help"])
            return 0

    elif args.command == "report":
        return run_report(
            session_id=args.session_id,
            format_type=args.format,
        )

    elif args.command == "estimator":
        if args.estimator_command == "eval":
            return run_estimator_eval(
                repo_path=args.repo,
                limit=args.limit,
                threshold=args.threshold,
                format_type=args.format,
            )
        else:
            parser.parse_args(["estimator", "--help"])
            return 0

    elif args.command == "rollout":
        if args.rollout_command == "collect":
            return run_rollout_collect(
                failures_dir=args.failures,
                seeds=args.seeds,
                tier=args.tier,
                max_turns=args.max_turns,
                format_type=args.format,
            )
        else:
            parser.parse_args(["rollout", "--help"])
            return 0

    elif args.command == "budget":
        return run_budget(
            level=args.level,
            format_type=args.format,
        )

    elif args.command == "status":
        return run_status(
            output_json=args.json,
            db_path=args.db if hasattr(args, "db") else None,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())

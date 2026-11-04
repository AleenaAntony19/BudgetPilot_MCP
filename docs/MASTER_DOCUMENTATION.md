# BudgetPilot: Master Architecture & Project Documentation

> **Welcome to BudgetPilot!** This document serves as the comprehensive architectural reference and technical onboarding guide for software engineers, systems architects, and contributors joining the project.

---

## 1. Executive Summary & Core Mission

### What is BudgetPilot?

[BudgetPilot](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/README.md) is a local, budget-aware decision engine and proxy framework designed for autonomous AI coding agents (such as Google Antigravity, Claude Code, and Cursor). It sits directly between an agent's reasoning core and its underlying operating system tools (`read_file`, `edit_file`, `list_dir`, `search`, and `run_tests`).

Rather than modifying model prompts or altering the agent's user interface, BudgetPilot operates at the **tool-interception layer** via the [Model Context Protocol (MCP 2.x)](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/adapters/mcp_server.py) and [ultra-low latency standard-library hooks](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/adapters/hooks.py) (<60ms SLA).

```
   ┌───────────────────────────────────────────────────────────┐
   │             User Prompt / IDE (Claude Code, Antigravity)  │
   └─────────────────────────────┬─────────────────────────────┘
                                 │
                                 ▼
   ┌───────────────────────────────────────────────────────────┐
   │                    Agent Host Core LLM                    │
   └─────────────────────────────┬─────────────────────────────┘
                                 │ Tool Calls (read, edit, test)
                                 ▼
   ┌───────────────────────────────────────────────────────────┐
   │                   BUDGETPILOT PROXY                       │
   │  ┌─────────────────┐ ┌───────────────┐ ┌────────────────┐ │
   │  │ Scope Estimator │ │ Execute Gate  │ │ Recovery Router│ │
   │  └─────────────────┘ └───────────────┘ └────────────────┘ │
   └─────────────────────────────┬─────────────────────────────┘
                                 │ Controlled Execution
                                 ▼
   ┌───────────────────────────────────────────────────────────┐
   │              Local Workspace / Git Repository             │
   └───────────────────────────────────────────────────────────┘
```

### The Problem It Solves

Modern coding agents are notoriously inefficient spenders:
1. **Context Over-Reading**: Agents routinely perform *maximum-context-first exploration*—reading hundreds of files or scanning entire repositories for single-line bug fixes. Research (e.g., Yin & Feng, *E3/MSE-Bench*, arXiv:2607.13034) shows this inflates token spend by **5x to 13x** on simple tasks.
2. **Premature Over-Escalation**: On encountering any test failure or syntax error, the default agent instinct is to immediately escalate to an expensive frontier model (e.g., Claude 3.7 Sonnet or GPT-4o), even when a cheap, local repair would have succeeded (He et al., *CodeRescue*, arXiv:2607.19338).
3. **Agent Loops & Hallucination Spirals**: Denied tool calls or unexpected test output often trigger cyclic tool ping-pong, depleting context windows and racking up runaway API bills.

### The BudgetPilot Solution

BudgetPilot introduces a four-stage closed-loop pipeline:
- **Pre-Execution Scope Estimation**: Predicts task complexity (Scope 1, 2, or 3) in under 50ms before the agent over-reads.
- **Minimum-Viable Execution Gating**: Enforces file-count caps and clamps over-broad read windows (e.g., 400 lines for Scope 1).
- **Automated Test Diagnostics**: Catches verification failures, filters environmental noise, and categorizes errors into 7 deterministic failure classes.
- **Cost-Calibrated Recovery Routing**: Replaces brute-force escalation with utility-maximizing recovery actions (**reflect**, **replan**, **expand-scope**, and **escalate**). A live conformal budget dial (**tight**, **balanced**, **loose**) allows developers to tune the cost-versus-accuracy tradeoff with zero model retraining.

---

## 2. Architectural Philosophy & Design Principles

BudgetPilot was engineered around three non-negotiable architectural invariants:

### 1. Zero-Dependency Isolation in Core

A critical design requirement of BudgetPilot is that core algorithmic and data structures must remain 100% decoupled from third-party networking and heavyweight frameworks.

- **The Rule**: Modules in [`budgetpilot/core/`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core) and [`budgetpilot/bench/`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/bench) must **never** import `ollama`, `mcp`, `fastapi`, or `uvicorn`.
- **Why**: Coding tools often run inside constrained developer environments, CI pipelines, and ephemeral git worktrees. Decoupling the decision logic from server runtimes ensures lightning-fast startup (<15ms), zero network overhead during decision paths, and trivial testability.
- **Enforcement**: This invariant is enforced automatically in CI via Abstract Syntax Tree (AST) scanning in [`tests/test_types.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/tests/test_types.py#L225-L250) and [`tests/test_bench_analysis.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/tests/test_bench_analysis.py#L380-L400). If any developer imports `ollama` or `mcp` into `core`, the test suite fails immediately.

### 2. Fail-Open Safety & Non-Disruptive Resilience

BudgetPilot is a supervisor, not an obstruction. Under no circumstances should an internal exception or database lock inside BudgetPilot crash the user's primary agent.

- **Fail-Open Gating**: If an unhandled exception occurs inside the Execute Gate ([`budgetpilot/core/gate.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/gate.py#L388-L396)), the gate automatically catches the error, logs the diagnostic context, and returns `GateVerdict.allow()` with `metadata={"fail_open": True}`. The developer's agent session continues uninterrupted.
- **SQLite Fallback Logging**: If the local SQLite database (`~/.budgetpilot/bp.db`) is locked by concurrent processes or suffers a filesystem error, [`safe_execute_write`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/store.py#L240-L260) falls open to append-only JSON Lines logging in [`~/.budgetpilot/fallback.jsonl`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/store.py#L263-L285).
- **Global Kill Switch (`BP_MODE=off`)**: If a developer ever encounters an unexpected gate block during critical work, setting `export BP_MODE=off` immediately causes BudgetPilot to bypass all checks and pass all tool calls directly through.

### 3. The "Reference USD" Economic Model

Local open-weight models (running via Ollama) consume compute and electricity, but they do not charge per-token credit-card bills. To model, optimize, and benchmark savings that translate directly to commercial coding agents, BudgetPilot introduces **Reference USD Pricing**:

- **Model Tiers**:
  - `cheap` tier (e.g., `qwen2.5:3b`): Reference rate of **$1.00 / M** input tokens, **$2.00 / M** output tokens.
  - `strong` tier (e.g., `qwen2.5:14b` or frontier APIs): Reference rate of **$3.00 / M** input tokens, **$6.00 / M** output tokens.
- **Prompt Cache Re-Use Protection**: To prevent distorted cost estimates from Ollama's local prompt cache hits (where `prompt_eval_tokens` reports `0`), [`CostLedger`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/ledger.py#L65-L85) prices billable input tokens using:
  $$\text{billable\_tokens} = \max\left(\text{prompt\_eval\_tokens}, \left\lfloor \frac{\text{len}(\text{prompt})}{4} \right\rfloor\right)$$
- **Three Metric Separation**:
  BudgetPilot strictly refuses to blend real and simulated numbers into a single metric. Financial reports ([`bp report`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/cli.py#L820-L1050)) always separate:
  1. `[1] Measured Spend (Exact)`: Exact tokens consumed by model calls multiplied by tier rates.
  2. `[2] Modeled Avoided Spend`: Counterfactual savings modeled from gate-denied calls, bounded by p25–p75 uncertainty envelopes (`low_usd <= avoided_usd <= high_usd`), accounting for subsequent late-read and override deductions.
  3. `[3] Paired-Run Savings`: Real delta measured against an ungated baseline running the identical task in a paired benchmark worktree.

---

## 3. Complete End-to-End System Workflow

The following diagram illustrates the complete execution lifecycle of a coding task running under BudgetPilot:

```
                      TASK INITIALIZATION
                               │
                               ▼
               ┌───────────────────────────────┐
               │    1. Scope Estimator         │
               │  - Fast Lexical Feature Match │
               │  - Ripgrep Probe (<50ms)      │
               │  - Output: Scope 1, 2, or 3   │
               └───────────────┬───────────────┘
                               │
                               ▼
               ┌───────────────────────────────┐
               │      2. Agent Tool Loop       │
               │  - Model proposes tool call   │
               │    (read_file, edit, test)    │
               └───────────────┬───────────────┘
                               │ Intercepted Event (BpEvent)
                               ▼
               ┌───────────────────────────────┐
               │       3. Execute Gate         │
               │  - Check BP_MODE (off/obs/enf)│
               │  - Check 3-Turn Override Key  │
               │  - Scope 1: Max 3 reads, 400L │
               │  - Scope 2: Max 10 reads, 800L│
               │  - Scope 3: Unrestricted      │
               └───────┬───────────────┬───────┘
                       │               │
        ALLOW / REWRITE│               │ DENIED
                       ▼               ▼
        ┌──────────────────────┐  ┌───────────────────────────────┐
        │ Execute Tool on Host │  │ Return Error Guidance to LLM  │
        │ Track Touched/Read   │  │ Increment Deny Counter        │
        └──────────────┬───────┘  └───────────────────────────────┘
                       │
                       ▼ (After code edit)
        ┌───────────────────────────────┐
        │     4. Test Verifier          │
        │  - Run pytest in subprocess   │
        │  - Parse exit code & stderr   │
        └───────┬───────────────┬───────┘
                │               │
         PASSED │               │ FAILED (7 Failure Classes)
                ▼               ▼
        ┌───────────────┐  ┌───────────────────────────────┐
        │  Task Done!   │  │     5. Recovery Router        │
        │ Commit State  │  │  Utility: U(a) = P - λ*C      │
        │ Record Savings│  │  Laplace Empirical Posteriors │
        └───────────────┘  └───────┬───────────────────────┘
                                   │
               ┌───────────────────┼───────────────────┐
               ▼                   ▼                   ▼
        ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
        │   Reflect   │     │   Replan    │     │Expand-Scope │
        │ Local patch │     │ Git rollback│     │Frontier +1  │
        │ on touched  │     │ 3-line plan │     │read budget  │
        └─────────────┘     └─────────────┘     └─────────────┘
                                   │ (If repeated failure)
                                   ▼
                            ┌─────────────┐
                            │  Escalate   │
                            │Strong tier  │
                            │Git worktree │
                            └─────────────┘
```

### Walkthrough of the 6 Core Stages

1. **Scope Estimation ([`estimator.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/estimator.py))**:
   Before reading files, BudgetPilot inspects the user prompt. It extracts symbols and runs a bounded `ripgrep` search to count matching files and definition sites.
   - *Scope 1 (Small)*: Single-file edit, ≤3 candidate files. Budget: 4,000 tokens.
   - *Scope 2 (Medium)*: Multi-file local subsystem, 4–10 candidate files. Budget: 8,000 tokens.
   - *Scope 3 (Large)*: Cross-cutting architectural refactor, >10 candidate files. Budget: 20,000 tokens.
2. **Execute Gate ([`gate.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/gate.py))**:
   Intercepts tool invocations. Read calls exceeding the scope's line limit are automatically rewritten to clamped windows (e.g., lines 1–400) instead of being rejected. Repeating a denied call within 3 turns triggers an automatic safety override, allowing the agent to proceed if it genuinely requires the file.
3. **Storage & Financial Ledger ([`store.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/store.py), [`ledger.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/ledger.py))**:
   Persists all model tokens, tool calls, gate verdicts, and git commit hashes into SQLite under WAL mode. Automated secret redaction strips credentials before write execution.
4. **Test Verifier ([`verifier.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/verifier.py))**:
   After edits are made, the agent runs tests. The verifier captures stdout/stderr, filters out transient warnings or environmental noise, computes a SHA-256 signature hash of the failure trace, and categorizes the failure into one of 7 canonical classes (e.g., `SYNTAX_OR_LINT`, `ASSERTION_LOCAL`, `SYMBOL_OUTSIDE_TOUCHED`, `REGRESSION_ELSEWHERE`, `REPEAT_SIGNATURE`, `CAPABILITY_GAP`, `ENV_OR_FLAKY`).
5. **Recovery Router ([`router.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/router.py))**:
   Evaluates candidate recovery actions using utility maximization:
   $$U(a) = P_{\text{success}}(a \mid \text{failure\_class}) - \lambda \cdot C_{\text{norm}}(a)$$
   Where $C_{\text{norm}}$ is the normalized action cost (Reflect = 0.10, Replan = 0.25, Expand-Scope = 0.45, Escalate = 0.90) and $\lambda$ is governed by the budget dial.
6. **Recovery Executors ([`executors.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/executors.py))**:
   - **Reflect**: Injects an explicit local repair directive into the agent's context, directing it to fix the issue within already touched files.
   - **Replan**: Automatically reverts touched files to their initial git snapshot and injects an exact 3-line replanning directive.
   - **Expand-Scope** *(Novel Contribution)*: Parses the traceback to extract files outside the touched set, queries `ripgrep` for missing symbol definitions, bumps the task scope by $+1$, and increases the file read budget.
   - **Escalate ([`escalation.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/escalation.py))**: Spins up an isolated git worktree and delegates the task to the `strong` model tier with hard turn and cost caps ($1.00 USD).

---

## 4. Phase-by-Phase Construction Breakdown (Phases A through P)

BudgetPilot was developed systematically across 16 sequential phases. Here is how the system was built:

```
  FOUNDATIONS              CORE ENGINES            RECOVERY & EXPANSION      CALIBRATION & STATS       HOSTS & HARDENING
┌──────────────┐         ┌──────────────┐         ┌─────────────────────┐   ┌─────────────────────┐   ┌─────────────────────┐
│ Phases A–B   │ ──────> │ Phases D–F   │ ──────> │ Phases G–H          │ ─>│ Phases I–L          │ ─>│ Phases M–P          │
│ Store, Model │         │ Estimator,   │         │ Router, Frontier,   │   │ Savings, Rollouts,  │   │ MCP 2.x, Hooks,     │
│ Tools, Loop  │         │ Gate, Verify │         │ Worktree Escalation │   │ CRC Dial, Multi-Arm │   │ Safety, Latency SLA │
└──────────────┘         └──────────────┘         └─────────────────────┘   └─────────────────────┘   └─────────────────────┘
```

### Milestone 1: Foundations & Storage (Phases A – B)
- **Phase A (CLI, Config & Store)**: Initialized [`pyproject.toml`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/pyproject.toml), created [`budgetpilot/config.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/config.py) (`BP_HOME`, `config.json`), implemented [`budgetpilot/core/store.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/store.py) (SQLite WAL pragmas, forward-only migrations, fallback JSON Lines error logging), and created [`bp doctor`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/cli.py#L35-L215).
- **Phase B (Agent Runtime & Tools)**: Implemented safe Ollama wrapper [`budgetpilot/agent/model.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/model.py), sandboxed path-confined tool dispatcher [`budgetpilot/agent/tools.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/tools.py) (`list_dir`, `read_file`, `edit_file`, `search`, `run_tests`), task state tracker [`budgetpilot/core/tracker.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/tracker.py), and the multi-turn agent loop [`budgetpilot/agent/loop.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/loop.py).

### Milestone 2: Benchmarking & Ground Truth (Phase C)
- **Phase C (Baseline Harness & Reporting)**: Built [`budgetpilot/bench/baseline.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/bench/baseline.py), running unconstrained tasks in isolated git worktrees. Built session reporting in [`budgetpilot/cli.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/cli.py) (`bp report`), establishing the ground truth for ungated costs and solve rates.

### Milestone 3: Scope Estimation & Gating (Phases D – F)
- **Phase D (Scope Estimator)**: Built [`budgetpilot/core/estimator.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/estimator.py) with lexical complexity rules and sub-50ms ripgrep probe, predicting Scope 1, 2, or 3 with low-confidence round-up safeguards.
- **Phase E (Execute Gate)**: Built [`budgetpilot/core/gate.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/gate.py) with the Scope Policy Enforcement Matrix, read-window clamping, 3-turn override tracking, and fail-open resilience.
- **Phase F (Test Verifier & Diagnostics)**: Built [`budgetpilot/core/verifier.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/verifier.py), capturing subprocess pytest outputs, stripping environmental noise, and classifying failures into 7 canonical failure classes.

### Milestone 4: Recovery Routing & Escalation (Phases G – H)
- **Phase G (Recovery Router & Frontier)**: Built [`budgetpilot/core/router.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/router.py) (utility maximization $U(a) = P - \lambda C$), [`budgetpilot/core/frontier.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/frontier.py) (traceback frame analysis and symbol discovery), and [`budgetpilot/core/executors.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/executors.py) (`execute_reflect`, `execute_replan`, `execute_expand_scope`).
- **Phase H (Worktree Escalation)**: Built [`budgetpilot/agent/escalation.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/escalation.py), enabling bounded strong-tier sub-runs inside isolated git worktrees with strict turn/USD caps and guaranteed worktree cleanup.

### Milestone 5: Conformal Calibration & Statistical Rigor (Phases I – L)
- **Phase I (Avoided Cost Engine)**: Built [`budgetpilot/core/savings.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/savings.py), computing counterfactual savings from denied calls with p25–p75 uncertainty bands (`low_usd <= avoided_usd <= high_usd`), override deductions, and late-read deductions.
- **Phase J (Rollout Collector & Empirical Priors)**: Built [`budgetpilot/core/calibration/failures.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/calibration/failures.py) (synthetic failure state generators) and [`budgetpilot/core/calibration/rollouts.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/calibration/rollouts.py) (`bp rollout collect`), using Laplace smoothing to blend empirical rollout posteriors into the router.
- **Phase K (Conformal Risk Control & Budget Slider)**: Built [`budgetpilot/core/calibration/crc.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/calibration/crc.py), implementing distribution-free Conformal Risk Control with monotone loss envelopes and empirical risk bounds ($\alpha=0.30$ tight, $\alpha=0.20$ balanced, $\alpha=0.10$ loose). Wired runtime slider in `bp budget`.
- **Phase L (Multi-Arm Benchmarking & Statistical Analysis)**: Built [`budgetpilot/bench/runner.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/bench/runner.py) and [`budgetpilot/bench/analysis.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/bench/analysis.py), computing non-parametric bootstrap confidence intervals (1,000 resamples), paired cost differences conditioned on equal solve status, and HTML dashboard reports. Added `bp status`.

### Milestone 6: Host Adapters, Safety Hardening & Release (Phases M – P)
- **Phase M (MCP 2.x Server Adapter)**: Built [`budgetpilot/adapters/mcp_server.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/adapters/mcp_server.py), exposing control tools (`bp_status`, `bp_set_budget`, `bp_request_scope`, `bp_escalate`, `bp_report`) and proxy tools (`bp_list_dir`, `bp_search`, `bp_read_file`, `bp_edit_file`, `bp_run_tests`).
- **Phase N (Ultra-Low Latency Hooks)**: Built [`budgetpilot/adapters/hooks.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/adapters/hooks.py), a pure standard-library hook shim (<60ms SLA) for Claude Code PreToolUse/PostToolUse events.
- **Phase O (Safety & Hardening)**: Built [`budgetpilot/core/safety.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/safety.py), introducing the global kill switch (`BP_MODE=off`), automated secret redaction (AWS keys, tokens, Bearer headers, private keys, JWTs), hard per-task cap guards ([`TaskCapGuard`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/safety.py#L207-L300)), and network egress confinement (local Ollama host only).
- **Phase P (Polish, Latency SLA & Release)**: Integrated Check 7 into `bp doctor` (50 gate iterations, p95 < 60ms SLA audit), created comprehensive documentation ([`docs/COMMANDS.md`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/docs/COMMANDS.md), [`docs/INTEGRATION.md`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/docs/INTEGRATION.md), [`docs/STATUS.md`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/docs/STATUS.md), [`README.md`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/README.md)), and confirmed 100% green pass status across 289 tests.

---

## 5. Key CLI Commands & How to Use Them

The unified CLI entrypoint is `bp`. Here are the primary operational commands:

### 1. `bp doctor` (System Diagnostics & Latency SLA)
Runs a 7-point health check across Ollama, git, ripgrep, SQLite, reference prices, and measures gate interception latency.
```bash
bp doctor
```
*Expected Result*: All 7 checks pass, reporting gate latency p95 < 60ms (typically ~0.03ms).

### 2. `bp run` (Autonomous Task Execution)
Executes a programming task against a local repository under active BudgetPilot scope gating and recovery routing.
```bash
# Fix an issue under active enforcement
bp run --task "Fix zero division in calculate_tax" --repo . --mode enforce

# Run in passive observe mode (measures and logs without blocking calls)
bp run --task "Add docstrings to api handlers" --mode observe

# Run with tight budget slider (aggressive token savings)
bp run --task "Fix typos in README" --budget tight
```

### 3. `bp budget` (Live Conformal Budget Dial)
Inspects or dynamically updates the runtime budget slider operating point with zero service restart and zero model retraining.
```bash
# View active slider and calibrated lambda penalties
bp budget

# Set to tight (aggressive savings, higher replan rate, λ = 2.50)
bp budget tight

# Set to balanced (optimal trade-off, λ = 0.50, default)
bp budget balanced

# Set to loose (maximum solve rate, lowest risk, λ = 0.10)
bp budget loose
```

### 4. `bp status` (System State & Calibration Guarantees)
Displays current runtime mode, active slider, Conformal Risk Control empirical guarantees, total historical spend, and gate counts.
```bash
bp status
```

### 5. `bp report` (Cost, Usage & Savings Accounting)
Generates comprehensive reports with strict 3-metric cost separation.
```bash
# Terminal report across all sessions
bp report

# View the most recent session
bp report --latest

# Export responsive interactive HTML dashboard
bp report --format html --output report.html

# Export Markdown summary for pull requests
bp report --format markdown --output summary.md
```

### 6. `bp bench` (Benchmark Suite Execution)
Runs controlled evaluations across repository tasks.
```bash
# Run ungated baseline in temporary worktrees
bp bench baseline --suite bench/tasks.example.yaml --repeats 3

# Run multi-arm evaluation across all 5 arms with cost caps
bp bench run --suite bench/tasks.example.yaml --arms off,tight,balanced,loose --repeats 3 --cost-cap-usd 0.50

# Analyze paired cost savings and bootstrap confidence intervals
bp bench analyze --baseline-arm off
```

---

## 6. Developer Getting Started & Quickstart Guide

Follow these steps to set up your local development environment, verify repository health, and run your first task.

### Step 1: Environment Setup

Clone the repository and activate a Python 3.11+ virtual environment (Miniconda recommended):

```bash
git clone https://github.com/AleenaAntony19/BudgetPilot_MCP.git
cd BudgetPilot_MCP

# Create and activate environment
conda create -n budgetpilot python=3.11 -y
conda activate budgetpilot

# Install BudgetPilot in editable mode with development dependencies
pip install -e ".[dev]"
```

### Step 2: Ensure Prerequisites are Available

Confirm that `git`, `ripgrep`, and `ollama` are installed:
```bash
# Check git
git --version

# Check ripgrep
rg --version

# Start Ollama daemon and pull a tool-calling model
ollama serve &
ollama pull qwen2.5:3b
```

### Step 3: Run System Diagnostics

Verify that BudgetPilot recognizes your local environment:
```bash
bp doctor
```
If any check shows `[FAIL]`, review the diagnostic message (e.g., ensure `ollama` is running or install `ripgrep`).

### Step 4: Run the Complete Test Suite

Verify that all unit, integration, Monte Carlo risk, and security audit tests pass:
```bash
pytest -v
```
You should see:
```text
======================= 288 passed, 1 skipped in 12.35s ========================
```

### Step 5: Test Autonomous Task Execution

Test the agent loop on a simple, isolated task:
```bash
bp run --task "Inspect README.md and verify project status" --repo . --mode observe
```

### Step 6: Connect to Your Preferred Agent Host

- **Google Antigravity**: Add BudgetPilot to `~/.gemini/config/mcp_config.json` as documented in [`docs/INTEGRATION.md`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/docs/INTEGRATION.md).
- **Claude Code**: Add BudgetPilot stdlib hooks to `~/.claude/settings.json` as documented in [`docs/COMMANDS.md`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/docs/COMMANDS.md).

---

## 7. Key Files & Architecture Cheat Sheet

| File / Module | Responsibility |
| :--- | :--- |
| [`budgetpilot/cli.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/cli.py) | Main CLI entrypoint (`bp doctor`, `bp run`, `bp budget`, `bp status`, `bp report`). |
| [`budgetpilot/core/types.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/types.py) | Core Enums (`RecoveryAction`, `FailureClass`), Dataclasses (`GateVerdict`, `RecoveryContext`). |
| [`budgetpilot/core/store.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/store.py) | SQLite WAL storage, 14 domain tables, migrations, fail-open `fallback.jsonl`. |
| [`budgetpilot/core/estimator.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/estimator.py) | Fast lexical complexity rules + sub-50ms ripgrep probe predicting Scope 1/2/3. |
| [`budgetpilot/core/gate.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/gate.py) | Policy matrix, read window clamping, 3-turn overrides, `BP_MODE=off` kill switch. |
| [`budgetpilot/core/verifier.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/verifier.py) | Pytest output capture, environmental noise filter, 7-class failure taxonomy. |
| [`budgetpilot/core/router.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/router.py) | Utility maximization $U(a) = P - \lambda C$ with Laplace smoothing priors. |
| [`budgetpilot/core/frontier.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/frontier.py) | Traceback frame parsing, symbol reference discovery, `expand-scope` action. |
| [`budgetpilot/core/executors.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/executors.py) | Directives for `reflect`, `replan` (git rollback), and `expand_scope`. |
| [`budgetpilot/core/savings.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/savings.py) | Avoided cost modeling with p25–p75 uncertainty bands, override/late-read deductions. |
| [`budgetpilot/core/calibration/crc.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/calibration/crc.py) | Conformal Risk Control distribution-free calibration mapping $\alpha \to \lambda$. |
| [`budgetpilot/core/safety.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/safety.py) | Kill switch, automated secret redaction, `TaskCapGuard`, local network confinement. |
| [`budgetpilot/adapters/mcp_server.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/adapters/mcp_server.py) | MCP 2.x server exposing control tools and gate-enforced proxy tools. |
| [`budgetpilot/adapters/hooks.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/adapters/hooks.py) | Pure standard-library hook shim (<60ms SLA) for Claude Code PreToolUse/PostToolUse. |

Welcome aboard, and happy hacking on BudgetPilot!

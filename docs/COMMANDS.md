# BudgetPilot — Operations & CLI Command Reference

> **Complete operations manual and command cheat sheet for BudgetPilot (v1.0.0).**
> Covers system initialization, diagnostics, interactive chat, autonomous task execution, dynamic budget dialing, multi-arm benchmarking, financial reporting, and MCP host tool operations.

---

## Quick Command Cheat Sheet

| Command | Category | Description | Primary Flags / Arguments |
| :--- | :--- | :--- | :--- |
| [`bp doctor`](#1-system-operations--diagnostics) | Diagnostics | System health check (Ollama, SQLite, git, rg, latency SLA <60ms) | None |
| [`bp init`](#1-system-operations--diagnostics) | Setup | Initialize storage (`~/.budgetpilot`), migrations, and config files | `--mode [enforce\|observe\|off]` |
| [`bp config`](#1-system-operations--diagnostics) | Inspection | Inspect active configuration, model tiers, and limit caps | `[--json]` |
| [`bp status`](#1-system-operations--diagnostics) | Status | View runtime mode, budget slider, CRC bounds ($\alpha \to \lambda$), historical spend | None |
| [`bp chat`](#2-interactive-operations) | Interactive | Chat loop with live token telemetry, anomaly alerts, and USD costs | `[--tier cheap\|strong]` `[--session-id ID]` |
| [`bp run`](#3-autonomous-task-execution) | Autonomous | Run autonomous coding task with Execute Gate and recovery routing | `--task "..."` `[--repo PATH]` `[--mode enforce\|observe\|off]` |
| [`bp budget`](#4-conformal-calibration--budget-dial) | Budget Dial | Inspect or dynamically change runtime budget slider operating point | `[tight\|balanced\|loose]` |
| [`bp rollout collect`](#4-conformal-calibration--budget-dial) | Calibration | Collect empirical rollout trajectories for offline CRC calibration | `--failures DIR` `--seeds N` `[--db PATH]` |
| [`bp bench baseline`](#5-benchmarking--statistical-evaluation) | Benchmark | Run ungated baseline benchmark in temporary isolated git worktrees | `--suite FILE` `[--repeats N]` |
| [`bp bench run`](#5-benchmarking--statistical-evaluation) | Benchmark | Run multi-arm benchmark suite (`off`, `observe`, `tight`, `balanced`, `loose`) | `--suite FILE` `--arms ARMS` `[--cost-cap-usd USD]` |
| [`bp report`](#6-reporting--financial-auditing) | Financials | Generate session / repo report with 3-metric cost separation | `[--latest]` `[--format terminal\|markdown\|json\|html]` |
| [`bp estimator eval`](#7-scope-estimator-operations) | Estimator | Evaluate lexical/ripgrep scope estimator against commit ground truth | `[--repo PATH]` `[--limit N]` `[--json]` |

---

## 1. System Operations & Diagnostics

### `bp doctor`
Verifies environment dependencies, connectivity, storage health, and gate latency SLA.

```bash
# Run complete system diagnostics
bp doctor
```

**Checks Performed:**
1. **Ollama Server**: Verifies connectivity to local Ollama daemon (`http://localhost:11434` or `$OLLAMA_HOST`).
2. **Tool-Calling Models**: Confirms presence of tool-calling models (e.g. `qwen2.5:3b`, `qwen3:4b`).
3. **Git Binary**: Verifies `git` availability for worktrees and snapshots.
4. **Ripgrep (`rg`)**: Confirms `rg` binary for sub-50ms lexical scope probes.
5. **Database & Storage**: Validates SQLite WAL mode, schema integrity, and 14 core tables in `~/.budgetpilot/bp.db`.
6. **Prices Configuration**: Verifies `prices.json` reference rates for `cheap` and `strong` tiers.
7. **Gate Latency SLA**: Executes 50 gate iterations; enforces **p95 < 60ms** (typical measured: ~0.03ms).

---

### `bp init`
Initializes BudgetPilot home directory (`~/.budgetpilot`), executes database schema migrations, and seeds default configuration and pricing files.

```bash
# Initialize with default enforce mode
bp init

# Initialize with observe mode (logging without blocking)
bp init --mode observe

# Initialize with kill switch active (fail-open bypass)
bp init --mode off
```

---

### `bp config`
Displays the active configuration including model tiers, context windows, reference prices, and safety cap limits.

```bash
# Display human-readable configuration
bp config

# Dump configuration as parseable JSON
bp config --json
```

---

### `bp status`
Displays high-level system status, active runtime mode, Conformal Risk Control calibration guarantees, historical spend, and gate counts.

```bash
bp status
```

**Example Output:**
```text
============================================================================
 BudgetPilot System Status & Calibration Guarantees (Phase L)
============================================================================
  BP Home:           ~/.budgetpilot
  Runtime Mode:      ENFORCE
  Active Slider:     balanced
  CRC Calibration:   CALIBRATED

----------------------- Conformal Calibration Guarantees ---------------------
  Level        Risk Bound (α)   Lambda (λ)   Empirical Risk   Status      
  ----------------------------------------------------------------------
  tight        0.30             2.5000       0.24             calibrated
  balanced     0.20             0.5000       0.16             calibrated <-- active
  loose        0.10             0.1000       0.07             calibrated

----------------------- Historical Savings & Financials ----------------------
  Total Sessions:           88
  Measured Spend (Exact):   $0.033869 USD
  Avoided Spend (Point):    $0.018420 USD  (Range: [$0.012100, $0.024800])
  Baseline Equivalent Cost: $0.052289 USD
  Historical Savings Ratio: 35.2%  (avoided / [measured + avoided])

--------------------------- Gate Activity ----------------------------------
  Allow: 62 | Rewrite: 14 | Deny: 8 | Overrides Deducted: 2
============================================================================
```

---

## 2. Interactive Operations

### `bp chat`
Starts an interactive terminal session with live token accounting, real-time USD billing against cloud reference rates, and non-mutating anomaly detection alerts (mean + 2σ threshold).

```bash
# Start interactive chat with cheap model tier (default)
bp chat

# Start interactive chat with strong model tier
bp chat --tier strong

# Attach chat session to a specific session ID
bp chat --session-id bug_investigation_42
```

**Controls During Chat:**
- Type any prompt and press `Enter`.
- Type `exit` or press `Ctrl+C` / `Ctrl+D` to quit gracefully.

---

## 3. Autonomous Task Execution

### `bp run`
Executes an autonomous software engineering task against a target repository using BudgetPilot's multi-turn loop, Execute Gate policy matrix, and recovery router.

```bash
# Standard autonomous execution in active enforcement mode
bp run --task "Fix KeyError in user_session when token expires" --repo .

# Autonomous execution in passive observe mode (records decisions without blocking)
bp run --task "Refactor database connection pool" --repo /path/to/repo --mode observe

# Autonomous execution with tight budget dial (aggressive token minimization)
bp run --task "Add unit tests for payment processing" --budget tight --max-turns 15

# Autonomous execution starting on strong model tier
bp run --task "Resolve race condition in worker threads" --tier strong
```

**CLI Flags for `bp run`:**
- `--task` *(required)*: Description of coding defect or feature request.
- `--repo`: Target repository directory (default: current directory `.`).
- `--mode`: Enforcement mode: `enforce` (default), `observe`, or `off`.
- `--tier`: Model tier: `cheap` (default) or `strong`.
- `--max-turns`: Maximum allowed agent turns before aborting (default: 20).
- `--budget`: Runtime budget slider setting: `tight`, `balanced` (default), or `loose`.

---

## 4. Conformal Calibration & Budget Dial

### `bp budget`
Dynamically inspects or adjusts the runtime budget slider operating point without restarting services or retraining models.

```bash
# View active budget slider position and calibrated risk parameters
bp budget

# Set budget slider to tight (minimizes cost, λ = 2.50)
bp budget tight

# Set budget slider to balanced (optimal trade-off, λ = 0.50)
bp budget balanced

# Set budget slider to loose (maximizes solve rate, λ = 0.10)
bp budget loose
```

---

### `bp rollout collect`
Executes offline calibration rollouts across induced failure states to gather empirical trajectory data for Conformal Risk Control.

```bash
# Collect rollouts across 5 random seeds per failure state
bp rollout collect --failures bench/synthetic_failures --seeds 5

# Collect rollouts with explicit output format and database target
bp rollout collect --failures bench/synthetic_failures --seeds 3 --format json
```

---

## 5. Benchmarking & Statistical Evaluation

### `bp bench baseline`
Runs unconstrained (ungated) baseline benchmarks against a YAML task suite in isolated, temporary git worktrees to establish ground-truth solve rates and expenditures.

```bash
# Run baseline benchmark suite with 3 repeats per task
bp bench baseline --suite bench/tasks.example.yaml --repeats 3
```

---

### `bp bench run`
Runs multi-arm evaluation comparing ungated baseline (`off`), passive observation (`observe`), and active conformal budget arms (`tight`, `balanced`, `loose`). Enforces per-trial turn and USD cost caps.

```bash
# Run multi-arm benchmark across all operating arms
bp bench run \
  --suite bench/tasks.example.yaml \
  --arms off,observe,tight,balanced,loose \
  --repeats 3 \
  --cost-cap-usd 0.50 \
  --max-turns 20
```

---

### `bp bench analyze`
Computes statistical evaluation metrics comparing gated arms against baseline:
- **Equal Solve Status Pairing**: Isolates pure cost savings by strictly pairing runs with matching solve outcomes (`passed == passed`).
- **Bootstrap Confidence Intervals**: 1,000 non-parametric percentile bootstrap resamples for paired dollar savings.
- **Expand-Scope Win Rate**: Frequency where local dependency expansion succeeded cheaper than escalation.

```bash
# Generate terminal analysis report
bp bench analyze --baseline-arm off

# Export analysis report as Markdown or JSON
bp bench analyze --format markdown > bench_results.md
bp bench analyze --format json > bench_results.json
```

---

## 6. Reporting & Financial Auditing

### `bp report`
Generates comprehensive session, multi-session, or repository-wide financial audit reports with strict **3-Metric Cost Separation**:
1. `[1] Measured Spend (Exact)`: Real USD from model token usage.
2. `[2] Modeled Avoided Spend`: Counterfactual savings from gate-denied calls with p25–p75 uncertainty bands (`low_usd <= avoided_usd <= high_usd`).
3. `[3] Paired-Run Savings`: Empirical savings from benchmark baseline pairs at equal solve status.

```bash
# View summary report across all sessions (terminal)
bp report

# View report for the most recent session
bp report --latest

# View report for a specific session ID
bp report --session sess_1790419357

# Generate dark-themed interactive HTML dashboard
bp report --format html --output report.html

# Generate Markdown report for GitHub PRs or documentation
bp report --format markdown --output financial_report.md
```

---

## 7. Scope Estimator Operations

### `bp estimator eval`
Evaluates the lightweight Scope Estimator (fast lexical heuristics + <50ms ripgrep probe) against the repository's git commit history ground truth.

```bash
# Evaluate estimator on current repository
bp estimator eval

# Evaluate across last 50 commits
bp estimator eval --limit 50

# Output evaluation metrics as JSON
bp estimator eval --json
```

---

## 8. Safety, Hardening & Kill Switch Operations

BudgetPilot includes defense-in-depth safety controls engineered for zero-disruption fail-open resilience.

### Emergency Kill Switch (`BP_MODE=off`)
To instantly disable all BudgetPilot policy gating, tool interception, and recovery redirections, set the environment variable:

```bash
# Activate global kill switch (all tools fail-open to ALLOW immediately)
export BP_MODE=off

# Re-enable standard enforcement
export BP_MODE=enforce
```

### Automated Secret Redaction
Active automatically across all SQLite write queries and fallback error logs. Redacts:
- AWS Access Keys (`AKIA...` $\to$ `[REDACTED_AWS_KEY]`)
- AWS Secret Keys (`wJalrXUtn...` $\to$ `[REDACTED_AWS_SECRET]`)
- GitHub PATs (`ghp_...` $\to$ `[REDACTED_TOKEN]`)
- Slack API Tokens (`xox[baprs]-...` $\to$ `[REDACTED_TOKEN]`)
- Bearer Authorization Headers (`Bearer eyJ...` $\to$ `Bearer [REDACTED_TOKEN]`)
- Private Keys (`-----BEGIN RSA PRIVATE KEY-----...` $\to$ `[REDACTED_PRIVATE_KEY]`)
- JSON Web Tokens (`eyJ...` $\to$ `[REDACTED_JWT]`)

### Hard Per-Task Cap Guards (`TaskCapGuard`)
Automatically enforced during agent loops:
- **Max Denials**: Max 5 gate denials per task before auto-switching to `observe` mode.
- **Max Recovery Attempts**: Max 3 recovery loops per task.
- **Max Turns**: Max 25 turns per task.
- **Max Escalation Spend**: Max $1.00 USD spent during strong-tier escalation.

### Network Egress Confinement
BudgetPilot is local-only. All network sockets are audited:
- Permitted targets: `localhost`, `127.0.0.1`, `::1`, or `$OLLAMA_HOST`.
- All external third-party outbound connections are rejected with `PermissionError`.

---

## 9. MCP Host Tools & Hook Operations

When connected to an MCP client (Google Antigravity, Claude Code, Cursor), BudgetPilot exposes native tools:

### MCP Control Tools

| Tool Name | Parameters | Description |
| :--- | :--- | :--- |
| `bp_status` | None | Returns task state, read/touched sets, turn count, and dollar spend. |
| `bp_set_budget` | `budget: "tight" \| "balanced" \| "loose"` | Dynamically changes the active budget slider operating point. |
| `bp_request_scope` | `target_scope: 2 \| 3`, `reason: str` | Requests a proactive scope expansion from the Execute Gate. |
| `bp_escalate` | `reason: str`, `target_tier: "strong"` | Escalates task to a frontier model within a temporary git worktree. |
| `bp_report` | `session_id: str` *(optional)* | Retrieves financial and gate-interception report. |

### MCP Proxy Tools (Execute Gate Enforced)

| Tool Name | Gate Enforcement Rules |
| :--- | :--- |
| `bp_list_dir` | Clamped to directory depth 1 for Scope 1 tasks. |
| `bp_search` | Restricted to package directories in Scope 2; repository-wide searches blocked. |
| `bp_read_file` | Read window clamped to 400 lines (Scope 1) or 800 lines (Scope 2); max 3 file reads in Scope 1. |
| `bp_edit_file` | Always permitted across all scopes; tracks touched file sets. |
| `bp_run_tests` | Captures exit codes, stdout, stderr, and automatically classifies test failures into 7 failure classes. |

### Claude Code Hook Shim
Run the ultra-low latency pure standard-library hook (<60ms SLA):

```bash
# Intercept pre-tool execution
python -m budgetpilot.adapters.hooks preToolUse < payload.json

# Intercept post-tool execution
python -m budgetpilot.adapters.hooks postToolUse < payload.json
```

---

## 10. Operational Troubleshooting

| Symptom | Cause | Remediation |
| :--- | :--- | :--- |
| `[FAIL] Ollama Server: Unreachable` | Ollama daemon not running or bound to non-standard port | Run `ollama serve` or configure `export OLLAMA_HOST=http://localhost:11434`. |
| `[FAIL] Database locked` | Concurrent SQLite write contention | BudgetPilot automatically retries and falls open to `~/.budgetpilot/fallback.jsonl`. Verify WAL mode with `bp doctor`. |
| Gate denies necessary file read | Task scope under-estimated for cross-file edit | Repeat tool call within 3 turns to trigger the automatic 3-turn override bypass, or call `bp_request_scope(target_scope=2)`. |
| Urgent emergency during agent session | Runaway agent loop or unexpected tool blocking | Set `export BP_MODE=off` to immediately disable all gating fail-open. |

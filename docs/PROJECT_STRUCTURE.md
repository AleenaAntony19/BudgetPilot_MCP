# BudgetPilot: Repository & Codebase Structure Reference

> **Complete architectural map of the BudgetPilot repository.**
> This reference maps every folder and file in [`budgetpilot/`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot) to its architectural system layer, engineering purpose, and key exported classes and functions.

---

## 1. System Layer Architecture

BudgetPilot is structured into seven distinct architectural layers to preserve modularity, strict zero-dependency isolation, and fail-open resilience:

```
┌────────────────────────────────────────────────────────────────────────┐
│ 1. CLI & Presentation Layer                                            │
│    budgetpilot/cli.py, budgetpilot/config.py                           │
├────────────────────────────────────────────────────────────────────────┤
│ 2. Agent Host Adapters                                                 │
│    budgetpilot/adapters/mcp_server.py, budgetpilot/adapters/hooks.py   │
├────────────────────────────────────────────────────────────────────────┤
│ 3. Agent Orchestration & Execution                                     │
│    budgetpilot/agent/model.py, tools.py, loop.py, escalation.py        │
├────────────────────────────────────────────────────────────────────────┤
│ 4. Core Decision Engine (Zero External Dependencies)                   │
│    budgetpilot/core/estimator.py, gate.py, verifier.py, router.py,     │
│    frontier.py, executors.py, tracker.py                               │
├────────────────────────────────────────────────────────────────────────┤
│ 5. Conformal Calibration & Rollouts                                    │
│    budgetpilot/core/calibration/crc.py, rollouts.py, failures.py       │
├────────────────────────────────────────────────────────────────────────┤
│ 6. Financial Ledger & Avoided Cost Modeling                            │
│    budgetpilot/core/ledger.py, prices.py, savings.py                   │
├────────────────────────────────────────────────────────────────────────┤
│ 7. Storage, Safety & Hardening                                         │
│    budgetpilot/core/store.py, safety.py, types.py                      │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Comprehensive Codebase Inventory

### Layer 1: CLI & Presentation Layer

| File Path | Architectural Layer | Purpose & Responsibility | Key Classes & Functions |
| :--- | :--- | :--- | :--- |
| [`budgetpilot/__init__.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/__init__.py) | Package Root | Exposes library version, metadata, and core exports. | `__version__ = "1.0.0"` |
| [`budgetpilot/cli.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/cli.py) | CLI Entrypoint | Main command-line dispatcher for `bp`. Handles command parsing, subcommands, interactive chat, benchmarking, and reporting. | [`run_doctor()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/cli.py#L35), [`run_chat_loop()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/cli.py#L215), [`run_cli_task()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/cli.py#L315), [`run_budget()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/cli.py#L650), [`run_status()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/cli.py#L720), [`run_report()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/cli.py#L820), [`main()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/cli.py#L2080) |
| [`budgetpilot/config.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/config.py) | Configuration | Manages `BP_HOME` resolution (`~/.budgetpilot`), `config.json` loading and schema validation via Pydantic, runtime mode overrides (`BP_MODE`). | [`ExecutionMode`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/config.py#L14), [`ModelTierConfig`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/config.py#L20), [`TiersConfig`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/config.py#L28), [`LimitsConfig`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/config.py#L49), [`BudgetPilotConfig`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/config.py#L62), [`get_config()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/config.py#L104) |

---

### Layer 2: Agent Host Adapters (`budgetpilot/adapters/`)

| File Path | Architectural Layer | Purpose & Responsibility | Key Classes & Functions |
| :--- | :--- | :--- | :--- |
| [`budgetpilot/adapters/mcp_server.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/adapters/mcp_server.py) | Host Adapter (MCP 2.x) | Standard Model Context Protocol server exposing control tools (`bp_status`, `bp_set_budget`, `bp_request_scope`, `bp_escalate`, `bp_report`) and proxy tools (`bp_list_dir`, `bp_search`, `bp_read_file`, `bp_edit_file`, `bp_run_tests`). | [`BudgetPilotMcpServer`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/adapters/mcp_server.py#L45), [`main()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/adapters/mcp_server.py#L320) |
| [`budgetpilot/adapters/hooks.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/adapters/hooks.py) | Host Adapter (Hooks Shim) | Pure standard-library hook shim (<60ms SLA) for Claude Code `preToolUse` and `postToolUse` lifecycle events. Translates Claude tool payloads to core `BpEvent`. | [`handle_pre_tool_use()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/adapters/hooks.py#L50), [`handle_post_tool_use()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/adapters/hooks.py#L130), [`main()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/adapters/hooks.py#L240) |

---

### Layer 3: Agent Orchestration & Execution (`budgetpilot/agent/`)

| File Path | Architectural Layer | Purpose & Responsibility | Key Classes & Functions |
| :--- | :--- | :--- | :--- |
| [`budgetpilot/agent/model.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/model.py) | LLM Client Wrapper | Safe Ollama client wrapper. Normalizes token counts (`None` $\to$ `0`), handles timeouts, formats tool calls, and returns `ModelResult`. | [`ModelResult`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/model.py#L25), [`chat()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/model.py#L55), [`get_available_models()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/model.py#L140) |
| [`budgetpilot/agent/tools.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/tools.py) | Tool Dispatcher | Sandboxed implementations of agent tools (`read_file`, `edit_file`, `list_dir`, `search`, `run_tests`). Enforces path confinement and 20,000-character output truncation. | [`list_dir()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/tools.py#L45), [`read_file()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/tools.py#L85), [`edit_file()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/tools.py#L125), [`search()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/tools.py#L170), [`run_tests()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/tools.py#L210), [`dispatch_tool()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/tools.py#L260) |
| [`budgetpilot/agent/loop.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/loop.py) | Agent Loop | Multi-turn task execution orchestrator. Intercepts tool calls, invokes Execute Gate, runs Test Verifier, handles recovery actions, and records spend in SQLite. | [`run_agent_loop()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/loop.py#L50) |
| [`budgetpilot/agent/escalation.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/escalation.py) | Worktree Escalation | Strong-tier model escalation manager. Spins up temporary isolated git worktree, executes bounded strong-tier sub-run, extracts unified diff, and guarantees worktree deletion in `finally` blocks. | [`run_escalation_subrun()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/escalation.py#L40) |

---

### Layer 4: Core Decision Engine (`budgetpilot/core/`)

> [!IMPORTANT]
> Modules in `budgetpilot/core/` and `budgetpilot/bench/` have **zero** imports of `ollama`, `mcp`, `fastapi`, or `uvicorn`.

| File Path | Architectural Layer | Purpose & Responsibility | Key Classes & Functions |
| :--- | :--- | :--- | :--- |
| [`budgetpilot/core/types.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/types.py) | Type Contracts | Core dataclasses, serialization protocols, and enums used across all BudgetPilot subsystems. | [`RecoveryAction`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/types.py#L25), [`FailureClass`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/types.py#L45), [`GateVerdictStatus`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/types.py#L80), [`GateVerdict`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/types.py#L95), [`ScopePrediction`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/types.py#L190), [`RecoveryContext`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/types.py#L231), [`RouterDecision`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/types.py#L315), [`BpEvent`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/types.py#L370) |
| [`budgetpilot/core/estimator.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/estimator.py) | Scope Estimator | Fast lexical complexity rules + sub-50ms `ripgrep` identifier probe predicting Scope 1, 2, or 3. Applies low-confidence round-up (<0.35). | [`extract_features()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/estimator.py#L65), [`estimate()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/estimator.py#L198), [`ScopeEstimator`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/estimator.py#L330) |
| [`budgetpilot/core/estimate.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/estimate.py) | Backward Compatibility | Legacy stub forwarding to `estimator.py`. | Legacy alias |
| [`budgetpilot/core/gate.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/gate.py) | Execute Gate | Policy Enforcement Matrix (Scope 1/2/3), 400/800-line read-window clamping, 3-turn override bypass, max denials observe switch, fail-open resilience, and `BP_MODE=off` global kill switch. | [`check()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/gate.py#L99), [`ExecuteGate`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/gate.py#L398), [`extract_named_files()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/gate.py#L47) |
| [`budgetpilot/core/verifier.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/verifier.py) | Test Verifier | Parses pytest/generic test output, strips environmental noise, computes SHA-256 failure signature hashes, and categorizes errors into 7 canonical failure classes. | [`parse_pytest_output()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/verifier.py#L149), [`classify_failure()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/verifier.py#L291), [`compute_signature_hash()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/verifier.py#L125), [`verify()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/verifier.py#L391) |
| [`budgetpilot/core/verify.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/verify.py) | Backward Compatibility | Legacy stub forwarding to `verifier.py`. | Legacy alias |
| [`budgetpilot/core/router.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/router.py) | Recovery Router | Utility maximization $U(a) = P_{\text{success}}(a) - \lambda \cdot C_{\text{norm}}(a)$. Blends Laplace-smoothed empirical posteriors from rollouts. Resolves $\lambda$ via `CrcLambda` and `HandSetLambda`. | [`route()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/router.py#L343), [`HandSetLambda`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/router.py#L33), [`CrcLambda`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/router.py#L54), [`load_empirical_priors()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/router.py#L207), [`RecoveryRouter`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/router.py#L465) |
| [`budgetpilot/core/frontier.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/frontier.py) | Dependency Frontier | Parses traceback frames outside touched files, queries `ripgrep` for missing symbol definitions, discovers related test files, and bumps scope by $+1$ for `expand-scope`. | [`build_dependency_frontier()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/frontier.py#L40), [`DependencyFrontier`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/frontier.py#L20), [`apply_frontier_to_state()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/frontier.py#L160) |
| [`budgetpilot/core/executors.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/executors.py) | Action Directives | Directives and rollback logic: `execute_reflect` (local repair), `execute_replan` (3-line replan + snapshot rollback), `execute_expand_scope`, and `execute_recovery_directive` dispatcher. | [`DirectiveString`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/executors.py#L32), [`execute_reflect()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/executors.py#L61), [`execute_replan()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/executors.py#L154), [`execute_expand_scope()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/executors.py#L200), [`execute_recovery_directive()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/executors.py#L224) |
| [`budgetpilot/core/tracker.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/tracker.py) | Task State Tracker | In-memory and SQLite-backed tracking of task lifecycle, read sets, touched sets, git stash snapshots, and normalized event ingestion. | [`TaskState`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/tracker.py#L25), [`create_git_snapshot()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/tracker.py#L90), [`normalize_tool_call()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/tracker.py#L140) |

---

### Layer 5: Conformal Calibration & Rollouts (`budgetpilot/core/calibration/`)

| File Path | Architectural Layer | Purpose & Responsibility | Key Classes & Functions |
| :--- | :--- | :--- | :--- |
| [`budgetpilot/core/calibration/crc.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/calibration/crc.py) | Conformal Risk Control | Finite-sample distribution-free risk calibration. Computes empirical loss envelopes, calculates corrected risk bounds with confidence $1 - \delta$, and outputs `lambda_table` for `tight` ($\alpha=0.30$), `balanced` ($\alpha=0.20$), and `loose` ($\alpha=0.10$). | [`calibrate_crc()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/calibration/crc.py#L85), [`CrcCalibrator`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/calibration/crc.py#L210), [`load_calibrated_lambda()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/calibration/crc.py#L320) |
| [`budgetpilot/core/calibration/rollouts.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/calibration/rollouts.py) | Rollout Collector | Executes offline action trials across failure states to collect empirical trajectories into the SQLite `rollouts` table. | [`collect_rollouts()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/calibration/rollouts.py#L55), [`load_rollout_counts()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/calibration/rollouts.py#L190) |
| [`budgetpilot/core/calibration/failures.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/calibration/failures.py) | Failure Generators | Generates reproducible synthetic failure states (under-scoped, repairable mutations, capability gap) in isolated git worktrees. | [`generate_failure_state()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/calibration/failures.py#L60), [`FailureState`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/calibration/failures.py#L25) |
| [`budgetpilot/core/calibrate.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/calibrate.py) | Backward Compatibility | Legacy stub forwarding to `calibration/crc.py`. | Legacy alias |

---

### Layer 6: Financial Ledger & Avoided Cost Modeling

| File Path | Architectural Layer | Purpose & Responsibility | Key Classes & Functions |
| :--- | :--- | :--- | :--- |
| [`budgetpilot/core/savings.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/savings.py) | Avoided Cost Engine | Computes counterfactual avoided spend from gate denies/rewrites with p25–p75 uncertainty bands (`low_usd <= avoided_usd <= high_usd`), override deductions, and late-read deductions. | [`compute_avoided_estimate()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/savings.py#L302), [`AvoidedEstimate`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/savings.py#L42), [`compute_paired_run_savings()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/savings.py#L456) |
| [`budgetpilot/core/prices.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/prices.py) | Cloud Pricing | Manages cloud pricing tiers (`cheap` vs `strong`), resolves `prices.json`, and computes per-token rates. | [`load_prices()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/prices.py#L35), [`resolve_prices_path()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/prices.py#L80), [`PriceTier`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/prices.py#L18) |
| [`budgetpilot/core/ledger.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/ledger.py) | Cost Ledger | Ingests raw LLM calls, computes billable tokens using prompt-cache re-use protection, tracks session spend, and detects statistical anomalies (mean + 2σ outlier alerts). | [`CostLedger`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/ledger.py#L30), [`record_model_call()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/ledger.py#L110), [`detect_anomalies()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/ledger.py#L185), [`AnomalyReport`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/ledger.py#L22) |

---

### Layer 7: Storage, Safety & Hardening

| File Path | Architectural Layer | Purpose & Responsibility | Key Classes & Functions |
| :--- | :--- | :--- | :--- |
| [`budgetpilot/core/store.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/store.py) | Persistence & WAL | SQLite connection factory, WAL pragmas (`synchronous=NORMAL`, `busy_timeout=5000`), forward migrations (14 tables), and fail-open logging to `fallback.jsonl`. | [`init_db()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/store.py#L140), [`get_connection()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/store.py#L55), [`safe_execute_write()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/store.py#L240), [`log_fallback_error()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/store.py#L263) |
| [`budgetpilot/core/safety.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/safety.py) | Safety & Hardening | Enforces emergency kill switch (`BP_MODE=off`), regex secret redaction prior to SQLite writes, hard per-task cap guards, and network egress confinement to local Ollama. | [`is_kill_switch_active()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/safety.py#L38), [`redact_secrets()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/safety.py#L116), [`redact_data()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/safety.py#L144), [`TaskCapGuard`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/safety.py#L207), [`is_allowed_network_target()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/safety.py#L332), [`NetworkAuditContext`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/safety.py#L370) |

---

### Benchmarking Subsystem (`budgetpilot/bench/`)

| File Path | Purpose & Responsibility | Key Classes & Functions |
| :--- | :--- | :--- |
| [`budgetpilot/bench/baseline.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/bench/baseline.py) | Runs ungated baseline benchmark tasks in temporary isolated git worktrees. Records ground-truth costs and solve rates. | [`run_baseline_suite()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/bench/baseline.py#L60) |
| [`budgetpilot/bench/runner.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/bench/runner.py) | Multi-arm benchmark runner evaluating `off`, `observe`, `tight`, `balanced`, and `loose` operating points with turn and USD cost caps. | [`run_multiarm_suite()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/bench/runner.py#L75) |
| [`budgetpilot/bench/analysis.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/bench/analysis.py) | Statistical analysis engine: bootstrap confidence intervals (1,000 resamples), equal solve status pairing, expand-scope win rates, and HTML/MD/JSON reporting. | [`bootstrap_ci()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/bench/analysis.py#L45), [`compute_paired_cost_difference()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/bench/analysis.py#L95), [`generate_html_report()`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/bench/analysis.py#L240) |

---

### Legacy Stubs & Backward Compatibility (`budgetpilot/mcp/`, `budgetpilot/recovery/`)

| File Path | Status | Purpose |
| :--- | :---: | :--- |
| [`budgetpilot/mcp/server.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/mcp/server.py) | `STUBBED` | Legacy Phase A stub preserved for API backward compatibility. Modern MCP 2.x server lives in [`budgetpilot/adapters/mcp_server.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/adapters/mcp_server.py). |
| [`budgetpilot/mcp/tools.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/mcp/tools.py) | `STUBBED` | Legacy tool definitions stub. Modern tools live in [`budgetpilot/agent/tools.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/agent/tools.py). |
| [`budgetpilot/recovery/actions.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/recovery/actions.py) | `STUBBED` | Legacy recovery stubs. Modern action executors live in [`budgetpilot/core/executors.py`](file:///home/aleena/Documents/Projects/BudgetPilot_MCP/budgetpilot/core/executors.py). |

# BudgetPilot: Daily Operations & CLI Cheat Sheet

> **1-Page Developer Reference Card for Daily Workflows**

---

## 1. System Health & Diagnostics

```bash
# Verify all 7 dependencies, storage health, and p95 latency SLA (<60ms)
bp doctor

# Inspect active runtime mode, budget slider, spend, and CRC calibration guarantees
bp status

# Dump active JSON configuration
bp config --json

# Re-initialize local database schema and config files
bp init --mode enforce
```

---

## 2. Running Autonomous Agent Tasks (`bp run`)

```bash
# Standard task under active Execute Gate enforcement (Scope 1/2/3)
bp run --task "Fix zero division in calculate_discount" --repo .

# Passive observe mode (monitors and logs tokens without blocking calls)
bp run --task "Add unit tests for shopping cart" --mode observe

# Aggressive cost-saving mode (tight budget dial, λ = 2.50)
bp run --task "Fix typo in docstring" --budget tight --max-turns 10

# Execute on strong model tier (frontier escalation)
bp run --task "Architectural migration of auth tokens" --tier strong
```

---

## 3. Dynamic Conformal Budget Dial (`bp budget`)

Adjust cost vs. accuracy tradeoff at runtime with zero retraining and zero server restart:

```bash
# View active slider position and calibrated λ penalties
bp budget

# Set to tight (aggressive token savings, λ = 2.50)
bp budget tight

# Set to balanced (optimal trade-off, λ = 0.50, default)
bp budget balanced

# Set to loose (maximum solve rate, lowest risk, λ = 0.10)
bp budget loose
```

---

## 4. Financial & Session Reporting (`bp report`)

```bash
# Summary across all sessions in terminal format
bp report

# View detailed financial breakdown for the most recent session
bp report --latest

# View report for a specific session ID
bp report --session sess_1790419357

# Generate dark-themed interactive HTML dashboard
bp report --format html --output report.html

# Export clean Markdown summary for pull requests
bp report --format markdown --output financial_summary.md
```

---

## 5. Benchmarking & Statistical Evaluation (`bp bench`)

```bash
# Run ungated baseline benchmark in isolated git worktrees
bp bench baseline --suite bench/tasks.example.yaml --repeats 3

# Run multi-arm benchmark (off, observe, tight, balanced, loose) with cost caps
bp bench run \
  --suite bench/tasks.example.yaml \
  --arms off,tight,balanced,loose \
  --repeats 3 \
  --cost-cap-usd 0.50

# Analyze paired cost differences (equal solve status) and bootstrap CIs
bp bench analyze --baseline-arm off
```

---

## 6. Testing & Quality Assurance (`pytest`)

```bash
# Run full 289-test suite
pytest -v

# Run safety, kill switch, and secret redaction tests
pytest tests/test_safety.py -v

# Run Execute Gate & Scope Estimator tests
pytest tests/test_gate.py tests/test_estimator.py -v

# Run Recovery Router & Action Executor tests
pytest tests/test_router.py tests/test_executors.py -v

# Run Conformal Risk Control & Avoided Cost tests
pytest tests/test_crc.py tests/test_savings.py -v

# Run strict architectural AST invariant check (0 forbidden imports)
pytest tests/test_types.py -k "forbidden_imports" -v
```

---

## 7. Emergency Controls & Environment Variables

| Variable | Values | Purpose |
| :--- | :--- | :--- |
| `BP_MODE` | `enforce` \| `observe` \| `off` | Global execution mode. Set `BP_MODE=off` as emergency kill switch (all tools fail-open). |
| `BP_BUDGET_SLIDER` | `tight` \| `balanced` \| `loose` | Sets initial runtime budget dial operating point. |
| `BP_HOME` | Path (default: `~/.budgetpilot`) | Custom storage location for SQLite database (`bp.db`) and logs (`fallback.jsonl`). |
| `OLLAMA_HOST` | URL (default: `http://localhost:11434`) | Target address of local Ollama server. Strictly audited for network confinement. |

```bash
# Instant kill switch: bypass all gating and recovery fail-open
export BP_MODE=off

# Re-enable active enforcement
export BP_MODE=enforce
```

---

## 8. Quick Troubleshooting

| Issue | Quick Fix |
| :--- | :--- |
| Ollama Unreachable | Run `ollama serve &` or check `export OLLAMA_HOST=http://localhost:11434` |
| Missing Models | Run `ollama pull qwen2.5:3b` |
| Database Lock Contention | WAL mode handles concurrency; check fallback logs in `~/.budgetpilot/fallback.jsonl` |
| Read Window Clamped | Scope 1 clamps to 400 lines; request scope expansion via `bp_request_scope(target_scope=2)` |
| Overriding a Denied Call | Repeat the same tool call within 3 turns to automatically trigger the 3-turn override bypass |

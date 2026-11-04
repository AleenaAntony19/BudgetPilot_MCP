# BudgetPilot — Post-Build Execution Plan & Operational Roadmap

> **System Status:** Core Development Complete (Phases A through P Verified)
>
> **Diagnostic Status:** `bp doctor` 7/7 Checks Passed (Gate SLA p95 = 0.03ms)
>
> **Test Status:** 288/289 Passing Tests across 20 Modules

## Executive Summary

With the completion of **Phases A through P**, BudgetPilot is fully built, calibrated, safety-hardened, and documented. The project has transitioned from active software construction to operational deployment, integration, and repository-specific calibration.

This document outlines the four primary post-build operational phases required to integrate BudgetPilot into daily developer workflows, execute gated agent tasks, audit token savings, and calibrate risk parameters for target repositories.

## 1. Post-Build Operational Roadmap

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    🎯 BUDGETPILOT POST-BUILD ROADMAP                        │
└─────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ PHASE 1: MCP HOST INTEGRATION                                             │
 │ 🔌 budgetpilot.adapters.mcp_server                                        │
 │ 💡 Connect BudgetPilot proxy tools to Claude Code, Cursor, & Antigravity  │
 └───────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ PHASE 2: LIVE AGENT WORKFLOWS                                             │
 │ ⚡ bp run --repo . --task "..."                                            │
 │ 💡 Run gated execution loops with active budget slider (tight/balanced/loose)
 └───────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ PHASE 3: FINANCIAL TRACKING & COST AUDITING                               │
 │ 📊 bp report                                                              │
 │ 💡 Audit Measured Spend, Modeled Avoided Spend, and Gate Interceptions    │
 └───────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ PHASE 4: CUSTOM REPO CALIBRATION & BENCHMARKING                           │
 │ 🧪 bp bench run --tasks tasks.yaml                                        │
 │ 💡 Collect failure rollouts & calibrate Conformal Risk Control bounds (λ) │
 └───────────────────────────────────────────────────────────────────────────┘

```

## 2. Phase 1: Host Agent & MCP Adapter Integration

BudgetPilot operates as a Model Context Protocol (MCP 2.x) proxy server. To use BudgetPilot with external agent host applications (such as Claude Code, Cursor, or Antigravity), register the local MCP adapter.

### Configuration snippet

Add the following configuration to your host agent's MCP configuration file (e.g., `~/.gemini/config/mcp_config.json` or `.cursor/mcp.json`):

```
{
  "mcpServers": {
    "budgetpilot": {
      "command": "python",
      "args": [
        "-m",
        "budgetpilot.adapters.mcp_server"
      ]
    }
  }
}

```

### Exposed MCP Control & Proxy Tools

* **Control Tools:** `bp_status`, `bp_set_budget`, `bp_request_scope`, `bp_escalate`, `bp_report`

* **Proxy Tools:** `bp_list_dir`, `bp_search`, `bp_read_file`, `bp_edit_file`, `bp_run_tests`

## 3. Phase 2: Live Agent Workflow Execution

When running tasks directly via the CLI or host agent, BudgetPilot enforces real-time tool gating, scope verification, and recovery routing.

### Step 1: Health Diagnostic

Before starting work, verify that local services and SLAs are operational:

```
bp doctor

```

### Step 2: Set the Operating Budget Dial

Adjust the Conformal Risk Control (CRC) budget dial based on task sensitivity:

```
# Options: tight (max savings), balanced (default), loose (max task success rate)
bp budget balanced

```

### Step 3: Execute Task Under Enforced Mode

Run the agent loop on a target repository task:

```
bp run --repo . --task "Refactor session store error handling and update tests" --mode enforce

```

## 4. Phase 3: Financial Tracking & Cost Auditing

BudgetPilot maintains strict 3-metric cost separation to ensure financial reporting accuracy. Metrics are stored in the SQLite ledger at `~/.budgetpilot/budgetpilot.db`.

### Generate Terminal Report

```
bp report --format terminal

```

### Cost Metric Definitions

| 

| **Metric** | **Calculation / Source** | **Purpose** | 
| **Measured Spend** | Exact input/output token count $\times$ tier reference rate | Tracks actual usage cost across cheap and strong model tiers | 
| **Modeled Avoided Spend** | Tokens avoided $\times$ turns remaining $\times$ rate (with 25th–75th uncertainty bounds) | Estimates reference USD saved by gate rewrites and denials | 
| **Paired Benchmark Savings** | Paired difference at equal solve status (from `bp bench`) | Statistically rigorous cost comparison against baseline | 

## 5. Phase 4: Custom Repo Calibration & Benchmarking

To tailor BudgetPilot's risk control parameters ($\lambda$) to a specific enterprise codebase or project:

### Step 1: Collect Failure Rollouts

Generate synthetic failure states (under-scoped, repairable, capability gap) and run rollout trials:

```
bp rollout collect --failures ./failures_dir --seeds 3

```

### Step 2: Calibrate Conformal Risk Control (CRC) Bounds

Compute empirical risk over calibration rollouts to update the `lambda_table` in SQLite:

```
bp budget calibrate --alpha-tight 0.30 --alpha-balanced 0.20 --alpha-loose 0.10

```

### Step 3: Run Paired Benchmark Suites

Evaluate cost savings and success rates across all experimental arms:

```
bp bench run --tasks tasks.yaml --arms off,observe,tight,balanced,loose --repeats 3

```

## 6. Daily Operations Quick Reference

| **Action** | **Command** | **Expected Result** | 
| **Diagnostic Check** | `bp doctor` | 7/7 PASS (Gate p95 < 60ms) | 
| **Check System Status** | `bp status` | Displays active $\lambda$, risk bounds ($\alpha$), and total spend | 
| **Update Budget Dial** | `bp budget [tight│balanced│loose]` | Updates runtime operating point immediately | 
| **Run Task** | `bp run --repo <path> --task "<prompt>"` | Executes gated agent execution loop | 
| **Generate Cost Report** | `bp report` | Outputs Measured Spend and Modeled Avoided Spend | 
| **Run Test Suite** | `pytest` | Runs all 288 verification tests across 20 modules | 

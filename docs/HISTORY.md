# Project history

BudgetPilot has had two released versions. This page explains what each one was, why the design changed, and where the older code lives. For a list of changes, see [CHANGELOG.md](../CHANGELOG.md).

| Version | Date | What it was | Where |
|---|---|---|---|
| 1.0 | 2026-11-05 | A decision engine with its own agent loop, driving a local Ollama model | commit `46e1722` (first commit on `main`) |
| — | 2026-11 | An intermediate "control plane" (gateway, runtime, budget manager, 12 lifecycle tools). Not released | not published |
| **2.0** | 2026-11-15 | Claude Code hooks, a small MCP server, a token ledger, a dashboard and a paired A/B harness | `main` (latest), tag `v2.0.0` |

`main` holds both versions. 1.0 is its first commit, and 2.0 was merged on top of it, so `git log` shows how the project developed. Users install from the latest `main`.

## Version 1.0: a local-model decision engine

The goal was the same as today: stop coding agents wasting tokens by reading too much context, and pick a cheap way to recover when a change fails. The method came from two papers:

- Yin & Feng (2026), arXiv:2607.13034: estimate how complex a task is before acting on it (scope 1, 2 or 3).
- He et al. (2026), arXiv:2607.19338: after a failure, choose between reflect, replan, expand-scope and escalate by expected value, with a budget dial calibrated by Conformal Risk Control (CRC).

Version 1.0 ran **its own agent loop** (`budgetpilot/agent/`) against a local Ollama model, with a "cheap" and a "strong" tier, and offered MCP proxy tools (`bp_read_file`, `bp_search` and others) that enforced scope limits when a host agent called them.

### What it taught us

- **It did not see real spend.** A developer using Claude Code pays for Claude Code's model calls. Version 1.0 managed calls it made itself to a local model, so it could not reduce what the developer actually spent.
- **The proxy tools were optional.** A host agent used its own `Read` and `Grep` tools and called the proxies only when it chose to, so the limits were rarely applied.
- **Savings were estimated, not measured.** Figures came from models of what an ungated agent would have done, and the documentation made latency and savings claims that no experiment backed.

The intermediate control-plane design kept the same basic approach and showed no savings in its own evaluation, so it was set aside.

## Version 2.0: acting inside Claude Code

Version 2.0 keeps the ideas (scope first, a gate, an expected-value recovery router, a budget dial) and moves them to where the tokens are spent:

| Version 1.0 | Version 2.0 |
|---|---|
| Own agent loop on a local model | No model calls. Claude Code, logged in with the developer's plan, is the model |
| Limits applied only if the agent called the proxy tools | Limits applied to Claude Code's own tool calls through `PreToolUse` and `PostToolUse` hooks |
| Recovery actions ran sub-tasks on a local model | Recovery is advice plus a human choice (the 3E loop); escalation suggests `/model` |
| Cost estimated from a pricing table | Token counts read from Claude Code's session transcript, checked against Claude Code's own totals |
| Savings estimated | Savings only from a paired A/B test with hidden tests ([RESULTS.md](RESULTS.md)) |
| Hook formats assumed | Hook formats pinned by captured payloads ([HOOK_CONTRACT.md](HOOK_CONTRACT.md)) |
| Default mode `observe` | Default mode `off` |

The first paired test measured 15.5% fewer billing units with BudgetPilot on (95% CI 9.8% to 20.4%, 6 pairs, Claude Haiku 5.5, a small synthetic repository), with 6 of 6 tasks solved in both arms. [RESULTS.md](RESULTS.md) has the details and caveats.

## What remains from version 1.0

These parts are still in the package and still tested, but are not part of the Claude Code path:

| Part | Status in 2.0 |
|---|---|
| `bp run`, `bp chat`, `bp bench` and `budgetpilot/agent/` | Legacy local-model mode; needs `pip install ".[local]"` |
| `bp init`, `bp config`, `bp report`, `bp estimator eval`, `bp rollout collect`, `bp calibrate` | Work on the shared database and config. `bp calibrate` writes the CRC table the router uses when one exists |
| `bp doctor --legacy` | The original Ollama checks |
| MCP proxy tools (`bp_read_file`, `bp_search`, `bp_list_dir`, `bp_edit_file`, `bp_run_tests`) | Kept for hosts without hooks |
| Root files `main.py`, `mcp_server.py`, `api.py`, `analytics.py`, `budgetpilot_core.py` | Compatibility modules from 1.0. Not installed by `pip install .` and not used by 2.0 |
| The scope estimator, recovery router and CRC calibration | Reused by 2.0, with the fixes listed in the changelog |

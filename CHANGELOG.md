# Changelog

How the project got here, release by release: [docs/HISTORY.md](docs/HISTORY.md).

## Unreleased — host-agnostic and billing-aware (branch `universal`)

### Added

- Host adapters with declared tiers (`enforce`, `gateway`, `tools`, `observe`) and one normalised usage record; `bp hosts`; [docs/HOST_SUPPORT.md](docs/HOST_SUPPORT.md).
- Billing detection per session (presence of variables and settings only, plus the cached `claude auth status`), user overrides, mixed-session marking, and a once-per-session warning when an API key is set although you said you use a plan (`bp install --billing`).
- One display policy for every report: plan and unknown sessions show tokens and billing units, never dollars; API and cloud sessions add "API-equivalent estimate".
- Price book with source and dates (`bp prices`, `bp prices refresh`, `bp prices set`); re-pricing from stored tokens; drift check against Claude Code's own cost (`bp statusline`, `bp ab`); `bp reconcile --console`.
- `bp gateway`: local API proxy with exact usage and an optional hard budget.
- `bp import codex` and `bp capture codex` (experimental, measure only).
- `bp report` shows Claude Code usage (`--legacy` for the 1.0 report).

### Fixed

- Opus 4 and 4.1 were priced as Opus 4.5 ($5 instead of $15 input); unknown models were priced as Sonnet. Both now come from the price book; unknown models are unpriced.
- Two processes creating a new database at once could fail, losing that request's usage.

## 2.0.0 — Claude Code integration (current)

A rebuild. BudgetPilot no longer drives its own model; it runs as Claude Code hooks, a small MCP server and a local dashboard, and Claude Code (logged in with your plan) stays the model.

### Added

- Hook engine for Claude Code (`UserPromptSubmit`, `SessionStart`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `SessionEnd`), built against payloads captured from Claude Code 2.1.325.
- In-chat commands `bp_mode_on`, `bp_mode_off`, `bp_mode_observe`, `bp_budget`, `bp_status` and `bp_dashboard`, handled by a hook without a model call.
- Execution gate: `Read` windows and `Grep` caps through `updatedInput`; denials for full test-suite runs, match-everything searches, unbounded shell output, oversized edits and identical retries; per-task budgets.
- Trimming of noisy Bash output, with the full text kept on disk.
- Failure fingerprints that persist across sessions, and the 3E recovery loop (Estimate, Execute, Expand, advisory Escalate) with a human choice.
- Token ledger read from session transcripts; billing units and API-equivalent cost.
- Commands `bp install`, `bp uninstall`, `bp doctor --claude`, `bp mode`, `bp dashboard`, `bp transcript-check`, `bp capture`, `bp ab`.
- Local dashboard (127.0.0.1 only, read-only).
- Paired A/B harness with a fixture repository and hidden tests, and the first measured result ([docs/RESULTS.md](docs/RESULTS.md)).
- MCP control tools `bp_status`, `bp_mode`, `bp_set_budget`, `bp_dashboard`, `bp_request_scope`, `bp_recovery_options`, `bp_recovery_choose`, `bp_report`. The proxy tools (`bp_read_file`, `bp_search`, `bp_list_dir`, `bp_edit_file`, `bp_run_tests`) are kept for hosts without hooks.

### Changed

- The default mode is `off`.
- One scope-limits table (`core/scope.py`) replaces three inconsistent copies.
- `ollama`, `fastapi`, `uvicorn` and `pytest` are optional extras (`local`, `api`, `dev`).
- `bp serve` starts the real MCP server and accepts `--repo`.
- MCP results are compact JSON and tool descriptions are short.
- `bp doctor` checks the Claude Code integration; the Ollama checks moved to `bp doctor --legacy`.
- The documentation was rewritten and reduced to the six files in `docs/`.

### Removed

- MCP tool `bp_escalate` (a sub-run on a local model).
- The stub package `budgetpilot/mcp`.
- Unmeasured latency and savings claims from the documentation.

### Fixed

- Scope estimation never verified nested paths such as `pkg/file.py`.
- A vague prompt was scored as a confident single-file task.
- `replan` could delete files that were not created during the run.
- `agent/escalation.py` called a method that did not exist.
- Intermittent "database is locked" errors when several processes opened the database.

## 1.0.0 — Local-model decision engine (2026-11-05)

The first version: an MCP server and its own agent loop driving a local Ollama model, with the scope estimator, execute gate, recovery router (reflect, replan, expand-scope, escalate) and CRC-calibrated budget dial. Its code is still in the package as the legacy local-model mode (`bp run`, `bp chat`, `bp bench`) and is described in [docs/HISTORY.md](docs/HISTORY.md).

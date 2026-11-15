# Changelog

## 1.0.0

First release of the Claude Code integration.

### Added

- Hook engine for Claude Code (`UserPromptSubmit`, `SessionStart`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `SessionEnd`), built against payloads captured from Claude Code 2.1.325.
- `bp_mode_on`, `bp_mode_off`, `bp_mode_observe`, `bp_budget`, `bp_status` and `bp_dashboard`, handled by a hook without a model call.
- Execution gate: `Read` windows and `Grep` caps through `updatedInput`; denials for full test-suite runs, match-everything searches, unbounded shell output, oversized edits and identical retries; per-task budgets.
- Trimming of noisy Bash output, with the full text kept on disk.
- Failure fingerprints that persist across sessions, and the 3E recovery loop (Estimate, Execute, Expand, advisory Escalate) with a human choice.
- Token ledger read from session transcripts; billing units and API-equivalent cost.
- `bp install`, `bp uninstall`, `bp doctor`, `bp mode`, `bp dashboard`, `bp transcript-check`, `bp capture`, `bp ab`.
- Local dashboard (127.0.0.1 only, read-only).
- Paired A/B harness with a fixture repository and hidden tests.
- MCP tools `bp_mode`, `bp_dashboard`, `bp_recovery_choose`, `bp_recovery_options`.

### Changed

- The default mode is `off`.
- One scope-limits table (`core/scope.py`) replaces three inconsistent copies.
- `ollama`, `fastapi`, `uvicorn` and `pytest` are optional extras (`local`, `api`, `dev`).
- `bp serve` starts the real MCP server and accepts `--repo`.
- MCP results are compact JSON and tool descriptions are short.
- `bp doctor` checks the Claude Code integration; the Ollama checks moved to `bp doctor --legacy`.

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

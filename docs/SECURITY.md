# Security model

BudgetPilot runs inside your Claude Code sessions as hooks. This page says what it does, what it deliberately does not do, and what it cannot protect you from.

## What BudgetPilot does

| Property | How | Test |
|---|---|---|
| Hooks fail open | Any exception, malformed input, missing or locked database gives exit code 0 and no output, so Claude Code proceeds normally | `test_malformed_input_fails_open`, `test_locked_database_fails_open_and_queues_writes`, `test_property_any_payload_exits_zero_with_empty_or_valid_output` |
| No network, no model calls, no API keys | The hook, the MCP server and the dashboard use only the standard library, SQLite and local files | `test_hook_process_imports_no_heavy_modules`, `test_page_has_no_external_urls_and_all_panels` |
| Never auto-approves a write or a command | `permissionDecision: "allow"` is returned only for `Read` and `Grep` | `test_edits_are_never_auto_approved`, the property test |
| Never auto-approves outside the project or on secrets | Paths are resolved (symlinks followed) and must stay inside the working directory; `.env`, key files, `.ssh/`, `.aws/`, `credentials*` and similar get no opinion | `test_paths_outside_repo_and_secrets_are_never_auto_approved` |
| Does not change files on its own | The only file-changing action is the **Estimate** recovery step, which restores the copies saved before the task's first edit of each file. It runs only after the human chose it, refuses paths outside the repository or inside `.git`, and deletes only files git reports as untracked | `test_estimate_restores_the_pre_task_snapshot_only_when_chosen` |
| Append-only towards the conversation | It never edits the transcript or an earlier tool result | by design |
| Local-only dashboard | Binds `127.0.0.1`, refuses requests with a foreign `Host` header, opens the database read-only, sends no CORS headers, loads nothing external | `test_binds_loopback_only_and_rejects_foreign_hosts`, `test_database_is_opened_read_only` |
| Reversible install | Settings are merged, backed up, and removed exactly by `bp uninstall` | `test_install_merges_hooks_and_uninstall_removes_exactly_them` |
| `off` really is off | With mode `off`, hooks return before opening the database | `test_mode_off_is_silent_and_writes_nothing` |

## What is stored, and where

Everything stays on your machine, under `~/.budgetpilot` (or `$BP_HOME`):

| File | Contents |
|---|---|
| `bp.db` | Task rows, gate decisions, token counts per API message, failure fingerprints (error type, top frame, a short message), recovery decisions |
| `raw/<hash>.txt` | Full text of Bash output that was trimmed, mode `0600`. **This can contain whatever the command printed, including secrets.** Delete the folder when you like |
| `snapshots/<task>/` | Copies of files as they were before a task first edited them; removed after a day |
| `captures/` | Raw hook payloads, only while `bp capture on` is active. Payloads include prompts, file contents and command output |
| `hook_errors.log`, `outbox.jsonl` | Internal errors, and log rows waiting for a locked database |

Prompts are not stored: only an estimated token count and the file names mentioned in the prompt. `bp doctor` warns if the home directory is writable by other users.

## What BudgetPilot is not

- **Not a sandbox.** It does not isolate commands, limit CPU, memory or network, or restrict what an allowed command can do. Claude Code's own permission system is the control for that; BudgetPilot only adds denials for a few wasteful calls.
- **Not a security filter.** The secret-path list is a short pattern list used to decide when *not* to auto-approve. It does not stop Claude Code from reading a secret if you approve the read.
- **Not a hard token cap.** It cannot limit the model's output tokens. Budgets act at tool boundaries; a denied call can be repeated to override it, and after three denials in a task the gate only observes.
- **Not a guarantee of human review.** The human-in-the-loop step relies on Claude Code showing its permission dialog. In permission modes that skip dialogs, and in headless runs, it is not shown; set `BP_HITL=0` there.
- **The MCP tools `bp_recovery_choose` and `bp_request_scope` can be called by the model.** `bp_recovery_choose` records the choice as the human's, and `bp_request_scope` raises the task's limits. Do not add them to an auto-allow list if you want to make those decisions yourself.

## Trust

- Hook commands run with your user's privileges on every tool call. `bp install` writes the absolute path of the Python interpreter it was run with; anyone who can modify that environment or `~/.claude/settings.json` can run code as you.
- The version 1.0 local-model mode (`bp run`, `bp chat`) executes model-written commands itself and predates these protections. It is not part of the Claude Code path ([HISTORY.md](HISTORY.md)).

## Reporting a problem

Please report security problems privately to the maintainers rather than in a public issue.

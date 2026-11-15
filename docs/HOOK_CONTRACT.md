# Claude Code hook contract used by BudgetPilot

This is the exact contract the hook engine (`budgetpilot/adapters/hooks.py`) is built on.

**Sources, in order of authority**

1. **Real captured payloads** from Claude Code **2.1.325** (CLI, headless `claude -p`, Linux), recorded with `bp capture on`. They are stored, scrubbed, in `tests/fixtures/hooks/` and are what the tests run against.
2. **Probe session** on the same version: a throwaway hook returned each output form and the model reported what it received.
3. The official reference, <https://code.claude.com/docs/en/hooks>, read on 2026-11-15.

Where the capture and the docs disagree, the capture wins. Items that could not be checked from a headless CLI session are listed under [Not yet verified](#not-yet-verified).

## Input (stdin, one JSON object)

### Common fields

| Field | Seen on | Notes |
|---|---|---|
| `session_id` | all events | |
| `transcript_path` | all events | Absolute path to the session `.jsonl`. |
| `cwd` | all events | Absolute. |
| `hook_event_name` | all events | |
| `prompt_id` | every event after the first prompt (not `SessionStart`) | UUID of the user prompt being processed. BudgetPilot uses it as the task id. The transcript's `user` rows carry the same value as `promptId`. |
| `permission_mode` | `UserPromptSubmit`, tool events, `Stop` | Absent on `SessionStart` and `SessionEnd`. |
| `effort.level` | tool events, `Stop` | Absent on `SessionStart`, `UserPromptSubmit`, `SessionEnd`. |

### Per event

| Event | Extra fields (captured) |
|---|---|
| `SessionStart` | `source` (`startup`, `resume`, `clear`, `compact`, `fork`). `model` is documented as optional and was absent. |
| `UserPromptSubmit` | `prompt` (the submitted text) |
| `PreToolUse` | `tool_name`, `tool_input`, `tool_use_id` |
| `PostToolUse` | `tool_name`, `tool_input`, `tool_response`, `tool_use_id`, `duration_ms` |
| `PostToolUseFailure` | `tool_name`, `tool_input`, `tool_use_id`, `error` (string), `is_interrupt`, `duration_ms` |
| `Stop` | `stop_hook_active`, `last_assistant_message`, `background_tasks`, `session_crons` |
| `SessionEnd` | `reason` (`other` in headless runs) |

### `tool_input` by tool (captured)

| Tool | Fields |
|---|---|
| `Read` | `file_path` (always absolute), optional `offset`, `limit` |
| `Grep` | `pattern`, `path`, `output_mode`, `head_limit`. Docs also list `glob`, `-i`, `multiline`. |
| `Glob` | `pattern`, `path` |
| `Edit` | `file_path`, `old_string`, `new_string`, `replace_all` |
| `Write` | `file_path`, `content` |
| `Bash` | `command`, `description`. Docs also list `timeout` (ms) and `run_in_background`. |

`MultiEdit` did not appear in this version. It stays in the matcher because an unused name is harmless.

### `tool_response` by tool (captured)

| Tool | Shape |
|---|---|
| `Read` | `{"type": "text", "file": {"filePath", "content", "numLines", "startLine", "totalLines"}}` |
| `Grep` | `{"mode", "numFiles", "filenames", "content", "numLines", "totalLines"}`, plus `appliedLimit` when `head_limit` was set |
| `Glob` | `{"filenames", "durationMs", "numFiles", "truncated", "totalMatches", "countIsComplete"}` |
| `Edit` | `{"filePath", "oldString", "newString", "originalFile", "structuredPatch", "userModified", "replaceAll"}` |
| `Write` | `{"type": "create", "filePath", "content", "structuredPatch", "originalFile", "userModified"}` |
| `Bash` | `{"stdout", "stderr", "interrupted", "isImage", "noOutputExpected"}`, plus `persistedOutputPath` and `persistedOutputSize` when the output was large |

### Findings that shaped the design

- **A failing command is a `PostToolUseFailure`, not a `PostToolUse`.** A `pytest` run with exit code 1 arrived only as `PostToolUseFailure`, with `error` set to `"Exit code 1\n<combined output>"`. Failure detection therefore reads `error`.
- **A failed command's output cannot be rewritten.** `PostToolUseFailure` accepts only `additionalContext`. BudgetPilot cannot shorten failing test output; it can only add a short note next to it.
- **Claude Code already cuts large Bash output.** A 200,000-character output reached the hook (and the model) as 30,000 characters, with the full text in `persistedOutputPath`. BudgetPilot's trimming works on what is left.

## Output (stdout, one JSON object, exit code 0)

No output and exit 0 means "no opinion": Claude Code continues normally. BudgetPilot prints nothing unless it changes something.

### `PreToolUse`

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "allow | deny | ask | defer",
  "permissionDecisionReason": "...",
  "updatedInput": {"...": "..."},
  "additionalContext": "..."}}
```

| Behaviour | Status |
|---|---|
| `allow` + `updatedInput` rewrites the call with no extra model round-trip. A `Read` with no `limit` was rewritten to `limit: 7` and returned 7 lines of 2,700. | Verified by probe |
| `updatedInput` replaces the whole input object, so unchanged fields must be included. | Docs; the probe sent the full object |
| The model is **not** told the input changed. `permissionDecisionReason` on `allow` goes only to the debug log. BudgetPilot adds a one-line `additionalContext` so the model knows the file was windowed. | Verified by probe |
| `additionalContext` reaches the model with the tool result. | Verified by probe |
| `allow` skips the permission prompt, so BudgetPilot sends it only for `Read`, `Grep` and `Glob` on non-secret paths inside `cwd`. | Docs |
| `deny`: the call does not run and the reason is shown to the model. | Docs |
| `ask` in headless mode (`claude -p`) is treated as a denial; the model receives the reason. BudgetPilot never emits `ask` when `BP_HITL=0`. | Verified by probe |
| Precedence across hooks: `deny` > `defer` > `ask` > `allow`. | Docs |

### `PostToolUse`

```json
{"hookSpecificOutput": {"hookEventName": "PostToolUse",
  "additionalContext": "...",
  "updatedToolOutput": {"stdout": "...", "stderr": "", "interrupted": false, "isImage": false, "noOutputExpected": false}}}
```

| Behaviour | Status |
|---|---|
| `updatedToolOutput` sits inside `hookSpecificOutput`. | Docs and probe |
| For `Bash`, sending back the original `tool_response` object with only `stdout` changed replaces what the model sees. | Verified by probe |
| For built-in tools, a value that does not match the tool's output shape is ignored and the original is used. BudgetPilot always starts from the received object and changes only `stdout` / `stderr`. | Docs |

### `PostToolUseFailure`

Only `hookSpecificOutput.additionalContext` is accepted. Verified by probe: the text reached the model.

### `UserPromptSubmit`

```json
{"decision": "block", "reason": "...",
 "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": true}}
```

| Behaviour | Status |
|---|---|
| A blocked prompt never reaches the model. The probe run reported 0 turns, 0 tokens and $0. | Verified by probe |
| `reason` is shown to the user. In headless mode the result text was `UserPromptSubmit operation blocked by hook:` followed by the reason. | Verified by probe |
| `suppressOriginalPrompt: true` leaves the original prompt text out of the block message. | Docs; used in the probe |
| Without `decision`, `hookSpecificOutput.additionalContext` is added to the model's context with the prompt. | Docs |

### `SessionStart`

`hookSpecificOutput.additionalContext` is added before the first prompt (docs).

### `Stop` and `SessionEnd`

BudgetPilot prints nothing on either. A `Stop` hook that returns `decision: "block"` or `additionalContext` makes the model continue, which costs tokens. `SessionEnd` output is discarded by Claude Code.

## Exit codes and timing

| Rule | Source |
|---|---|
| Exit `0`: stdout JSON is applied. Exit `2`: blocking error, stderr is the reason. Any other code: non-blocking error, the action proceeds. | Docs |
| BudgetPilot always exits `0`, including on internal errors (fail open). | Design |
| Default timeout for command hooks is 600 s; 30 s on `UserPromptSubmit`. BudgetPilot registers 10 s (30 s for `Stop` and `SessionEnd`). | Docs |
| `SessionEnd` hooks share a 1.5 s budget unless a hook sets its own `timeout`; the budget then rises to the highest value, up to 60 s. | Docs |
| All matching hooks for an event run in parallel. | Docs; observed (capture and probe hooks both ran) |

## What BudgetPilot registers

| Event | Matcher | Timeout |
|---|---|---|
| `SessionStart`, `UserPromptSubmit` | none | 10 s |
| `PreToolUse` | `Read\|Grep\|Glob\|Bash\|Edit\|MultiEdit\|Write` | 10 s |
| `PostToolUse` | `Bash\|Read\|Grep\|Glob\|Edit\|MultiEdit\|Write` | 10 s |
| `PostToolUseFailure` | `Bash` | 10 s |
| `Stop`, `SessionEnd` | none | 30 s |

`PostToolUse` includes the edit tools because that event is the only signal that an edit actually ran: it is how BudgetPilot learns that the user allowed a permission dialog, and that code changed since a command last ran. The command is `<absolute python> -m budgetpilot.adapters.hooks`.

## Matchers

- `*`, an empty string, or no matcher matches everything.
- A matcher made only of letters, digits, `_`, `-`, spaces, `,` and `|` is a list of exact tool names (`Read|Grep|Glob`).
- Anything else is an unanchored JavaScript regular expression (`mcp__budgetpilot__.*`).

## Transcript (`transcript_path`)

Checked against the same sessions:

- Each `assistant` row has `message.id`, `message.model`, `message.usage`, `timestamp` and `effort`.
- One API message can be written as several rows with the same `message.id` (up to 3 seen). Keeping one row per `message.id` (the one with the largest `output_tokens`) gave totals **identical** to the `usage` block in `claude -p --output-format json`: input 24, cache write 35,929, cache read 465,598, output 2,003, thinking 213.
- `message.usage` fields: `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`, `output_tokens_details.thinking_tokens`, and `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`.
- In these sessions all cache writes were 1-hour writes.

## Not yet verified

These need a person at an interactive session. None of them blocks the hook engine; each has a fallback.

| Item | Why it matters | Fallback in place |
|---|---|---|
| Hook payloads from the **Antigravity extension** (same extension build 2.1.325, but not captured there) | Field names could differ from the CLI | Hooks ignore unknown fields and fail open on missing ones |
| The block `reason` is **visible in the extension UI** after `bp_mode_on` | The user needs to see the confirmation | MCP tool `bp_mode`, and `bp mode on` in a terminal |
| `permissionDecision: "ask"` shows a native dialog in the extension and in the interactive CLI | Human-in-the-loop recovery | Numbered options in `additionalContext`, answered through `bp_recovery_choose` |
| Behaviour of `ask` and `allow` under `bypassPermissions` and `auto` permission modes | `ask` may be skipped | BudgetPilot does not rely on `ask` in those modes |
| `AskUserQuestion` renders in the extension | Recovery options | Plain numbered list |
| Windows command quoting for the hook command | Registration on Windows | Not claimed; Linux only is tested |

To close the first item, run `bp capture on`, do the listed steps inside the Antigravity extension, run `bp capture off`, then:

```bash
python scripts/import_captures.py ~/.budgetpilot/captures tests/fixtures/hooks \
    --source "claude-code <version> antigravity extension" --suffix _antigravity
```

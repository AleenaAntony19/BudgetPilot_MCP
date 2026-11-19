# Installing BudgetPilot

## Requirements

- Python 3.11 or newer, `git`. `rg` (ripgrep) is optional and makes the scope estimate a little better.
- Claude Code (CLI, or the extension in VS Code or a VS Code-based editor), logged in with your Pro or Max plan.
- `ANTHROPIC_API_KEY` **not** set. If it is set, Claude Code bills the API instead of your plan. `bp doctor` warns about it.

## Install

```bash
git clone https://github.com/AleenaAntony19/BudgetPilot_MCP.git   # the main branch is the current version
cd BudgetPilot_MCP
python3 -m venv .venv && source .venv/bin/activate
pip install .                 # or: pip install -e ".[dev]" --config-settings editable_mode=compat
bp install --yes              # hooks + MCP server, user scope
bp doctor --claude            # checks, and measured hook latency on your machine
```

Then start a **new** Claude Code session (hooks are read at session start) and type `bp_mode_on`.

`bp doctor` prints the wall time one hook call costs on your machine. On the development laptop (Linux, Python 3.12, idle) it measured a median of 36 to 39 ms per tool call with BudgetPilot on, 16 ms with it off, against 10 ms for starting Python alone.

A plain `pip install -e .` also works, but its import hook adds tens of milliseconds to every hook call; `editable_mode=compat` avoids that.

### What `bp install` changes

| Change | Where | Undo |
|---|---|---|
| Hook entries for 7 events, merged with yours | `~/.claude/settings.json` (a `settings.json.bp-backup-<time>` copy is kept) | `bp uninstall` |
| MCP server `budgetpilot` | registered with `claude mcp add` | `bp uninstall` |
| What was added | `~/.budgetpilot/install.json` | removed by `bp uninstall` |

Options:

| Option | Meaning |
|---|---|
| `--scope project --repo PATH` | Write to `PATH/.claude/settings.local.json` instead: one repository only |
| `--hooks` / `--mcp` | Only that part |
| `--claude-md` | Add a short command block to `CLAUDE.md`, for hosts that do not run hooks |
| `--yes` | Do not ask for confirmation |

If the `claude` command is not on your `PATH`, `bp install` looks for the binary bundled with the editor extension. If it finds none, it registers the hooks and prints the MCP block for you to add through `/mcp`:

```json
{"mcpServers": {"budgetpilot": {"type": "stdio", "command": "/path/to/.venv/bin/python",
                                "args": ["-m", "budgetpilot.adapters.mcp_server", "--repo", "/path/to/repo"]}}}
```

`--repo` is optional; the server falls back to `$BP_REPO_ROOT`, then to its working directory.

## Commands

Type these as the whole message in Claude Code. They are handled by a hook and never reach the model, so they cost no tokens. Each also exists as a terminal command.

| In Claude Code | Terminal | Effect |
|---|---|---|
| `bp_mode_on` | `bp mode on` | Gate, output limits, recovery and ledger are active |
| `bp_mode_observe` | `bp mode observe` | Measure only: full ledger, no intervention |
| `bp_mode_off` | `bp mode off` | Hooks do nothing (the default after install) |
| `bp_budget tight` / `balanced` / `loose` | `bp budget tight` | Budget dial |
| `bp_status` | `bp status` | Mode, dial, recent token use, current task. In a terminal, `bp status` prints the mode and dial, followed by the router's λ values, CRC calibration state and gate counts |
| `bp_dashboard` | `bp dashboard` | Start or reuse the local dashboard and show its URL |

The mode is global: it applies to every project on this machine.

### MCP tools

`bp install` registers an MCP server named `budgetpilot`. Its tools are for the model or for hosts that do not run hooks; in Claude Code you normally use the commands above.

| Tool | Effect |
|---|---|
| `bp_status` | Mode, dial, token use in the last 24 hours, current task |
| `bp_mode` | Set the mode: `on`, `off` or `observe` |
| `bp_set_budget` | Set the dial: `tight`, `balanced` or `loose` |
| `bp_dashboard` | Start or reuse the dashboard and return its URL |
| `bp_request_scope` | Raise the current task's scope by one level (or to a given level), with a reason. The request is logged |
| `bp_recovery_options` | The pending recovery decision after a failed test run: failure signature, router scores, recommended action |
| `bp_recovery_choose` | Apply a recovery choice: `estimate`, `execute`, `expand`, `escalate`, `retry`, `deny` or `mode_off` |
| `bp_report` | Cost and execution report as JSON, terminal text, Markdown or HTML |
| `bp_read_file`, `bp_search`, `bp_list_dir`, `bp_edit_file`, `bp_run_tests` | Proxy tools that apply the scope limits themselves, for hosts without hooks (see [Hosts](#hosts)) |

The model can call `bp_recovery_choose` and `bp_request_scope` itself. Keep them off any auto-allow list if you want to make those decisions yourself ([SECURITY.md](SECURITY.md)).

### Other terminal commands

| Command | Effect |
|---|---|
| `bp install`, `bp uninstall` | See [Install](#install) |
| `bp doctor --claude` | Check the installation and measure hook latency (`--runs N` timing runs per payload) |
| `bp ab smoke`, `bp ab run`, `bp ab report FILE` | Paired A/B test; see [Measuring savings](#measuring-savings) |
| `bp transcript-check [PATH]` | See [Checking a transcript](#checking-a-transcript) |
| `bp capture on` / `off` | Record raw hook payloads; see [Checking the hook contract](#checking-the-hook-contract-on-your-host) |
| `bp serve [--repo PATH]` | Run the MCP server on stdio (what `bp install` registers) |
| `bp config [--json]` | Print the configuration |
| `bp report [--session ID] [--format …]` | Cost and execution report from the database |
| `bp rollout collect`, `bp calibrate` | Collect recovery rollouts and build the CRC table the router uses instead of the hand-set λ values |
| `bp init`, `bp run`, `bp chat`, `bp bench`, `bp estimator eval`, `bp doctor --legacy` | Version 1.0 local-model mode; see [HISTORY.md](HISTORY.md) |

`bp --version` prints the installed version.

## Configuration

`~/.budgetpilot/config.json` (set `BP_HOME` to move it). Environment variables win over the file.

| Setting | Environment | Values |
|---|---|---|
| `mode` | `BP_MODE` | `enforce`, `observe`, `off` |
| `budget_slider` | `BP_BUDGET_SLIDER` | `tight`, `balanced`, `loose` |
| `hitl` | `BP_HITL` | `1` (ask the human on failures and at the budget boundary) or `0` (headless, CI) |
| `scope_limits` | | per-scope overrides, see [ARCHITECTURE.md](ARCHITECTURE.md#scope-limits) |
| `chars_per_token` | | bytes per token used for estimates (default 3.8) |
| `limits.max_recovery_attempts` | | recovery attempts per task (default 3) |
| `billing.intended_mode` | | `subscription`, `api` or `cloud`: how you pay. Asked once by `bp install` (or `bp install --billing ...`). With `subscription`, a session that has `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` set gets a one-time warning, a dashboard banner and a failed `bp doctor` check |
| `billing.mode_override` | | Force the detected billing mode (`subscription`, `api`, `cloud`, `unknown`); shown as "set by you" |
| `billing.provider_override` | | Force the provider (`anthropic`, `bedrock`, `vertex`, `azure`, ...) |
| `billing.codex_cli.mode_override` | | Billing mode for imported Codex CLI sessions (not recorded in its logs) |
| `gateway.port`, `gateway.upstreams`, `gateway.hard_budget_units`, `gateway.session_headers` | | Gateway settings, see [Gateway](#gateway) |
| `prices.auto_refresh` | | `true`: `bp doctor` and `bp dashboard` refresh the price book when it is older than 30 days (default off) |

Example:

```json
{"mode": "enforce", "budget_slider": "balanced",
 "scope_limits": {"1": {"read_window_lines": 300}}}
```

## Prices

API-equivalent figures come from a price book: per-model rates with their source page and date. A snapshot of Anthropic's [pricing page](https://platform.claude.com/docs/en/about-claude/pricing) ships with BudgetPilot; refreshed copies and rates you set go to `~/.budgetpilot/pricebook.json` (the previous copy is kept as `.bak`).

| Command | Effect |
|---|---|
| `bp prices` | The rates in use, their source and date; "STALE" after 30 days |
| `bp prices refresh` | Fetch the pricing page, check it, and update. A rate change applies from today, so older usage keeps its old price. A page that no longer parses, rates in the wrong order, or a rate moving more than 50% (without `--confirm`) leaves the file unchanged; so does a network failure |
| `bp prices set PROVIDER MODEL --input X --cache-write-5m X --cache-write-1h X --cache-read X --output X` | Set rates yourself (per million tokens), for a model or provider the book does not list |
| `bp reconcile --console FILE` | Compare an exported usage file (CSV or JSON with model, cost and date columns) with the ledger, per model |

A model that is not in the book is shown as unpriced: tokens are counted, but it gets no billing units or dollars. BudgetPilot never prices it as a similar model. Only Anthropic's page can be refreshed automatically; other providers need `bp prices set`.

The older `claude_models` block in `~/.budgetpilot/prices.json` is still honoured, for exact model IDs.

### Price drift check (optional status line)

Claude Code computes its own session cost at list price. To compare BudgetPilot's figure with it continuously, use BudgetPilot as your status line in `~/.claude/settings.json` (this replaces any status line you have):

```json
{"statusLine": {"type": "command", "command": "/path/to/.venv/bin/python -m budgetpilot.cli statusline"}}
```

It shows the mode, model and context use, and records both costs. If they differ by more than 2% in the last three updates, the dashboard says "price table may be stale" and no figure is labelled "reconciled". `bp ab` makes the same comparison against `claude -p` for every test run. Subagents, web search fees, US-only inference and fast mode are not priced by BudgetPilot and can also cause a difference.

## Billing and dollar figures

BudgetPilot detects how each Claude Code session is billed, from the presence of variables and settings only (it never reads a key's value):

| Signal | Detected as |
|---|---|
| `CLAUDE_CODE_USE_BEDROCK`, `_MANTLE`, `_VERTEX`, `_FOUNDRY`, `_ANTHROPIC_AWS`, `_ANTHROPIC_GOOGLE_CLOUD` | cloud |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, an `apiKeyHelper` setting | api (per token) |
| `ANTHROPIC_BASE_URL` alone | api, low confidence (depends on the gateway) |
| a claude.ai login with a plan, from `claude auth status` (cached by `bp doctor` and `bp install`) | subscription |
| nothing | unknown, shown like a subscription |

Dollar figures appear only for api and cloud sessions, labelled "API-equivalent estimate". Plan sessions show tokens and billing units ("relative weighting, not dollars"). A session whose billing changed between start and end is marked mixed and left out of dollar totals. `bp doctor` prints what was detected and why.

## Gateway

`bp gateway` is an optional local proxy between an agent and the model API. Point any agent that lets you set its API base URL at it:

```bash
bp gateway                                       # http://127.0.0.1:8899, upstream https://api.anthropic.com
export ANTHROPIC_BASE_URL=http://127.0.0.1:8899  # in the shell that starts Claude Code
```

It forwards every request unchanged and streams the response back as it arrives, so prompt caching works as before. It reads token usage from each response, which is exact and includes calls the transcript does not record (Claude Code makes small side requests, such as session titles). A response seen by both the gateway and the hooks is counted once.

| Option | Config key | Meaning |
|---|---|---|
| `--port` | `gateway.port` | Default 8899 |
| `--anthropic-upstream`, `--openai-upstream` | `gateway.upstreams` | Where requests go (default the official APIs) |
| `--hard-budget-units N` | `gateway.hard_budget_units` | Refuse model calls once a session has used N billing units (default off) |
| | `gateway.session_headers` | Request headers that identify a session (default `x-claude-code-session-id`) |
| `--capture DIR` | | Record request and response shapes for checking a new host: header names, event names and usage numbers, never text or credentials |

The hard budget is the only true cap BudgetPilot has: the refused call gets a clear error instead of reaching the model. It is checked before each call against the usage recorded so far, so a call that starts in the same instant as the previous response ends can still go through. The gateway sees model calls only, not tool calls: the gate, output trimming and recovery need the hooks.

Front doors: `/v1/messages` (Anthropic, verified with Claude Code 2.1.328), `/v1/chat/completions` and `/v1/responses` (OpenAI format, built from OpenAI's API reference and not yet verified with real traffic). OpenAI's streaming chat reports usage only when the request asks for it (`stream_options.include_usage`). Any other path is forwarded without usage or budget. A response compressed with Brotli or zstd is forwarded but its usage cannot be read.

## Importing Codex CLI usage (experimental, measure only)

```bash
bp import codex            # every log under $CODEX_HOME/sessions (default ~/.codex)
bp report                  # Codex sessions appear with host codex_cli
```

BudgetPilot reads token usage from Codex CLI's session logs: one record per model response, with cached and reasoning tokens. It cannot control Codex, so no gate, limits or recovery apply. Importing again only adds what is new.

- Built from Codex's source code (github.com/openai/codex at 6eea328, 2026-11-19), not yet checked against a real log. `bp capture codex` writes a redacted sample of your newest log (item types, ids, model names and token numbers; no messages, tool calls, instructions or paths) that can be used to check it.
- Codex compresses logs older than 7 days to `.jsonl.zst`, which needs a library BudgetPilot does not ship. They are listed and skipped; decompress them with `zstd -d` to import them.
- The logs do not say how Codex was billed, so these sessions show no dollar figures unless you set `billing.codex_cli.mode_override` in `config.json`. OpenAI models also need rates: `bp prices set openai MODEL ...`.

## Hosts

| Host | Hooks (gate, limits, ledger) | MCP tools |
|---|---|---|
| Claude Code CLI | yes | yes |
| Claude Code extension (VS Code, Antigravity, Cursor) | yes: it shares `~/.claude/settings.json` with the CLI | yes |
| An editor's own built-in agent (not Claude Code) | no | yes, if you add the MCP server to that editor. Only the proxy tools `bp_read_file`, `bp_search`, `bp_list_dir`, `bp_edit_file`, `bp_run_tests` apply the scope limits, and only when the agent calls them |

## Checking a transcript

```bash
bp transcript-check            # the newest local transcript
bp transcript-check ~/.claude/projects/<project>/<session>.jsonl
```

It prints which usage fields the transcript has and the totals BudgetPilot derives from it. Compare them with the `usage` block of `claude -p --output-format json` for the same session.

## Measuring savings

```bash
bp ab smoke        # 3 tasks x 2 arms x 2 repeats on Haiku: a first number
bp ab run --repeats 3 --model sonnet
```

This starts real Claude Code sessions and uses your plan's quota. Results go to `bench_results/` and [RESULTS.md](RESULTS.md).

## Checking the hook contract on your host

The hook formats were captured from the Claude Code CLI. To check another host, such as the extension inside Antigravity:

```bash
bp capture on          # prints the steps to perform in one session
bp capture off
python scripts/import_captures.py ~/.budgetpilot/captures tests/fixtures/hooks \
    --source "claude-code <version> antigravity extension" --suffix _antigravity
```

## Uninstall

```bash
bp uninstall
pip uninstall budgetpilot
rm -rf ~/.budgetpilot          # optional: your ledger and settings
```

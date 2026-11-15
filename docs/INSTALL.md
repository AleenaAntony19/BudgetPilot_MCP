# Installing BudgetPilot

## Requirements

- Python 3.11 or newer, `git`. `rg` (ripgrep) is optional and makes the scope estimate a little better.
- Claude Code (CLI, or the extension in VS Code or a VS Code-based editor), logged in with your Pro or Max plan.
- `ANTHROPIC_API_KEY` **not** set. If it is set, Claude Code bills the API instead of your plan. `bp doctor` warns about it.

## Install

```bash
git clone <this repository> BudgetPilot_MCP
cd BudgetPilot_MCP
python3 -m venv .venv && source .venv/bin/activate
pip install .                 # or: pip install -e ".[dev]" --config-settings editable_mode=compat
bp install --yes              # hooks + MCP server, user scope
bp doctor --claude            # checks, and measured hook latency on your machine
```

Then start a **new** Claude Code session (hooks are read at session start) and type `bp_mode_on`.

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
| `bp_status` | `bp status` | Mode, dial, recent token use, current task |
| `bp_dashboard` | `bp dashboard` | Start or reuse the local dashboard and show its URL |

The mode is global: it applies to every project on this machine.

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

Example:

```json
{"mode": "enforce", "budget_slider": "balanced",
 "scope_limits": {"1": {"read_window_lines": 300}}}
```

Model prices for the API-equivalent figures are built in. Override them in `~/.budgetpilot/prices.json`:

```json
{"claude_models": {"claude-sonnet-5-5": {"input_per_million": 2.0, "cache_read_multiplier": 0.05}}}
```

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

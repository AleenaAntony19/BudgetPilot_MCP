# BudgetPilot

BudgetPilot is a set of Claude Code hooks, a small MCP server and a local dashboard that aim to cut the token cost of coding-agent sessions.
It sizes each prompt, narrows or blocks wasteful tool calls, limits what a task may add to the context, and steps in when tests fail.
It never calls a model itself: Claude Code, logged in with your plan, stays the model.

**Measured effect so far:** in a first paired test (6 pairs, Claude Haiku 5.5, a small synthetic repository), sessions with BudgetPilot on used **15.5% fewer billing units** than sessions with it only observing (95% CI 9.8% to 20.4% fewer), and both solved 6 of 6 tasks. In that run the saving came from the short notes BudgetPilot adds, which led the model to make fewer tool calls and write less; the gate itself changed only 4 calls. It is a small sample on easy tasks: read [docs/RESULTS.md](docs/RESULTS.md) before relying on it. That paired test is the only source of a savings figure; everything else BudgetPilot shows is labelled as an estimate.

## Quickstart

```bash
pip install .            # in a virtual environment, from a checkout of this repository
bp install --yes         # registers the hooks and the MCP server in Claude Code
```

Then open a new Claude Code session and type:

```
bp_mode_on
```

Run `bp doctor --claude` to check the installation and see the hook latency on your machine. Details: [docs/INSTALL.md](docs/INSTALL.md).

## Commands

Type a command as the whole message in Claude Code. A hook handles it before the model sees it, so it costs no tokens.

| Command | Effect |
|---|---|
| `bp_mode_on` | Turn BudgetPilot on |
| `bp_mode_off` | Turn it off: hooks do nothing (the default) |
| `bp_mode_observe` | Measure only: record token use, change nothing |
| `bp_budget tight` \| `balanced` \| `loose` | Set the budget dial |
| `bp_status` | Mode, dial, recent token use, current task |
| `bp_dashboard` | Open the local dashboard (ledger, gate decisions, failures, savings) |

The same commands exist in a terminal: `bp mode on`, `bp budget tight`, `bp status`, `bp dashboard`. The mode applies to all projects on the machine.

## How it works

```
prompt -> estimate -> gate -> output limits -> verify / recover (3E) -> ledger
```

1. **Estimate.** When you send a prompt, BudgetPilot estimates its size and specificity and assigns scope 1 (single file), 2 (a few files) or 3 (broad). When it is unsure it picks the larger scope. The scope sets the limits for the task.
2. **Gate.** Before each tool call, a hook can narrow it or block it. An unbounded `Read` of a long file becomes a 400-line window, and a `Grep` gets a result cap; both are rewritten in place, with no extra model round-trip. A full test-suite run, a match-everything search, `cat` of a huge file and a few similar calls are denied with a concrete alternative.
3. **Output limits.** Each task has a budget for the context its tool results add and for output. BudgetPilot also caps the size of a single edit, blocks an identical retry, trims noisy command output (the full text is kept on disk) and asks once per session for terse answers.
4. **Verify and recover (3E).** When a test run fails, BudgetPilot classifies the failure and recommends one of: **Estimate** again (restore and re-plan), **Execute** again (smallest fix), **Expand** scope, or **Escalate** (advisory: suggest a stronger model). You choose, or decline, or switch BudgetPilot off. Headless runs apply the recommendation automatically.
5. **Ledger.** Real token counts are read from the Claude Code session transcript. Every failure is kept in a per-repository fingerprint ledger that survives sessions, so a repeated failure shows what resolved it last time.

Design and limits in detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Using it with a Pro or Max plan

- BudgetPilot needs no API key. Keep `ANTHROPIC_API_KEY` **unset**, or Claude Code bills the API instead of your plan. `bp doctor` warns if it is set.
- A plan is not billed per token. A saving means more headroom in the plan's 5-hour and weekly limits, not dollars.
- The dashboard shows an "API-equivalent" cost for comparison only.

## Limits

- BudgetPilot **cannot cap a model's output tokens**. Budgets are enforced at tool boundaries and by asking for brevity.
- A denied call can be repeated to override the denial, and after three denials in a task the gate only observes. This is deliberate: it must not stop you from finishing work.
- It cannot shorten the output of a failed command; Claude Code does not allow a hook to rewrite a failure.
- Token counts from transcripts are diagnostic. They matched Claude Code's own report exactly in our checks, but the transcript format is not a documented interface.
- Numbers labelled "estimated" (tokens avoided, BudgetPilot's own overhead) are estimates. **Savings are only claimed from the paired test.**
- Each hook call starts a Python process. `bp doctor` prints the measured cost on your machine.
- The hook formats were verified on the Claude Code CLI. Items not yet verified in editor extensions are listed in [docs/HOOK_CONTRACT.md](docs/HOOK_CONTRACT.md#not-yet-verified).
- It is not a sandbox and not a security filter: [docs/SECURITY.md](docs/SECURITY.md).

## Measuring it yourself

```bash
bp ab smoke                              # 3 tasks x 2 arms x 2 repeats on Haiku
bp ab run --repeats 3 --model sonnet     # all 8 tasks
```

Each task is run twice on a fresh copy of `bench/fixture_repo`: once with BudgetPilot observing only, once with it on, same model, in random order. A hidden test decides whether the task was solved. The report gives the paired difference in billing units with a 95% confidence interval **next to the pass rate of both arms**, because a cheaper run that solves fewer tasks is not a saving. These are real sessions and use your plan's quota.

## Development

```bash
pip install -e ".[dev]" --config-settings editable_mode=compat
python -m pytest -q
python scripts/check_fixtures.py     # every hook fixture must be a real captured payload
```

The hook tests run the real hook process against payloads captured from Claude Code (`tests/fixtures/hooks`).

`bp run`, `bp chat` and `bp bench` are an older local-model (Ollama) mode. They need `pip install ".[local]"` and are not part of the Claude Code integration.

## Background

The recovery router and the scope-first design follow two papers, cited as given by the project authors:

- Yin & Feng (2026), arXiv:2607.13034
- He et al. (2026), arXiv:2607.19338

## Documents

| Document | Contents |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | Installation, commands, configuration, hosts |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The flow, scope limits, gate rules, recovery |
| [docs/HOOK_CONTRACT.md](docs/HOOK_CONTRACT.md) | The Claude Code hook formats BudgetPilot relies on, and how each was verified |
| [docs/RESULTS.md](docs/RESULTS.md) | Paired A/B results |
| [docs/SECURITY.md](docs/SECURITY.md) | What it does and does not protect |

# Host support

What BudgetPilot can do depends on the host running the agent. There are four levels, and they are not interchangeable: only `enforce` changes what the agent does with its tools. `bp hosts` prints this table for your machine, with recent activity.

| Tier | Meaning |
|---|---|
| `enforce` | The host calls BudgetPilot before and after each tool call (hooks): it can narrow, deny or ask, trim output, and run the recovery loop |
| `gateway` | BudgetPilot sits between the agent and the model API: exact usage, and an optional hard budget that refuses model calls. No control over tool calls |
| `tools` | The host only connects to BudgetPilot's MCP server: the agent may use the proxy tools and their limits, or ignore them |
| `observe` | BudgetPilot reads the host's logs afterwards: measured only, no control |

## Hosts

| Host | Tier | BudgetPilot can | BudgetPilot cannot | Verified | Still to verify |
|---|---|---|---|---|---|
| Claude Code CLI | enforce | Gate `Read`/`Grep`/`Bash`/edits, trim Bash output, 3E recovery, per-task budgets, exact token ledger from the transcript, billing detection | Cap model output tokens; shorten a failed command's output; see Claude Code's side requests (titles) in the transcript | Hook payloads captured on 2.1.325; billing signals and `claude auth status` probed on 2.1.328; A/B test of 6 pairs | Windows hook command |
| Claude Code in VS Code-based editors (VS Code, Antigravity, Cursor) | enforce | The same: the extension shares `~/.claude/settings.json` and runs the same hooks | As above | The extension bundles the same Claude Code binary (2.1.328 here); `CLAUDE_CODE_ENTRYPOINT=claude-vscode` observed | That BudgetPilot's hooks fire there with the same payloads (not captured from the extension); how it shows `systemMessage` and `ask` dialogs |
| Any agent with a configurable API base URL, through `bp gateway` | gateway | Exact usage per model call, including calls the transcript misses; hard budget per session | Gate or trim tool calls; shorten a call | Claude Code 2.1.328 with a plan login: one capture; its cost matched Claude Code's own figure exactly | OpenAI chat/Responses front doors (built from the API reference); session headers for other hosts; Bedrock and Vertex upstreams |
| An editor's own agent (Antigravity, Cursor, Cline) connected to the MCP server | tools | Offer proxy tools (`bp_read_file`, `bp_search`, ...) that apply scope limits when called; status and dashboard tools | See or limit the agent's own tool calls; read its token usage | Nothing: none of these has been tested with BudgetPilot | Everything in this row |
| Codex CLI, through `bp import codex` (experimental) | observe | Token usage per model response, with cached and reasoning tokens, in the same ledger and reports | Any control; know how the session was billed | Format read from Codex's source (openai/codex@6eea328); no real log yet | The log format against a real session (`bp capture codex`); `cache_write_input_tokens` semantics |

## Billing and dollars by host

| Host | How billing is known | Dollars shown |
|---|---|---|
| Claude Code | Detected per session from variables, settings and the cached `claude auth status` (see [INSTALL.md](INSTALL.md#billing-and-dollar-figures)) | Only for API or cloud sessions, as "API-equivalent estimate" |
| Gateway | From the upstream and the auth header names (an OAuth plan login is recognised) | Same rule |
| Codex CLI | Not in its logs: unknown unless `billing.codex_cli.mode_override` is set | Same rule; OpenAI models also need `bp prices set` |
| MCP-only hosts | No usage at all | None |

The main claim everywhere is fewer tokens with the same task success, measured by `bp ab` ([RESULTS.md](RESULTS.md)). Dollar figures are a secondary view for people billed per token.

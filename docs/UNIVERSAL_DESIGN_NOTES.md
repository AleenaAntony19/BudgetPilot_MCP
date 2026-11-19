# Universal design notes

Working notes for making BudgetPilot host-agnostic. They list where the code assumes Claude Code and Anthropic, and what moves behind the new host-adapter interface (`budgetpilot/hosts/`). Written 2026-11-19, before Phase A. Status 2026-11-20: phases A to E are implemented on branch `universal`; what each host supports is in [HOST_SUPPORT.md](HOST_SUPPORT.md).

## Where the Claude-specific assumptions live

| Assumption | Where | Plan |
|---|---|---|
| Usage comes from a Claude Code transcript (`type: assistant`, `message.id`, `message.usage`) | `core/cc_ledger.py`: `read_turns`, `parse_usage`, `ingest` | Wrapped as `hosts/claude_code.py` → `iter_usage`. Parser and dedupe (one row per `message.id`, keep max `output_tokens`) unchanged |
| Four token classes with Anthropic semantics: `input_tokens` excludes cached tokens; `cache_creation_input_tokens` split into 5-minute and 1-hour writes; `thinking_tokens` already inside `output_tokens` | `cc_ledger.parse_usage`, the `cc_turns` columns | `UsageEvent` carries the raw counts plus `semantics` flags; `hosts/usage.py` converts any provider's counts to the disjoint classes the ledger stores |
| Billing-unit weights fixed for all models: output ×5, 5-minute write ×1.25, 1-hour write ×2, cache read ×`r` | `cc_ledger.billing_units`, constants at the top | Phase C: per-model rate vector from the price book, units = class rate ÷ that model's input rate |
| `r` (cache-read factor) and the input price are found by model-id prefix, with a silent Sonnet fallback (`price_guess`) | `cc_ledger.DEFAULT_MODEL_PRICES`, `price_for` | Phase C: `price_for(provider, model, ts)` returns `Unpriced` instead of guessing |
| Dollars (`ref_usd`) stored per turn | `cc_turns.ref_usd`, dashboard, `bp transcript-check`, `bp ab` | Kept for compatibility; Phase B/C: display decided by `report_policy`, and dollars can be recomputed from stored tokens |
| `ANTHROPIC_API_KEY` presence = "billed per token" | `cc_cli.doctor` (warn), `bench/ab.py` (removes it for test sessions) | Phase B: `detect_billing` on each adapter, presence only, cloud flags first |
| Host identity = `CLAUDE_CODE_ENTRYPOINT`, stored in `cc_sessions.host` | `hook_engine._host`, `hookdb.touch_session` | Kept as is (it is the entrypoint, such as `cli`). The adapter id goes in a new column `cc_sessions.host_adapter` |
| Control = Claude Code hooks (`PreToolUse` and others) | `adapters/hooks.py`, `adapters/hook_engine.py`, `core/ccgate.py` | Unchanged. The `claude_code` adapter declares tier `enforce` |
| MCP proxy tools apply limits only when the agent calls them | `adapters/mcp_server.py` | Declared by `hosts/generic_mcp.py`, tier `tools`, no usage source |
| Transcript location `~/.claude/projects` or `$CLAUDE_CONFIG_DIR` | `cc_ledger.newest_transcript` | Stays Claude-specific inside the `claude_code` adapter |

## What moves behind the interface in Phase A

- `HostAdapter` (base class: `id`, `tier`, `capabilities()`, `detect_billing()`, `iter_usage()`), `HostCapabilities`, `UsageEvent`, `Cursor`, `BillingContext`.
- The usage path: an adapter emits `UsageEvent`s; `hosts/usage.py` normalises them and writes them to `cc_turns`, which gains `host`, `provider` and `source` columns (defaults `claude_code`, `anthropic`, `transcript`). Existing rows keep their values.
- The hook path does not change in Phase A: hooks still call `cc_ledger.ingest` directly, so hook latency and behaviour are unchanged. The adapter is the entry point for everything new (other hosts, `bp hosts`, the dashboard tier).

## Deliberate choices

- Hook-path modules stay standard library only and avoid `dataclasses` and `typing` at import time (each costs milliseconds on every tool call). The interface is a plain base class rather than a `typing.Protocol`; records are `collections.namedtuple`.
- One usage table (`cc_turns`), not a second one, so the dashboard, `bp ab` and `bp_status` read every host's usage through the same queries.

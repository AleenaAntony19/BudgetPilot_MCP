# BudgetPilot — Integration Guide (Phases M & N)

> **Integration Adapters for External Agents, MCP Hosts & IDEs**
> Supports **Google Antigravity**, **Claude Code**, **Cursor**, and **OpenCode**.

---

## 1. Google Antigravity Integration (`~/.gemini/config/mcp_config.json`)

Google Antigravity natively integrates MCP servers via its global configuration file at `~/.gemini/config/mcp_config.json` or workspace-relative configuration in `.agents/mcp_config.json`.

### Global Antigravity Configuration
Add the `budgetpilot` MCP server entry to `~/.gemini/config/mcp_config.json`:

```json
{
  "mcpServers": {
    "budgetpilot": {
      "command": "python3",
      "args": [
        "-m",
        "budgetpilot.adapters.mcp_server"
      ],
      "env": {
        "BP_MODE": "enforce",
        "BP_BUDGET_SLIDER": "balanced"
      }
    }
  }
}
```

### With Miniconda / Virtualenv
If running BudgetPilot inside a virtual environment (e.g. miniconda):

```json
{
  "mcpServers": {
    "budgetpilot": {
      "command": "/home/aleena/miniconda3/bin/python3",
      "args": [
        "-m",
        "budgetpilot.adapters.mcp_server"
      ],
      "env": {
        "BP_HOME": "/home/aleena/.budgetpilot",
        "BP_MODE": "enforce",
        "BP_BUDGET_SLIDER": "balanced"
      }
    }
  }
}
```

---

## 2. Claude Code Integration

BudgetPilot supports two complementary integration modes with **Claude Code**:
1. **MCP Server**: Exposes explicit control tools (`bp_status`, `bp_set_budget`, `bp_request_scope`, `bp_escalate`, `bp_report`) and proxy tools directly to Claude Code.
2. **Ultra-Low Latency Hooks Shim (`hooks.py`)**: Intercepts native Claude Code tool use (`View`, `Edit`, `LS`, `Grep`, `Bash`) via standard-library IPC in `<60ms`.

### A. MCP Server Configuration (`~/.claude/config.json` or `.claude/config.json`)

```json
{
  "mcpServers": {
    "budgetpilot": {
      "command": "python3",
      "args": [
        "-m",
        "budgetpilot.adapters.mcp_server"
      ],
      "env": {
        "BP_MODE": "enforce",
        "BP_BUDGET_SLIDER": "balanced"
      }
    }
  }
}
```

You can also register it via the Claude Code CLI:
```bash
claude mcp add budgetpilot python3 -m budgetpilot.adapters.mcp_server
```

### B. Ultra-Low Latency Tool Interception Hooks (`.claude/settings.json`)

Configure Claude Code to pipe tool events to `budgetpilot.adapters.hooks`:

```json
{
  "hooks": {
    "PreToolUse": {
      "command": "python3 -m budgetpilot.adapters.hooks"
    },
    "PostToolUse": {
      "command": "python3 -m budgetpilot.adapters.hooks"
    },
    "TaskStart": {
      "command": "python3 -m budgetpilot.adapters.hooks"
    },
    "TaskEnd": {
      "command": "python3 -m budgetpilot.adapters.hooks"
    }
  }
}
```

---

## 3. Tool Reference

### Control Tools
| Tool Name | Parameters | Description |
| :--- | :--- | :--- |
| `bp_status` | `session_id?` | Inspect runtime mode, budget slider, CRC calibration guarantees, and cumulative spend. |
| `bp_set_budget` | `budget_slider?`, `mode?` | Dynamically adjust budget sensitivity (`tight`, `balanced`, `loose`) or mode (`enforce`, `observe`, `off`). |
| `bp_request_scope` | `reason`, `target_scope?` | Request scope expansion (+1 or target level) with rationale. |
| `bp_escalate` | `task_prompt`, `max_turns?`, `cost_cap_usd?` | Launch bounded strong-tier sub-run in an isolated git worktree; returns unified diff patch. |
| `bp_report` | `session_id?`, `format_type?` | Generate financial and execution report in `json`, `terminal`, `markdown`, or `html`. |

### Proxy Tools (Gate-Protected)
| Proxy Tool | Canonical Target | Gate Interception Behavior |
| :--- | :--- | :--- |
| `bp_list_dir` | `list_dir(path, depth)` | Clamps root directory listings (`depth > 2`) to depth 2 to prevent token blowout. |
| `bp_search` | `search(pattern, path)` | Executes ripgrep search with path sandboxing and output truncation. |
| `bp_read_file` | `read_file(path, start, end)` | Rewrites oversized or unbounded reads to max 300-line windows. |
| `bp_edit_file` | `edit_file(path, old, new)` | Enforces touched-file set constraints; blocks editing non-touched files under scope 1. |
| `bp_run_tests` | `run_tests(command, timeout)` | Sandboxed test execution; runs verifier to classify test failures into 7 diagnostic classes. |

---

## 4. Hook Event Payload Examples

### `PreToolUse` (Claude Code View Interception)
**Incoming Stdin:**
```json
{
  "event": "PreToolUse",
  "tool": "View",
  "parameters": {
    "file_path": "budgetpilot/core/gate.py",
    "view_range": [1, 2000]
  },
  "session_id": "claude-sess-42"
}
```

**Stdout Decision (<60ms):**
```json
{
  "verdict": "rewrite",
  "decision": "rewrite",
  "reason": "Oversized read (1999 lines) clamped to 300 lines [1:301]",
  "rewritten_parameters": {
    "path": "budgetpilot/core/gate.py",
    "start": 1,
    "end": 301
  },
  "latency_ms": 1.42
}
```

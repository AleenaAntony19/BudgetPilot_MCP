"""
Host adapters: one per kind of agent host BudgetPilot supports.

    get_adapter("claude_code") -> ClaudeCodeAdapter()
    all_adapters()              -> every registered adapter, in display order

Adapters are imported lazily so that importing this package costs nothing on
the hook path. See docs/UNIVERSAL_DESIGN_NOTES.md and docs/HOST_SUPPORT.md.
"""

from __future__ import annotations

# id -> (module, class name), in display order
REGISTRY = {
    "claude_code": ("budgetpilot.hosts.claude_code", "ClaudeCodeAdapter"),
    "gateway": ("budgetpilot.hosts.gateway", "GatewayAdapter"),
    "generic_mcp": ("budgetpilot.hosts.generic_mcp", "GenericMcpAdapter"),
    "codex_cli": ("budgetpilot.hosts.codex_cli", "CodexCliAdapter"),
}
DEFAULT_HOST = "claude_code"


def get_adapter(host_id: str):
    import importlib

    try:
        module, cls = REGISTRY[host_id]
    except KeyError:
        raise KeyError(f"unknown host adapter {host_id!r}; known: {', '.join(REGISTRY)}") from None
    return getattr(importlib.import_module(module), cls)()


def all_adapters() -> list:
    return [get_adapter(host_id) for host_id in REGISTRY]

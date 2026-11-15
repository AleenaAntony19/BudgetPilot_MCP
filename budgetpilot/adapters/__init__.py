"""
BudgetPilot host adapters: the Claude Code hook engine and the MCP server.

Attributes are resolved lazily (PEP 562) so that running the hook process
(`python -m budgetpilot.adapters.hooks`) never imports the MCP server, `mcp`
or `pydantic`. Hook start-up time is paid on every tool call.
"""

from importlib import import_module

_MCP_NAMES = {"create_mcp_server", "mcp", "run_server"}


def __getattr__(name: str):
    if name in _MCP_NAMES:
        return getattr(import_module("budgetpilot.adapters.mcp_server"), name)
    hooks = import_module("budgetpilot.adapters.hooks")
    try:
        return getattr(hooks, name)
    except AttributeError:
        raise AttributeError(f"module 'budgetpilot.adapters' has no attribute '{name}'") from None

"""
BudgetPilot Adapters Subsystem.
Phases M & N: Integration with external agent hosts and IDE environments.

Adapters:
- `mcp_server.py`: Model Context Protocol 2.x server with control and proxy tools.
- `hooks.py`: Ultra-low latency (<60ms) standard-library shim for tool interception.
"""

from budgetpilot.adapters.hooks import (
    CLAUDE_TOOL_MAP,
    handle_hook_event,
    handle_lifecycle_trigger,
    map_claude_tool_to_canonical,
    measure_interception_latency,
    payload_to_bp_event,
)
from budgetpilot.adapters.mcp_server import (
    create_mcp_server,
    mcp,
    run_server,
)

__all__ = [
    "create_mcp_server",
    "mcp",
    "run_server",
    "handle_hook_event",
    "handle_lifecycle_trigger",
    "map_claude_tool_to_canonical",
    "measure_interception_latency",
    "payload_to_bp_event",
    "CLAUDE_TOOL_MAP",
]

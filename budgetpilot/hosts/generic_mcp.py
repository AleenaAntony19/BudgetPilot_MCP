"""
Generic MCP host adapter (tier `tools`).

For hosts that connect to BudgetPilot's MCP server but run no BudgetPilot
hooks: an editor's own agent (Antigravity, Cursor, Cline and similar, each
[VERIFY]: none has been tested with BudgetPilot yet). BudgetPilot cannot see
or stop that agent's own tool calls and cannot read its token usage; it offers
the proxy tools, which apply the scope limits only when the agent chooses to
call them, and the control tools.

Standard library only.
"""

from __future__ import annotations

from budgetpilot.hosts.base import HostAdapter, HostCapabilities


class GenericMcpAdapter(HostAdapter):
    id = "generic_mcp"
    name = "MCP-only hosts (no BudgetPilot hooks)"
    tier = "tools"
    provider = "unknown"

    open_items = (
        "Antigravity, Cursor and Cline: none tested with BudgetPilot's MCP server yet",
    )

    def capabilities(self) -> HostCapabilities:
        return HostCapabilities(
            tier=self.tier,
            proxy_tools=True,
            usage_source=None,
            limitations=(
                "Does not see or control the agent's own tool calls.",
                "Scope limits apply only when the agent calls the bp_* proxy tools.",
                "No token usage: the host does not report it to BudgetPilot. bp_report shows only "
                "what other adapters stored.",
            ),
        )

    def iter_usage(self, since):
        return iter(())

"""
Gateway host adapter (tier `gateway`).

Any agent that lets the user set its model API base URL can be pointed at
`bp gateway` (Claude Code: ANTHROPIC_BASE_URL, verified on 2.1.328 with a plan
login; other hosts [VERIFY]). Usage is read from the API's own responses, so
it is exact and includes calls a transcript does not record (Claude Code's
side requests). The gateway sees model calls, not tool calls: it cannot
narrow a Read or deny a command, but it can refuse a model call once a hard
budget is used up.

The gateway writes usage to the ledger as responses arrive; `iter_usage` has
nothing to read later.

Standard library only.
"""

from __future__ import annotations

from budgetpilot.hosts.base import HostAdapter, HostCapabilities


class GatewayAdapter(HostAdapter):
    id = "gateway"
    name = "Agents pointed at bp gateway (API base URL)"
    tier = "gateway"
    provider = "unknown"

    open_items = (
        "OpenAI chat and Responses front doors: built from the API reference, not captured traffic",
        "Session headers for hosts other than Claude Code",
        "Bedrock and Vertex upstreams (signed requests)",
    )

    def capabilities(self) -> HostCapabilities:
        return HostCapabilities(
            tier=self.tier,
            usage_source="gateway",
            usage_exact=True,
            hard_token_cap=True,   # optional, off by default: gateway.hard_budget_units
            limitations=(
                "Sees model calls, not tool calls: no Read windows, no denials, no output trimming.",
                "The hard budget refuses whole model calls; it cannot shorten one.",
                "Only hosts that let you set the API base URL can use it.",
            ),
        )

    def detect_billing(self, env, ctx=None):
        """From the upstream host and auth header names of each request; see gateway.server.gateway_billing."""
        from budgetpilot.hosts.base import BillingContext
        from budgetpilot.gateway.server import gateway_billing

        ctx = ctx or {}
        b = gateway_billing(ctx.get("upstream", "https://api.anthropic.com"), ctx.get("header_names") or [],
                            ctx.get("beta", ""))
        return BillingContext(b["mode"], b["provider"], b["confidence"], tuple(b["evidence"]), "detected")

    def iter_usage(self, since):
        return iter(())

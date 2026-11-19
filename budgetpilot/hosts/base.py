"""
Host adapter interface and the records every adapter shares.

A host is the program that runs the agent (Claude Code, an editor's agent, a
command-line agent pointed at the gateway). Each adapter declares one tier:

    enforce   the host exposes pre/post tool hooks: BudgetPilot can allow,
              rewrite, deny and ask
    gateway   BudgetPilot is a local HTTP proxy between the agent and the
              model API: exact usage, and a hard request-level budget
    tools     the host connects over MCP only: proxy tools and advice, no
              interception
    observe   usage is imported from the host's own logs: no control

Standard library only, and no `typing` or `dataclasses` import at runtime: the
hook process may import this module and pays its start-up time on every tool
call. Annotations are strings (PEP 563), so the names below are documentation.
"""

from __future__ import annotations

from collections import namedtuple

TIERS = ("enforce", "gateway", "tools", "observe")
TIER_LABELS = {
    "enforce": "enforced",
    "gateway": "API proxy: exact usage, optional hard budget",
    "tools": "advisory (MCP tools only)",
    "observe": "measured only, no control",
}
USAGE_SOURCES = ("transcript", "gateway", "hook", "import")


# What an adapter can do. Shown by `bp hosts` and on the dashboard, so it must
# never claim more than the host allows.
HostCapabilities = namedtuple(
    "HostCapabilities",
    "tier intercept_tool_calls rewrite_tool_input deny_tool_calls ask_user trim_tool_output "
    "proxy_tools usage_source usage_exact hard_token_cap limitations",
    defaults=(False, False, False, False, False, False, None, False, False, ()),
)
#   tier                 one of TIERS
#   intercept_tool_calls the host calls BudgetPilot before each tool call
#   rewrite_tool_input   BudgetPilot can change a call's input (e.g. a Read window)
#   deny_tool_calls      BudgetPilot can stop a call
#   ask_user             BudgetPilot can put a decision to the human
#   trim_tool_output     BudgetPilot can shorten a tool result before the model sees it
#   proxy_tools          BudgetPilot's MCP proxy tools are available
#   usage_source         None or one of USAGE_SOURCES
#   usage_exact          token counts come from the provider's own usage report
#   hard_token_cap       a budget can stop model calls (only the gateway can)
#   limitations          plain-language list of what this host cannot do


# The one normalised usage record. Token fields are the provider's raw counts;
# `semantics` says how they overlap, and `hosts.usage.canonical_tokens` turns
# them into the disjoint classes the ledger stores.
#
#   semantics = {"cached_is_subset_of_input": bool,     cache reads/writes are inside input_tokens
#                "reasoning_is_subset_of_output": bool}  reasoning tokens are inside output_tokens
#   raw_ref   = where the record came from (path:offset, request id), never content
#   task_id, extra = optional host details (Claude Code: prompt id, effort, tools called)
UsageEvent = namedtuple(
    "UsageEvent",
    "session_id host provider model ts message_id input_tokens cache_write_5m cache_write_1h cache_read "
    "output_tokens reasoning_tokens semantics source raw_ref task_id extra",
    defaults=(None, None),
)

ANTHROPIC_SEMANTICS = {"cached_is_subset_of_input": False, "reasoning_is_subset_of_output": True}


# Billing context. Filled in by Phase B (detection); until then every adapter
# reports "unknown", which is displayed like a subscription: tokens, no dollars.
BillingContext = namedtuple(
    "BillingContext",
    "mode provider confidence evidence source",
    defaults=("unknown", "unknown", "low", (), "detected"),
)
BILLING_MODES = ("subscription", "api", "cloud", "unknown")


class Cursor:
    """
    Where an adapter's incremental read stopped. `key` identifies the stream
    (a session id, a log file), `position` is adapter-defined (a byte offset),
    `state` carries anything else the reader needs to resume (Claude Code: the
    current prompt id). `iter_usage` advances the cursor as it yields, and the
    caller saves it only after the events are stored, so a crash re-reads and
    the message-id key absorbs the repeats.
    """

    __slots__ = ("key", "path", "position", "state")

    def __init__(self, key: str, path: str = "", position: int = 0, state: object = None):
        self.key, self.path, self.position, self.state = key, path, position, state

    def __repr__(self) -> str:
        return f"Cursor(key={self.key!r}, path={self.path!r}, position={self.position!r})"


class HostAdapter:
    """
    Base class for host adapters (the interface is structural: anything with
    these attributes and methods works).

        id      "claude_code", "generic_mcp", ...
        tier    one of TIERS
        provider  default model provider for this host's usage records
    """

    id = ""
    tier = "observe"
    provider = "unknown"
    name = ""
    open_items = ()  # what is still [VERIFY] for this host, shown by `bp hosts`

    def capabilities(self) -> HostCapabilities:
        return HostCapabilities(tier=self.tier)

    def detect_billing(self, env, ctx=None) -> BillingContext:
        """Billing mode from presence-only signals. Phase B; `unknown` until then."""
        return BillingContext()

    def iter_usage(self, since: Cursor):
        """Yield new UsageEvents after `since`, advancing it. Must be idempotent."""
        return iter(())

    # Cursor persistence. The default keeps one row per (host, key) in
    # `usage_cursors`; adapters with an existing cursor table override these.
    def load_cursor(self, conn, key: str, path: str = "") -> Cursor:
        row = conn.execute("SELECT path, position, state FROM usage_cursors WHERE host=? AND key=?",
                           (self.id, key)).fetchone()
        if row is None:
            return Cursor(key, path)
        return Cursor(key, path or row[0] or "", int(row[1] or 0), row[2])

    def save_cursor(self, conn, cursor: Cursor) -> None:
        conn.execute(
            "INSERT INTO usage_cursors (host, key, path, position, state) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(host, key) DO UPDATE SET path=excluded.path, position=excluded.position, "
            "state=excluded.state",
            (self.id, cursor.key, cursor.path, int(cursor.position), cursor.state),
        )

    def describe(self) -> dict:
        caps = self.capabilities()
        return {"id": self.id, "name": self.name or self.id, "tier": self.tier,
                "tier_label": TIER_LABELS.get(self.tier, self.tier), "capabilities": caps._asdict(),
                "open_items": list(self.open_items)}

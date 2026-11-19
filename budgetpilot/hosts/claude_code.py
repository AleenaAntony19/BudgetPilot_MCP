"""
Claude Code host adapter (tier `enforce`).

Control comes from the hook engine (`adapters/hooks.py`); usage comes from the
session transcript, parsed by the existing `core/cc_ledger.read_turns` with its
dedupe unchanged (one record per `message.id`, keeping the largest output
count). The hooks still call `cc_ledger.ingest` directly; this adapter is the
same data behind the host-neutral interface.

Standard library only.
"""

from __future__ import annotations

import os

from budgetpilot.hosts.base import ANTHROPIC_SEMANTICS, Cursor, HostAdapter, HostCapabilities, UsageEvent


class ClaudeCodeAdapter(HostAdapter):
    id = "claude_code"
    name = "Claude Code (CLI and editor extensions)"
    tier = "enforce"
    provider = "anthropic"

    def capabilities(self) -> HostCapabilities:
        return HostCapabilities(
            tier=self.tier,
            intercept_tool_calls=True,
            rewrite_tool_input=True,      # Read and Grep, through updatedInput (docs/HOOK_CONTRACT.md)
            deny_tool_calls=True,
            ask_user=True,                # permissionDecision "ask"; a denial in headless runs
            trim_tool_output=True,        # successful Bash output only
            proxy_tools=True,
            usage_source="transcript",
            usage_exact=True,             # matched `claude -p --output-format json` exactly on 2.1.325
            hard_token_cap=False,
            limitations=(
                "Cannot cap the model's output tokens; budgets act at tool boundaries.",
                "Cannot shorten the output of a failed command.",
                "Transcript format is not a documented interface.",
            ),
        )

    def detect_billing(self, env, ctx=None):
        """Presence-only billing detection; see hosts/billing.py for each signal's source."""
        from budgetpilot.hosts.billing import detect_claude_code

        ctx = ctx or {}
        return detect_claude_code(env, cwd=ctx.get("cwd", ""), config=ctx.get("config"), auth=ctx.get("auth"))

    def iter_usage(self, since: Cursor):
        """
        New messages in the transcript at `since.path` after byte `since.position`.
        `since.state` is the current prompt id (the task id). The cursor is
        advanced once the read is complete.
        """
        from budgetpilot.core.cc_ledger import read_turns

        path = since.path
        if not path or not os.path.isfile(path):
            return
        turns, new_offset, prompt_id = read_turns(path, int(since.position or 0), since.state)
        for t in turns:
            yield UsageEvent(
                session_id=since.key, host=self.id, provider=self.provider, model=t["model"], ts=t["ts"],
                message_id=t["message_id"],
                input_tokens=t["input_tokens"], cache_write_5m=t["cache_write_5m"],
                cache_write_1h=t["cache_write_1h"], cache_read=t["cache_read"],
                output_tokens=t["output_tokens"], reasoning_tokens=t["thinking_tokens"],
                semantics=ANTHROPIC_SEMANTICS, source="transcript",
                raw_ref=f"{os.path.basename(path)}@{int(since.position or 0)}",
                task_id=t["task_id"], extra={"effort": t["effort"], "tools_called": t["tools_called"]},
            )
        since.position, since.state = new_offset, prompt_id

    # The hooks already keep a per-session cursor in `transcript_cursor`; share it
    # so the adapter and the hooks never read the same bytes twice.
    def load_cursor(self, conn, key: str, path: str = "") -> Cursor:
        row = conn.execute("SELECT byte_offset, prompt_id FROM transcript_cursor WHERE session_id=?",
                           (key,)).fetchone()
        if row is None:
            return Cursor(key, path)
        return Cursor(key, path, int(row[0] or 0), row[1])

    def save_cursor(self, conn, cursor: Cursor) -> None:
        conn.execute(
            "INSERT INTO transcript_cursor (session_id, byte_offset, prompt_id) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET byte_offset=excluded.byte_offset, prompt_id=excluded.prompt_id",
            (cursor.key, int(cursor.position), cursor.state),
        )

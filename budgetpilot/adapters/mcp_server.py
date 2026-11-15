"""
BudgetPilot MCP server (MCP 2.x, stdio).

A small tool surface. Tool descriptions are sent to the model on every turn,
so each is kept to one short line, and results are compact JSON.

Control tools:
    bp_status            mode, budget dial, recent token use, current task
    bp_mode              on | off | observe (same code path as the bp_mode_* commands)
    bp_set_budget        tight | balanced | loose
    bp_dashboard         start or reuse the local dashboard, return its URL
    bp_request_scope     raise the current task's scope by one level
    bp_recovery_options  the pending recovery decision, if any
    bp_recovery_choose   apply the human's recovery decision (3E loop)
    bp_report            cost and execution report

Gate-enforced proxy tools, for hosts that do not run Claude Code hooks:
    bp_list_dir, bp_search, bp_read_file, bp_edit_file, bp_run_tests
"""

from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from budgetpilot.agent.tools import execute_tool, get_repo_root, set_repo_root
from budgetpilot.config import ExecutionMode, get_bp_home, load_config, save_config
from budgetpilot.core.gate import check as gate_check
from budgetpilot.core.ledger import CostLedger, record_tool_event
from budgetpilot.core.router import CrcLambda
from budgetpilot.core.store import get_connection, init_db, log_fallback_error, safe_execute_write
from budgetpilot.core.tracker import TaskTracker
from budgetpilot.core.types import (
    BpEvent,
    GateVerdict,
    GateVerdictStatus,
    normalize_tool_call,
)

logger = logging.getLogger(__name__)

# Trackers keyed by session_id to maintain state across proxy tool calls
_ACTIVE_TRACKERS: Dict[str, TaskTracker] = {}


def get_or_create_tracker(session_id: str, repo_root: Optional[Union[Path, str]] = None) -> TaskTracker:
    """Retrieve or initialize an in-memory TaskTracker for an MCP session."""
    if session_id not in _ACTIVE_TRACKERS:
        root = Path(repo_root).expanduser().resolve() if repo_root else get_repo_root()
        _ACTIVE_TRACKERS[session_id] = TaskTracker(session_id=session_id, repo_root=root)
    return _ACTIVE_TRACKERS[session_id]


def _compact(value: Any) -> Any:
    """Drop empty fields: tool output is input tokens on the next turn."""
    if isinstance(value, dict):
        cleaned = {k: _compact(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


def _heartbeat() -> None:
    """Record that the MCP server is alive (shown in the dashboard). Never raises."""
    try:
        from budgetpilot.core import hookdb

        conn = hookdb.open_ready()
        if conn is not None:
            try:
                hookdb.heartbeat(conn, "mcp", "mcp", {"repo": str(get_repo_root()), "pid": os.getpid()}, min_interval=0)
            finally:
                conn.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("MCP heartbeat failed: %s", exc)


def _format_tool_result(data: Dict[str, Any], is_error: bool = False) -> CallToolResult:
    """Compact JSON text plus the same data as structured content."""
    _heartbeat()
    data = _compact(data)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, separators=(",", ":"), ensure_ascii=False))],
        structured_content=data,
        is_error=is_error,
    )


def create_mcp_server(name: str = "BudgetPilot") -> MCPServer:
    """
    Factory creating a configured MCPServer instance with all control
    and proxy tools registered.
    """
    server = MCPServer(name)

    # -----------------------------------------------------------------------
    # CONTROL TOOLS
    # -----------------------------------------------------------------------

    @server.tool()
    def bp_status() -> CallToolResult:
        """Show BudgetPilot mode, budget dial, token use in the last 24 hours and the current task."""
        from budgetpilot.core import commands

        return _format_tool_result(commands.status_data())

    @server.tool()
    def bp_mode(mode: str) -> CallToolResult:
        """Turn BudgetPilot on, off, or to observe (measure only). Applies to all projects."""
        from budgetpilot.core import commands, state

        message = commands.set_mode_text(mode)
        return _format_tool_result({"message": message, "mode": state.get_mode()})

    @server.tool()
    def bp_set_budget(budget_slider: str) -> CallToolResult:
        """Set the budget dial: tight, balanced or loose."""
        from budgetpilot.core import commands, state

        message = commands.set_budget_text(budget_slider)
        return _format_tool_result({"message": message, "budget": state.get_budget_slider()})

    @server.tool()
    def bp_dashboard(port: int = 8765, open_browser: bool = True) -> CallToolResult:
        """Start or reuse the local BudgetPilot dashboard and return its URL."""
        try:
            from budgetpilot.dashboard import ensure_running

            return _format_tool_result({"url": ensure_running(port=port, open_browser=open_browser)})
        except Exception as exc:
            return _format_tool_result({"error": f"{type(exc).__name__}: {str(exc)[:160]}"}, is_error=True)

    @server.tool()
    def bp_request_scope(reason: str, target_scope: Optional[int] = None,
                         session_id: Optional[str] = None) -> CallToolResult:
        """Raise the current task's scope by one level (or to target_scope), with a reason."""
        from budgetpilot.core import hookdb
        from budgetpilot.core.recovery_flow import expand_task_scope
        from budgetpilot.core.scope import clamp_scope, get_limits

        active_sess = session_id or "default_mcp_session"
        tracker = get_or_create_tracker(active_sess)
        old_scope = tracker.state.current_scope

        # A task tracked by the Claude Code hooks takes precedence over the proxy-tool tracker.
        conn = hookdb.open_ready()
        try:
            task = hookdb.latest_task(conn) if conn is not None else None
            if task is not None and task.get("status") == "active":
                with hookdb.TaskTxn(conn, task["task_id"]) as txn:
                    if txn.task is not None:
                        old_scope = int(txn.task["scope"])
                        wanted = clamp_scope(target_scope) if target_scope is not None else min(old_scope + 1, 3)
                        while int(txn.task["scope"]) < wanted:
                            expand_task_scope(txn.task, str(txn.task.get("budget_slider") or "balanced"))
        finally:
            if conn is not None:
                conn.close()

        new_scope = clamp_scope(target_scope) if target_scope is not None else min(old_scope + 1, 3)
        tracker.state.current_scope = new_scope
        safe_execute_write(
            conn_or_path=None,
            sql="INSERT INTO task_events (session_id, event_type, payload) VALUES (?, 'request_scope', ?);",
            params=(active_sess, json.dumps({"reason": reason, "old_scope": old_scope, "granted_scope": new_scope})),
        )
        return _format_tool_result({
            "approved": True,
            "previous_scope": old_scope,
            "granted_scope": new_scope,
            "max_read_files": get_limits(new_scope).max_files_outside_named,
        })

    @server.tool()
    def bp_recovery_options() -> CallToolResult:
        """Show the pending recovery decision after a failed test run: signature, scores, recommended action."""
        from budgetpilot.core import hookdb, recovery_flow

        conn = hookdb.open_ready()
        if conn is None:
            return _format_tool_result({"pending": False})
        try:
            return _format_tool_result(recovery_flow.pending_options(conn))
        finally:
            conn.close()

    @server.tool()
    def bp_recovery_choose(action: str, note: str = "") -> CallToolResult:
        """Apply the user's recovery choice: estimate, execute, expand, escalate, retry, deny or mode_off."""
        from budgetpilot.core import hookdb, recovery_flow

        conn = hookdb.open_ready()
        if conn is None:
            return _format_tool_result({"ok": False, "error": "database unavailable"}, is_error=True)
        try:
            result = recovery_flow.choose(conn, action, note)
        finally:
            conn.close()
        return _format_tool_result(result, is_error=not result.get("ok", False))

    @server.tool()
    def bp_report(
        session_id: Optional[str] = None,
        format_type: str = "json",
    ) -> CallToolResult:
        """Cost and execution report. format_type: json, terminal, markdown or html."""
        from budgetpilot.cli import run_report
        import io
        import sys

        fmt = (format_type or "json").lower()
        if fmt not in ("json", "terminal", "markdown", "md", "html"):
            fmt = "json"

        # Capture output from run_report
        old_stdout = sys.stdout
        sys.stdout = buffer = io.StringIO()
        try:
            run_report(session_id=session_id, format_type=fmt)
            output_text = buffer.getvalue()
        finally:
            sys.stdout = old_stdout

        if fmt == "json":
            try:
                data = json.loads(output_text)
            except Exception:
                data = {"raw_report": output_text}
            return CallToolResult(
                content=[TextContent(type="text", text=output_text)],
                structured_content=data,
            )
        else:
            return _format_tool_result({"format": fmt, "report": output_text})

    # -----------------------------------------------------------------------
    # PROXY TOOLS (Gate-Enforced Repository Operations)
    # -----------------------------------------------------------------------

    def _proxy_execute(
        canonical_name: str,
        arguments: Dict[str, Any],
        session_id: Optional[str] = None,
        repo_root: Optional[str] = None,
    ) -> CallToolResult:
        """
        Core proxy interception pipeline:
        1. Normalizes tool call to BpEvent.
        2. Applies ExecuteGate check according to active mode.
        3. If DENIED: returns block response immediately.
        4. If REWRITTEN: applies rewritten arguments.
        5. Executes sandboxed tool.
        6. Records tool audit telemetry.
        """
        sess_id = session_id or os.environ.get("BP_SESSION_ID") or "mcp_proxy_session"
        root = Path(repo_root).expanduser().resolve() if repo_root else get_repo_root()
        init_db()

        cfg = load_config()
        mode = cfg.mode.value

        tracker = get_or_create_tracker(sess_id, repo_root=root)

        # 1. Normalize tool call
        event = normalize_tool_call(
            tool_call={"name": canonical_name, "arguments": arguments},
            session_id=sess_id,
        )

        # 2. Gate check (if mode == 'enforce')
        verdict: GateVerdict = GateVerdict.allow(reason="Gate inactive")
        if mode == "enforce":
            if canonical_name == "edit_file":
                scope = tracker.state.current_scope
                target_path = tracker._normalize_path(arguments.get("path", ""))
                if scope == 1 and len(tracker.state.touched_set) >= 1 and target_path not in tracker.state.touched_set:
                    verdict = GateVerdict.deny(
                        reason="Scope 1 permits editing at most 1 file. Request scope elevation via bp_request_scope to edit additional files."
                    )
                else:
                    try:
                        verdict = gate_check(event, tracker.state)
                    except Exception as g_exc:
                        logger.error("Gate check error; failing open: %s", g_exc)
                        log_fallback_error(g_exc, "mcp_gate_check")
                        verdict = GateVerdict.allow(reason="Gate error fallback: allow")
            else:
                try:
                    verdict = gate_check(event, tracker.state)
                except Exception as g_exc:
                    logger.error("Gate check error; failing open: %s", g_exc)
                    log_fallback_error(g_exc, "mcp_gate_check")
                    verdict = GateVerdict.allow(reason="Gate error fallback: allow")

            # Persist gate decision
            safe_execute_write(
                conn_or_path=None,
                sql="""
                INSERT INTO gate_decisions (session_id, tool_name, verdict, reason, original_call)
                VALUES (?, ?, ?, ?, ?);
                """,
                params=(
                    sess_id,
                    canonical_name,
                    verdict.status.value,
                    verdict.reason,
                    json.dumps(arguments),
                ),
            )

        # 3. Interception outcome
        if verdict.status == GateVerdictStatus.DENY and mode == "enforce":
            block_msg = f"[BLOCKED BY BUDGETPILOT GATE]: {verdict.reason}"
            tracker.record_tool_call(canonical_name, arguments)
            record_tool_event(
                session_id=sess_id,
                tool_name=canonical_name,
                arguments=arguments,
                result=block_msg,
                duration=0.001,
                is_error=True,
            )
            return _format_tool_result({
                "success": False,
                "blocked": True,
                "verdict": "deny",
                "gate_reason": verdict.reason,
                "output": block_msg,
                "metadata": {"blocked": True, "verdict": "deny"},
            }, is_error=False)

        # 4. Resolve arguments (use rewritten arguments if gate rewrote them)
        effective_args = dict(arguments)
        if verdict.status == GateVerdictStatus.REWRITE and verdict.rewritten_call:
            effective_args = dict(verdict.rewritten_call.arguments)

        # 5. Execute sandboxed tool
        t_start = time.time()
        output, success, meta = execute_tool(canonical_name, effective_args, repo_root=root)
        duration = time.time() - t_start

        # 6. Verifier hook for test execution
        verification_details: Optional[Dict[str, Any]] = None
        if canonical_name == "run_tests":
            try:
                from budgetpilot.core.verifier import parse_and_classify_test_output
                v_verdict, v_sig, v_err = parse_and_classify_test_output(
                    stdout=output,
                    stderr="",
                    exit_code=0 if success else 1,
                    session_id=sess_id,
                    test_command=str(effective_args.get("command", "pytest")),
                    repo_root=root,
                )
                verification_details = {
                    "passed": v_verdict.passed,
                    "failure_class": v_verdict.failure_class.value if v_verdict.failure_class else None,
                    "signature_hash": v_sig,
                    "failing_tests": v_verdict.failing_test_ids,
                }
            except Exception as v_exc:
                logger.debug("Verification classification error in proxy: %s", v_exc)

        # 7. Record tracking and telemetry
        tracker.record_tool_call(canonical_name, effective_args)
        record_tool_event(
            session_id=sess_id,
            tool_name=canonical_name,
            arguments=effective_args,
            result=output[:2000],
            duration=duration,
            is_error=not success,
        )

        return _format_tool_result({
            "success": success,
            "blocked": False,
            "verdict": verdict.status.value,
            "gate_reason": verdict.reason,
            "output": output,
            "effective_arguments": effective_args,
            "verification": verification_details,
            "metadata": meta,
        }, is_error=not success)

    @server.tool()
    def bp_list_dir(
        path: str = ".",
        depth: int = 1,
        session_id: Optional[str] = None,
        repo_root: Optional[str] = None,
    ) -> CallToolResult:
        """List a directory, with depth limited by the task scope."""
        return _proxy_execute("list_dir", {"path": path, "depth": depth}, session_id, repo_root)

    @server.tool()
    def bp_search(
        pattern: str,
        path: str = ".",
        session_id: Optional[str] = None,
        repo_root: Optional[str] = None,
    ) -> CallToolResult:
        """Search the repository with ripgrep, limited by the task scope."""
        return _proxy_execute("search", {"pattern": pattern, "path": path}, session_id, repo_root)

    @server.tool()
    def bp_read_file(
        path: str,
        start: int = 1,
        end: Optional[int] = None,
        session_id: Optional[str] = None,
        repo_root: Optional[str] = None,
    ) -> CallToolResult:
        """Read a file by line range; large reads are narrowed to the scope window."""
        args = {"path": path, "start": start}
        if end is not None:
            args["end"] = end
        return _proxy_execute("read_file", args, session_id, repo_root)

    @server.tool()
    def bp_edit_file(
        path: str,
        old: str,
        new: str,
        session_id: Optional[str] = None,
        repo_root: Optional[str] = None,
    ) -> CallToolResult:
        """Replace an exact string in a file."""
        return _proxy_execute("edit_file", {"path": path, "old": old, "new": new}, session_id, repo_root)

    @server.tool()
    def bp_run_tests(
        command: Optional[str] = None,
        target: Optional[str] = None,
        timeout: int = 60,
        session_id: Optional[str] = None,
        repo_root: Optional[str] = None,
    ) -> CallToolResult:
        """Run tests and classify any failure."""
        args = {"timeout": timeout}
        if command is not None:
            args["command"] = command
        if target is not None:
            args["target"] = target
        return _proxy_execute("run_tests", args, session_id, repo_root)

    return server


# Default singleton instance for direct execution / import
mcp = create_mcp_server()


def _parse_server_args(argv: Optional[List[str]] = None) -> Tuple[str, Optional[str]]:
    """Parse `--repo PATH` and `--transport stdio`. Unknown arguments are ignored."""
    import argparse

    parser = argparse.ArgumentParser(prog="budgetpilot-mcp", description="BudgetPilot MCP server")
    parser.add_argument("--repo", default=None, help="Repository root (default: $BP_REPO_ROOT, then cwd)")
    parser.add_argument("--transport", default="stdio", choices=["stdio"], help="MCP transport")
    args, _unknown = parser.parse_known_args(argv)
    return args.transport, args.repo


def run_server(
    transport: Optional[str] = None,
    repo: Optional[Union[Path, str]] = None,
    argv: Optional[List[str]] = None,
) -> None:
    """
    Launch the BudgetPilot MCP server over stdio.

    Called with no arguments (console script / `python -m`), it parses
    `--repo PATH` and `--transport stdio` from the command line. The repository
    root falls back to $BP_REPO_ROOT and then to the current directory.
    """
    import asyncio
    import sys

    if transport is None and repo is None:
        transport, repo = _parse_server_args(argv)
    transport = transport or "stdio"
    if repo:
        set_repo_root(repo)
    print(
        f"Starting BudgetPilot MCP Server (transport={transport}, repo={get_repo_root()})...",
        file=sys.stderr,
    )
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    run_server()

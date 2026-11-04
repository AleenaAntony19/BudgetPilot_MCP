"""
BudgetPilot Model Context Protocol (MCP) 2.x Server Adapter.
Phase M: Exposes BudgetPilot control tools and gate-enforced proxy tools
for MCP-compliant agent hosts (Antigravity, Claude Code, Cursor, OpenCode).

Uses MCP 2.x API (`from mcp.server import MCPServer`).

Exposed Tools:
1. Control Tools:
   - `bp_status`: Show current runtime mode, budget slider, CRC calibration, and spend.
   - `bp_set_budget`: Dynamically adjust runtime budget slider (tight, balanced, loose) or mode.
   - `bp_request_scope`: Explicitly request scope elevation with justification.
   - `bp_escalate`: Launch bounded strong-tier sub-run inside an isolated git worktree.
   - `bp_report`: Retrieve financial and execution telemetry report (json/markdown/terminal/html).

2. Proxy Tools (Gate-Enforced):
   - `bp_list_dir`: Sandboxed directory listing with depth limiting.
   - `bp_search`: Ripgrep code search with pattern confinement.
   - `bp_read_file`: Line-bounded reading with oversized chunk rewriting.
   - `bp_edit_file`: Exact string replacement with scope policy checks.
   - `bp_run_tests`: Test execution with automated failure classification.
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


def _format_tool_result(data: Dict[str, Any], is_error: bool = False) -> CallToolResult:
    """Format dictionary result into an MCP 2.x CallToolResult preserving structured data."""
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, indent=2))],
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
    def bp_status(session_id: Optional[str] = None) -> CallToolResult:
        """
        Query current BudgetPilot system status:
        - Active execution mode (enforce, observe, off)
        - Active budget slider (tight, balanced, loose)
        - Conformal Risk Control (CRC) calibration guarantees
        - Historical spend, modeled avoided spend, and savings ratio
        - Current session state (if session_id is provided)
        """
        init_db()
        cfg = load_config()
        bp_home = str(get_bp_home())

        # Calibration Guarantees
        calibration_guarantees: Dict[str, Any] = {}
        for level in ("tight", "balanced", "loose"):
            lam, source = CrcLambda.get(level)
            target_alpha = {"tight": 0.30, "balanced": 0.20, "loose": 0.10}[level]
            calibration_guarantees[level] = {
                "budget_level": level,
                "lambda": round(lam, 4),
                "target_alpha": target_alpha,
                "source": source,
                "calibrated": (source != "uncalibrated"),
            }

        # Historical financial metrics
        total_sessions = 0
        total_measured_spend = 0.0
        total_avoided_mid = 0.0
        gate_counts = {"allow": 0, "rewrite": 0, "deny": 0, "override": 0}

        conn = get_connection()
        try:
            s_row = conn.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(total_cost), 0.0) as cost FROM sessions;").fetchone()
            if s_row:
                total_sessions = int(s_row["cnt"])
                total_measured_spend = float(s_row["cost"])

            mc_row = conn.execute("SELECT COALESCE(SUM(estimated_cost), 0.0) as cost FROM model_calls;").fetchone()
            if mc_row and mc_row["cost"] > 0:
                total_measured_spend = max(total_measured_spend, float(mc_row["cost"]))

            av_rows = conn.execute("SELECT saved_cost_usd, metadata FROM avoided_estimates;").fetchall()
            for r in av_rows:
                saved = float(r["saved_cost_usd"] or 0.0)
                total_avoided_mid += saved
                meta_json = r["metadata"]
                if meta_json:
                    try:
                        m = json.loads(meta_json)
                        gate_counts["override"] += int(m.get("override_count", 0))
                    except Exception:
                        pass

            gd_rows = conn.execute("SELECT verdict, COUNT(*) as cnt FROM gate_decisions GROUP BY verdict;").fetchall()
            for r in gd_rows:
                v = str(r["verdict"]).lower()
                if v in gate_counts:
                    gate_counts[v] += int(r["cnt"])

        except Exception as exc:
            logger.debug("Failed querying status financial details: %s", exc)
        finally:
            conn.close()

        baseline_spend = total_measured_spend + total_avoided_mid
        savings_ratio = (total_avoided_mid / baseline_spend * 100.0) if baseline_spend > 0 else 0.0

        session_info: Optional[Dict[str, Any]] = None
        if session_id:
            tracker = _ACTIVE_TRACKERS.get(session_id)
            if tracker:
                session_info = {
                    "session_id": session_id,
                    "task_id": tracker.state.task_id,
                    "current_scope": tracker.state.current_scope,
                    "read_files_count": len(tracker.state.read_files),
                    "touched_files": list(tracker.state.touched_files),
                    "denial_count": tracker.state.denial_count,
                    "override_active": tracker.state.override_active,
                }

        ret = {
            "bp_home": bp_home,
            "active_mode": cfg.mode.value,
            "active_budget_slider": cfg.budget_slider,
            "calibration_guarantees": calibration_guarantees,
            "financials": {
                "total_sessions": total_sessions,
                "measured_spend_usd": round(total_measured_spend, 6),
                "modeled_avoided_usd": round(total_avoided_mid, 6),
                "baseline_equivalent_usd": round(baseline_spend, 6),
                "savings_ratio_pct": round(savings_ratio, 2),
            },
            "gate_counts": gate_counts,
            "session": session_info,
        }
        return _format_tool_result(ret)

    @server.tool()
    def bp_set_budget(
        budget_slider: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> CallToolResult:
        """
        Dynamically adjust BudgetPilot operational parameters:
        - `budget_slider`: 'tight' (conservative), 'balanced' (standard), 'loose' (lenient)
        - `mode`: 'enforce' (active gate policy), 'observe' (passive telemetry), 'off' (disabled)
        """
        cfg = load_config()
        changed = {}

        if budget_slider:
            slider_clean = str(budget_slider).strip().lower()
            if slider_clean not in ("tight", "balanced", "loose"):
                return _format_tool_result({
                    "success": False,
                    "error": f"Invalid budget slider '{budget_slider}'. Must be one of: tight, balanced, loose.",
                }, is_error=False)
            cfg.budget_slider = slider_clean
            changed["budget_slider"] = slider_clean

        if mode:
            mode_clean = str(mode).strip().lower()
            try:
                cfg.mode = ExecutionMode(mode_clean)
                changed["mode"] = cfg.mode.value
            except ValueError:
                return _format_tool_result({
                    "success": False,
                    "error": f"Invalid mode '{mode}'. Must be one of: enforce, observe, off.",
                }, is_error=False)

        if changed:
            save_config(cfg)

        return _format_tool_result({
            "success": True,
            "updated": changed,
            "active_mode": cfg.mode.value,
            "active_budget_slider": cfg.budget_slider,
        })

    @server.tool()
    def bp_request_scope(
        reason: str,
        target_scope: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> CallToolResult:
        """
        Explicitly request scope elevation (+1 level or target scope) when a task requires
        broader repository edits or reading additional files.
        """
        active_sess = session_id or "default_mcp_session"
        tracker = get_or_create_tracker(active_sess)

        old_scope = tracker.state.current_scope
        if target_scope is not None:
            new_scope = max(1, min(int(target_scope), 3))
        else:
            new_scope = min(old_scope + 1, 3)

        tracker.state.current_scope = new_scope

        # Record task event
        safe_execute_write(
            conn_or_path=None,
            sql="""
            INSERT INTO task_events (session_id, event_type, payload)
            VALUES (?, 'request_scope', ?);
            """,
            params=(active_sess, json.dumps({
                "reason": reason,
                "old_scope": old_scope,
                "granted_scope": new_scope,
            })),
        )

        return _format_tool_result({
            "approved": True,
            "previous_scope": old_scope,
            "granted_scope": new_scope,
            "max_read_files": {1: 5, 2: 15, 3: 50}.get(new_scope, 15),
            "reason": reason,
        })

    @server.tool()
    def bp_escalate(
        task_prompt: str,
        session_id: Optional[str] = None,
        repo_path: Optional[str] = None,
        max_turns: int = 5,
        cost_cap_usd: float = 0.50,
    ) -> CallToolResult:
        """
        Launch a bounded recovery sub-run on the 'strong' model tier inside an
        isolated git worktree, enforcing USD and turn caps, and returning a unified diff.
        """
        from budgetpilot.agent.escalation import run_escalation
        from budgetpilot.core.types import FailureClass, RecoveryContext

        active_sess = session_id or f"escalate_{uuid.uuid4().hex[:8]}"
        root = Path(repo_path).expanduser().resolve() if repo_path else get_repo_root()

        ctx = RecoveryContext(
            failure_class=FailureClass.UNKNOWN,
            error_message=task_prompt,
            prompt=task_prompt,
            session_id=active_sess,
        )

        result = run_escalation(
            prompt=task_prompt,
            repo_path=root,
            ctx=ctx,
            max_turns=max_turns,
            cost_cap_usd=cost_cap_usd,
            session_id=active_sess,
        )

        return _format_tool_result({
            "success": getattr(result, "success", getattr(result, "succeeded", False)),
            "patch": getattr(result, "patch", ""),
            "cost_usd": getattr(result, "cost_usd", 0.0),
            "tokens_used": getattr(result, "tokens_used", getattr(result, "total_tokens", 0)),
            "wall_time": getattr(result, "wall_time", 0.0),
            "turns_used": getattr(result, "turns_used", getattr(result, "turns", 0)),
            "error": getattr(result, "error", None),
        })

    @server.tool()
    def bp_report(
        session_id: Optional[str] = None,
        format_type: str = "json",
    ) -> CallToolResult:
        """
        Generate a BudgetPilot financial and execution report.
        Formats: 'json', 'terminal', 'markdown', 'html'.
        """
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
        """
        List directory contents with depth and permission sandboxing.
        Protected by BudgetPilot execute gate against excessive exploration.
        """
        return _proxy_execute("list_dir", {"path": path, "depth": depth}, session_id, repo_root)

    @server.tool()
    def bp_search(
        pattern: str,
        path: str = ".",
        session_id: Optional[str] = None,
        repo_root: Optional[str] = None,
    ) -> CallToolResult:
        """
        Search repository code using ripgrep with pattern sandboxing.
        """
        return _proxy_execute("search", {"pattern": pattern, "path": path}, session_id, repo_root)

    @server.tool()
    def bp_read_file(
        path: str,
        start: int = 1,
        end: Optional[int] = None,
        session_id: Optional[str] = None,
        repo_root: Optional[str] = None,
    ) -> CallToolResult:
        """
        Read file contents with line limits.
        Large or unbounded reads are automatically rewritten into bounded windows.
        """
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
        """
        Perform exact string replacement in a file.
        Enforces scope limits on touched files and single-file policies.
        """
        return _proxy_execute("edit_file", {"path": path, "old": old, "new": new}, session_id, repo_root)

    @server.tool()
    def bp_run_tests(
        command: Optional[str] = None,
        target: Optional[str] = None,
        timeout: int = 60,
        session_id: Optional[str] = None,
        repo_root: Optional[str] = None,
    ) -> CallToolResult:
        """
        Execute repository tests in a sandboxed subprocess.
        Automatically classifies failures into 7 diagnostic classes.
        """
        args = {"timeout": timeout}
        if command is not None:
            args["command"] = command
        if target is not None:
            args["target"] = target
        return _proxy_execute("run_tests", args, session_id, repo_root)

    return server


# Default singleton instance for direct execution / import
mcp = create_mcp_server()


def run_server(transport: str = "stdio") -> None:
    """
    Launch the BudgetPilot MCP server over stdio or specified transport.
    """
    import asyncio
    print(f"Starting BudgetPilot MCP Server (MCP 2.x, transport={transport})...", file=os.sys.stderr)
    if transport == "stdio":
        asyncio.run(mcp.run_stdio_async())
    else:
        mcp.run()


if __name__ == "__main__":
    run_server()

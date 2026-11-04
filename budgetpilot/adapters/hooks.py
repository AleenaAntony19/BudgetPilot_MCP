"""
BudgetPilot Ultra-Low Latency Hooks Adapter.
Phase N: Standard-library-only shim for Claude Code and external agent tool interception.

Guarantees:
- Ultra-low latency: Target <60ms execution overhead per interception.
- Pure Standard Library: Zero third-party dependencies (no ollama, no mcp, no fastapi).
- Canonical Mapping: Translates external tool payloads (Claude Code, OpenCode) into core `BpEvent`.
- Task Lifecycle Triggers: Coordinates `TaskStart`, `PreToolUse`, `PostToolUse`, and `TaskEnd`.
"""

import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid

from budgetpilot.core.types import BpEvent

# Mapping of Claude Code tool names to BudgetPilot canonical names
CLAUDE_TOOL_MAP: Dict[str, str] = {
    # File Reading
    "view": "read_file",
    "read": "read_file",
    "readfile": "read_file",
    "read_file": "read_file",
    # File Editing
    "edit": "edit_file",
    "fileedit": "edit_file",
    "file_edit": "edit_file",
    "multiedit": "edit_file",
    "write": "edit_file",
    "create": "edit_file",
    # Directory Listing
    "ls": "list_dir",
    "listdir": "list_dir",
    "list_dir": "list_dir",
    "directorylist": "list_dir",
    # Search
    "grep": "search",
    "glob": "search",
    "find": "search",
    "search": "search",
    # Command Execution
    "bash": "bash",
    "terminal": "bash",
    "executecommand": "bash",
}


def get_fast_bp_home() -> Path:
    """Retrieve BP_HOME path using only standard library."""
    custom = os.environ.get("BP_HOME")
    if custom:
        return Path(custom).expanduser().resolve()
    return Path.home() / ".budgetpilot"


def get_fast_mode() -> str:
    """Fast lookup of runtime execution mode from env or config file (<1ms)."""
    env_mode = os.environ.get("BP_MODE")
    if env_mode:
        m = env_mode.strip().lower()
        if m in ("enforce", "observe", "off"):
            return m

    cfg_file = get_fast_bp_home() / "config.json"
    if cfg_file.exists():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return str(data.get("mode", "enforce")).strip().lower()
        except Exception:
            pass
    return "enforce"


def get_fast_budget_slider() -> str:
    """Fast lookup of active budget slider from env or config file (<1ms)."""
    env_slider = os.environ.get("BP_BUDGET_SLIDER")
    if env_slider:
        s = env_slider.strip().lower()
        if s in ("tight", "balanced", "loose"):
            return s

    cfg_file = get_fast_bp_home() / "config.json"
    if cfg_file.exists():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return str(data.get("budget_slider", "balanced")).strip().lower()
        except Exception:
            pass
    return "balanced"


def map_claude_tool_to_canonical(
    tool_name: str,
    parameters: Dict[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    """
    Map Claude Code / external tool name and parameter schema to canonical
    BudgetPilot tool format:
    - View/ReadFile -> read_file(path, start, end)
    - Edit/FileEdit -> edit_file(path, old, new)
    - LS/ListDir -> list_dir(path, depth)
    - Grep/Glob/Find -> search(pattern, path)
    - Bash -> run_tests (if pytest/test runner) or bash
    """
    raw_name = str(tool_name).strip().lower()
    canonical = CLAUDE_TOOL_MAP.get(raw_name, raw_name)
    canon_args: Dict[str, Any] = {}

    params = parameters or {}

    if canonical == "read_file":
        path = params.get("file_path") or params.get("path") or params.get("target") or ""
        canon_args["path"] = str(path)

        view_range = params.get("view_range")
        if isinstance(view_range, (list, tuple)) and len(view_range) >= 2:
            canon_args["start"] = int(view_range[0])
            canon_args["end"] = int(view_range[1])
        else:
            if "start" in params or "offset" in params:
                canon_args["start"] = int(params.get("start") or params.get("offset") or 1)
            if "end" in params or "limit" in params:
                limit = params.get("end") or params.get("limit")
                if limit is not None:
                    canon_args["end"] = int(limit)

    elif canonical == "edit_file":
        path = params.get("file_path") or params.get("path") or ""
        canon_args["path"] = str(path)
        canon_args["old"] = str(params.get("old_string") or params.get("old") or "")
        canon_args["new"] = str(params.get("new_string") or params.get("new") or "")

    elif canonical == "list_dir":
        path = params.get("path") or params.get("dir_path") or "."
        canon_args["path"] = str(path)
        canon_args["depth"] = int(params.get("depth") or 1)

    elif canonical == "search":
        pattern = params.get("pattern") or params.get("query") or params.get("regex") or ""
        path = params.get("path") or "."
        canon_args["pattern"] = str(pattern)
        canon_args["path"] = str(path)

    elif canonical == "bash":
        cmd = str(params.get("command") or params.get("cmd") or "").strip()
        canon_args["command"] = cmd
        # Detect test commands and map to run_tests
        lower_cmd = cmd.lower()
        if any(runner in lower_cmd for runner in ("pytest", "python -m unittest", "npm test", "cargo test", "go test")):
            canonical = "run_tests"

    else:
        canon_args = dict(params)

    return canonical, canon_args


def payload_to_bp_event(
    payload: Dict[str, Any],
    session_id: Optional[str] = None,
) -> BpEvent:
    """
    Parse an incoming tool hook payload from Claude Code or external agent
    into a standardized core BpEvent dataclass.
    """
    event_type = payload.get("event") or payload.get("type") or "PreToolUse"
    raw_tool = payload.get("tool") or payload.get("tool_name") or payload.get("name") or "unknown"
    params = payload.get("parameters") or payload.get("arguments") or payload.get("args") or {}

    sess_id = (
        session_id
        or payload.get("session_id")
        or payload.get("conversation_id")
        or os.environ.get("BP_SESSION_ID")
        or "claude_hook_session"
    )

    canonical_name, canonical_args = map_claude_tool_to_canonical(raw_tool, params)

    return BpEvent(
        event_id=payload.get("id") or str(uuid.uuid4()),
        timestamp=float(payload.get("timestamp") or time.time()),
        event_type=canonical_name,
        session_id=sess_id,
        payload={
            "tool_name": canonical_name,
            "original_tool": raw_tool,
            "arguments": canonical_args,
            "hook_event": event_type,
        },
        metadata={
            "source": "claude_code_hook",
            "raw_parameters": params,
        },
    )


def fast_log_sqlite(
    db_path: Optional[Union[Path, str]],
    sql: str,
    params: Tuple[Any, ...],
) -> bool:
    """
    Standard-library-only fast SQLite write with short timeout (<5ms).
    Fails open gracefully without throwing exceptions.
    """
    path = Path(db_path) if db_path else get_fast_bp_home() / "bp.db"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=0.05)
        try:
            conn.execute(sql, params)
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def handle_lifecycle_trigger(
    event_name: str,
    payload: Dict[str, Any],
    db_path: Optional[Union[Path, str]] = None,
) -> Dict[str, Any]:
    """
    Handle task lifecycle triggers from external agent hosts:
    - TaskStart / SessionStart: Initializes session tracking
    - TaskEnd / SessionEnd: Finalizes session state and records outcomes
    """
    norm_event = str(event_name).strip().lower()
    sess_id = payload.get("session_id") or os.environ.get("BP_SESSION_ID") or "claude_session"
    mode = get_fast_mode()
    slider = get_fast_budget_slider()

    if norm_event in ("taskstart", "sessionstart", "start"):
        fast_log_sqlite(
            db_path,
            """
            INSERT OR IGNORE INTO sessions (session_id, mode, budget_slider, status)
            VALUES (?, ?, ?, 'active');
            """,
            (sess_id, mode, slider),
        )
        fast_log_sqlite(
            db_path,
            """
            INSERT INTO task_events (session_id, event_type, payload)
            VALUES (?, 'task_start', ?);
            """,
            (sess_id, json.dumps(payload)),
        )
        return {
            "status": "initialized",
            "session_id": sess_id,
            "mode": mode,
            "budget_slider": slider,
        }

    elif norm_event in ("taskend", "sessionend", "end"):
        status_val = payload.get("status", "done")
        fast_log_sqlite(
            db_path,
            """
            UPDATE sessions
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE session_id = ?;
            """,
            (status_val, sess_id),
        )
        fast_log_sqlite(
            db_path,
            """
            INSERT INTO task_events (session_id, event_type, payload)
            VALUES (?, 'task_end', ?);
            """,
            (sess_id, json.dumps(payload)),
        )
        return {
            "status": "finalized",
            "session_id": sess_id,
            "verdict": status_val,
        }

    return {"status": "unhandled", "event": event_name}


def handle_hook_event(
    payload: Dict[str, Any],
    db_path: Optional[Union[Path, str]] = None,
) -> Dict[str, Any]:
    """
    Main entrypoint for ultra-low latency tool interception (<60ms target).

    Evaluates:
    - `PreToolUse`: Evaluates tool policy, returns allow/rewrite/deny verdict.
    - `PostToolUse`: Logs tool execution result and latency.
    - Lifecycle triggers: Dispatches to handle_lifecycle_trigger.
    """
    t0 = time.time()
    event_type = str(payload.get("event") or payload.get("type") or "PreToolUse")

    # 1. Lifecycle trigger interception
    if event_type in ("TaskStart", "SessionStart", "TaskEnd", "SessionEnd"):
        res = handle_lifecycle_trigger(event_type, payload, db_path=db_path)
        res["latency_ms"] = round((time.time() - t0) * 1000, 3)
        return res

    # 2. Convert payload to core BpEvent
    event = payload_to_bp_event(payload)
    canonical_tool = event.payload.get("tool_name", "unknown")
    canon_args = event.payload.get("arguments", {})
    sess_id = event.session_id

    mode = get_fast_mode()

    # 3. PostToolUse: Record telemetry and return
    if event_type == "PostToolUse":
        res_output = str(payload.get("result") or payload.get("output") or "")[:2000]
        is_error = bool(payload.get("is_error", False))
        duration = float(payload.get("duration", 0.0))

        fast_log_sqlite(
            db_path,
            """
            INSERT INTO tool_events (session_id, tool_name, arguments, result, duration, is_error)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (sess_id, canonical_tool, json.dumps(canon_args), res_output, duration, 1 if is_error else 0),
        )
        return {
            "verdict": "recorded",
            "decision": "allow",
            "tool": canonical_tool,
            "latency_ms": round((time.time() - t0) * 1000, 3),
        }

    # 4. PreToolUse: Fast Policy Decision Gate
    # Mode = off: Always allow immediately
    if mode == "off":
        return {
            "verdict": "allow",
            "decision": "allow",
            "reason": "BudgetPilot mode=off",
            "latency_ms": round((time.time() - t0) * 1000, 3),
        }

    # Mode = observe: Log and allow immediately
    if mode == "observe":
        fast_log_sqlite(
            db_path,
            """
            INSERT INTO gate_decisions (session_id, tool_name, verdict, reason, original_call)
            VALUES (?, ?, 'allow', 'Passive observation mode', ?);
            """,
            (sess_id, canonical_tool, json.dumps(canon_args)),
        )
        return {
            "verdict": "allow",
            "decision": "allow",
            "reason": "BudgetPilot mode=observe (passive)",
            "latency_ms": round((time.time() - t0) * 1000, 3),
        }

    # Mode = enforce: Ultra-fast rule evaluation
    # Rule 1: read_file line range rewrite
    if canonical_tool == "read_file":
        start = int(canon_args.get("start", 1))
        end = canon_args.get("end")

        MAX_LINES = 300
        if end is None:
            rewritten_end = start + MAX_LINES
            rewritten_args = dict(canon_args)
            rewritten_args["end"] = rewritten_end
            reason = f"Unbounded read rewritten to {MAX_LINES}-line window [{start}:{rewritten_end}]"

            fast_log_sqlite(
                db_path,
                "INSERT INTO gate_decisions (session_id, tool_name, verdict, reason, original_call) VALUES (?, ?, 'rewrite', ?, ?);",
                (sess_id, canonical_tool, reason, json.dumps(canon_args)),
            )
            return {
                "verdict": "rewrite",
                "decision": "rewrite",
                "reason": reason,
                "rewritten_parameters": rewritten_args,
                "latency_ms": round((time.time() - t0) * 1000, 3),
            }
        elif int(end) - start > MAX_LINES:
            rewritten_end = start + MAX_LINES
            rewritten_args = dict(canon_args)
            rewritten_args["end"] = rewritten_end
            reason = f"Oversized read ({int(end) - start} lines) clamped to {MAX_LINES} lines [{start}:{rewritten_end}]"

            fast_log_sqlite(
                db_path,
                "INSERT INTO gate_decisions (session_id, tool_name, verdict, reason, original_call) VALUES (?, ?, 'rewrite', ?, ?);",
                (sess_id, canonical_tool, reason, json.dumps(canon_args)),
            )
            return {
                "verdict": "rewrite",
                "decision": "rewrite",
                "reason": reason,
                "rewritten_parameters": rewritten_args,
                "latency_ms": round((time.time() - t0) * 1000, 3),
            }

    # Rule 2: list_dir depth rewrite
    elif canonical_tool == "list_dir":
        path_str = str(canon_args.get("path", "."))
        depth = int(canon_args.get("depth", 1))
        if path_str in (".", "./", "") and depth > 2:
            rewritten_args = dict(canon_args)
            rewritten_args["depth"] = 2
            reason = f"Root directory listing depth {depth} clamped to 2 to prevent token blowout"

            fast_log_sqlite(
                db_path,
                "INSERT INTO gate_decisions (session_id, tool_name, verdict, reason, original_call) VALUES (?, ?, 'rewrite', ?, ?);",
                (sess_id, canonical_tool, reason, json.dumps(canon_args)),
            )
            return {
                "verdict": "rewrite",
                "decision": "rewrite",
                "reason": reason,
                "rewritten_parameters": rewritten_args,
                "latency_ms": round((time.time() - t0) * 1000, 3),
            }

    # Rule 3: edit_file empty string protection
    elif canonical_tool == "edit_file":
        old_str = canon_args.get("old", "")
        if not old_str and old_str != "":
            pass
        elif old_str == "":
            reason = "Denying edit_file: 'old' target string cannot be empty"
            fast_log_sqlite(
                db_path,
                "INSERT INTO gate_decisions (session_id, tool_name, verdict, reason, original_call) VALUES (?, ?, 'deny', ?, ?);",
                (sess_id, canonical_tool, reason, json.dumps(canon_args)),
            )
            return {
                "verdict": "deny",
                "decision": "deny",
                "reason": reason,
                "latency_ms": round((time.time() - t0) * 1000, 3),
            }

    # Default policy: allow
    fast_log_sqlite(
        db_path,
        "INSERT INTO gate_decisions (session_id, tool_name, verdict, reason, original_call) VALUES (?, ?, 'allow', 'Policy allowed', ?);",
        (sess_id, canonical_tool, json.dumps(canon_args)),
    )

    return {
        "verdict": "allow",
        "decision": "allow",
        "reason": "Policy allowed",
        "latency_ms": round((time.time() - t0) * 1000, 3),
    }


def measure_interception_latency(
    payload: Dict[str, Any],
    iterations: int = 100,
    db_path: Optional[Union[Path, str]] = None,
) -> float:
    """
    Benchmark tool to mathematically verify the <60ms latency invariant.
    Runs N hook interceptions and returns the average latency in milliseconds.
    """
    times_ms: List[float] = []

    for _ in range(iterations):
        t_start = time.perf_counter()
        _ = handle_hook_event(payload, db_path=db_path)
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0
        times_ms.append(elapsed_ms)

    avg_ms = sum(times_ms) / len(times_ms)
    return avg_ms


def main() -> None:
    """
    CLI / Stdin Hook entrypoint for Claude Code tool interceptor.
    Reads single JSON payload from stdin, writes decision JSON to stdout.
    """
    try:
        raw_input = sys.stdin.read().strip()
        if not raw_input:
            response = {"verdict": "allow", "decision": "allow", "reason": "Empty payload"}
        else:
            payload = json.loads(raw_input)
            response = handle_hook_event(payload)
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()
    except Exception as exc:
        # Strict fail-open guarantee: never block user workflow on unhandled hook exception
        err_res = {"verdict": "allow", "decision": "allow", "reason": f"Hook fallback: {exc}"}
        sys.stdout.write(json.dumps(err_res) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

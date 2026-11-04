"""
BudgetPilot Execute Gate Subsystem.
Phase E: Policy Enforcement Matrix, execution mode routing (enforce, observe, off),
safety overrides, and fail-open resilience.

Strict Rule: This module must NOT import ollama, mcp, or fastapi.
"""

import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from budgetpilot.core.tracker import TaskState
from budgetpilot.core.types import (
    BpEvent,
    GateVerdict,
    GateVerdictStatus,
    ScopePrediction,
    ToolCall,
)

logger = logging.getLogger(__name__)

# File extension pattern for parsing named files from prompt/traceback
FILE_NAME_PATTERN = (
    r'\b(?:[a-zA-Z0-9_\-\.\/]+\/)?([a-zA-Z0-9_\-]+\.'
    r'(?:py|js|ts|jsx|tsx|json|yaml|yml|md|txt|html|css|toml|sql|sh|go|rs|c|cpp|h))\b'
)


def _make_override_key(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Generate a deterministic key identifying a tool call for override tracking."""
    filtered = {
        k: v for k, v in arguments.items()
        if k not in ("timestamp", "call_id", "session_id")
    }
    try:
        args_str = json.dumps(filtered, sort_keys=True)
    except Exception:
        args_str = str(sorted(filtered.items()))
    return f"{tool_name}:{args_str}"


def extract_named_files(state: Optional[TaskState]) -> Set[str]:
    """
    Extract file paths named directly in user prompt, traceback, or pre-configured named set.
    """
    if state is None:
        return set()

    named_files: Set[str] = set()

    # Pre-configured named set in state metadata
    explicit_named = state.metadata.get("named_set") or state.metadata.get("named_files")
    if explicit_named:
        for f in explicit_named:
            clean_f = str(f).strip()
            named_files.add(clean_f)
            named_files.add(Path(clean_f).name)

    # Prompt and traceback extraction
    prompt_text = str(state.metadata.get("prompt", ""))
    traceback_text = str(state.metadata.get("traceback", ""))
    combined_text = f"{prompt_text}\n{traceback_text}"

    if combined_text.strip():
        matches = re.findall(FILE_NAME_PATTERN, combined_text)
        for m in matches:
            clean_m = m.strip()
            named_files.add(clean_m)
            named_files.add(Path(clean_m).name)

    return named_files


def is_named_file(path: str, named_files: Set[str]) -> bool:
    """Check if a path matches any entry in the named files set."""
    if not path or not named_files:
        return False

    clean_p = str(path).strip().lower()
    base = Path(clean_p).name

    for named in named_files:
        n_clean = str(named).strip().lower()
        n_base = Path(n_clean).name

        if clean_p == n_clean or base == n_base:
            return True
        if clean_p.endswith("/" + n_clean) or n_clean.endswith("/" + clean_p):
            return True

    return False


def check(
    event: BpEvent,
    state: Optional[TaskState] = None,
    mode: Optional[str] = None,
) -> GateVerdict:
    """
    Evaluate an intercepted BpEvent against the active gate policy and task state.

    Enforcement Matrix:
    - Scope 1: Max 3 file reads outside named set, 400-line read window, depth 1 list_dir, deny full-suite tests.
    - Scope 2: Max 10 file reads, 800-line read window, package directory search/list.
    - Scope 3: Unrestricted.
    - Edits: Always allowed across all scope levels.

    Safety & Overrides:
    - Never gate the first read of a file named directly in prompt or traceback.
    - Denying a call stores an override_key. Repeating a denied call within 3 turns is allowed as an override.
    - Switch to 'observe' mode if max_denies_per_task is reached.
    """
    try:
        # 1. Fallback state initialization
        active_state = state if state is not None else TaskState(session_id="default")
        meta = active_state.metadata

        # 2. Extract tool name and arguments safely
        payload = event.payload or {}
        tool_name = (payload.get("tool_name") or event.event_type or "").strip().lower()
        raw_arguments = payload.get("arguments")
        if raw_arguments is None and isinstance(payload, dict):
            raw_arguments = {
                k: v for k, v in payload.items()
                if k not in ("tool_name", "call_id")
            }
        arguments: Dict[str, Any] = dict(raw_arguments) if isinstance(raw_arguments, dict) else {}

        # 3. Resolve execution mode (enforce, observe, off)
        env_mode = os.environ.get("BP_MODE", "").strip().lower()
        if env_mode == "off":
            return GateVerdict.allow(
                reason="Allowed by execute gate: gate is off (BP_MODE=off).",
                mode="off",
            )

        active_mode = (
            mode
            or (env_mode if env_mode else None)
            or meta.get("mode")
            or getattr(active_state, "mode", None)
            or "enforce"
        ).strip().lower()

        # Mode: OFF -> immediate passthrough
        if active_mode == "off":
            return GateVerdict.allow(
                reason="Allowed by execute gate: gate is off (mode=off).",
                mode="off",
            )

        # Check if max_denies_per_task was reached prior to this call
        max_denies = int(meta.get("max_denies_per_task", 3))
        deny_count = int(meta.get("deny_count", 0))
        if deny_count >= max_denies:
            active_mode = "observe"
            meta["mode"] = "observe"
            if hasattr(active_state, "mode"):
                active_state.mode = "observe"

        # 4. Check Override Mechanism: repeating a denied call within 3 turns
        override_key = _make_override_key(tool_name, arguments)
        current_turn = int(meta.get("turn", meta.get("current_turn", 0)))
        denied_calls: Dict[str, int] = meta.setdefault("denied_calls", {})

        if override_key in denied_calls:
            denied_turn = denied_calls[override_key]
            turn_delta = current_turn - denied_turn
            if 0 <= turn_delta <= 3:
                overrides_list = meta.setdefault("overrides", [])
                overrides_list.append({
                    "override_key": override_key,
                    "turn": current_turn,
                    "denied_turn": denied_turn,
                })
                return GateVerdict.allow(
                    reason=f"Allowed by execute gate: repeated denied call within 3 turns (override_key: {override_key}).",
                    override=True,
                    override_key=override_key,
                )

        # 5. Policy Enforcement Matrix
        verdict: Optional[GateVerdict] = None

        # A. Tool 'request_scope' is always allowed
        if tool_name == "request_scope":
            verdict = GateVerdict.allow(reason="Allowed by execute gate: request_scope is always allowed.")

        # B. Edits are always allowed across all scope levels
        elif tool_name in ("edit_file", "write_file", "append_file") or event.event_type == "edit_file":
            verdict = GateVerdict.allow(reason="Allowed by execute gate: edits are always allowed across all scope levels.")

        else:
            # C. Determine Scope Level (default 1)
            scope = int(meta.get("current_scope", meta.get("scope", 1)))

            # Scope 3: Unrestricted
            if scope >= 3:
                verdict = GateVerdict.allow(reason="Allowed by execute gate: Scope 3 is unrestricted.")

            else:
                named_files = extract_named_files(active_state)

                # Safety Rule: Never gate the first read of a file named in prompt or traceback
                if tool_name == "read_file":
                    raw_path = str(arguments.get("path", "")).strip()
                    first_reads: Set[str] = meta.setdefault("first_read_files", set())
                    if is_named_file(raw_path, named_files) and raw_path not in first_reads and raw_path not in active_state.read_set:
                        first_reads.add(raw_path)
                        first_reads.add(Path(raw_path).name)
                        verdict = GateVerdict.allow(
                            reason=f"Allowed by execute gate: first read of file named directly in prompt or traceback ({raw_path}).",
                            named_file=True,
                        )

                # Scope 1 Policy Rules
                if verdict is None and scope == 1:
                    if tool_name == "read_file":
                        raw_path = str(arguments.get("path", "")).strip()
                        start = int(arguments.get("start", 1))
                        end = arguments.get("end")

                        # 400-line read window
                        if end is None or (isinstance(end, (int, float, str)) and int(end) - start + 1 > 400):
                            clamped_end = start + 399
                            rewritten_args = dict(arguments)
                            rewritten_args["start"] = start
                            rewritten_args["end"] = clamped_end
                            verdict = GateVerdict.rewrite(
                                rewritten_call=ToolCall(tool_name="read_file", arguments=rewritten_args),
                                reason="Rewritten by execute gate: Read window clamped to 400 lines for Scope 1.",
                            )
                        else:
                            # Max 3 file reads outside named set
                            outside_reads = {
                                f for f in active_state.read_set
                                if not is_named_file(f, named_files)
                            }
                            if not is_named_file(raw_path, named_files):
                                path_already_read = any(
                                    f == raw_path or Path(f).name == Path(raw_path).name
                                    for f in outside_reads
                                )
                                if not path_already_read and len(outside_reads) >= 3:
                                    verdict = GateVerdict.deny(
                                        reason="Denied by execute gate: Exceeded maximum 3 file reads outside named set for Scope 1. Consider requesting scope expansion.",
                                    )
                                else:
                                    verdict = GateVerdict.allow(reason="Allowed by execute gate: read_file within Scope 1 limits.")
                            else:
                                verdict = GateVerdict.allow(reason="Allowed by execute gate: named file read in Scope 1.")

                    elif tool_name == "list_dir":
                        depth = int(arguments.get("depth", 1))
                        if depth > 1:
                            rewritten_args = dict(arguments)
                            rewritten_args["depth"] = 1
                            verdict = GateVerdict.rewrite(
                                rewritten_call=ToolCall(tool_name="list_dir", arguments=rewritten_args),
                                reason="Rewritten by execute gate: list_dir depth clamped to 1 for Scope 1.",
                            )
                        else:
                            verdict = GateVerdict.allow(reason="Allowed by execute gate: list_dir depth 1 in Scope 1.")

                    elif tool_name == "run_tests":
                        target = arguments.get("target")
                        command = str(arguments.get("command", "")).strip()
                        # Deny full-suite test runs (suggest targeted path)
                        is_full_suite = (
                            not target
                            and (
                                not command
                                or command in ("pytest", "npm test", "cargo test", "python -m unittest", "ctest", "make test")
                            )
                        )
                        if is_full_suite:
                            verdict = GateVerdict.deny(
                                reason="Denied by execute gate: Full-suite test runs denied in Scope 1. Suggest targeted path (e.g. run_tests(target='tests/test_foo.py')).",
                            )
                        else:
                            verdict = GateVerdict.allow(reason="Allowed by execute gate: targeted test run in Scope 1.")

                    elif tool_name == "search":
                        path = str(arguments.get("path", ".")).strip()
                        if path in (".", "", "./"):
                            verdict = GateVerdict.deny(
                                reason="Denied by execute gate: Repository-wide search denied in Scope 1. Consider reading named files or requesting scope expansion.",
                            )
                        else:
                            verdict = GateVerdict.allow(reason="Allowed by execute gate: targeted search in Scope 1.")

                # Scope 2 Policy Rules
                elif verdict is None and scope == 2:
                    if tool_name == "read_file":
                        raw_path = str(arguments.get("path", "")).strip()
                        start = int(arguments.get("start", 1))
                        end = arguments.get("end")

                        # 800-line read window
                        if end is None or (isinstance(end, (int, float, str)) and int(end) - start + 1 > 800):
                            clamped_end = start + 799
                            rewritten_args = dict(arguments)
                            rewritten_args["start"] = start
                            rewritten_args["end"] = clamped_end
                            verdict = GateVerdict.rewrite(
                                rewritten_call=ToolCall(tool_name="read_file", arguments=rewritten_args),
                                reason="Rewritten by execute gate: Read window clamped to 800 lines for Scope 2.",
                            )
                        else:
                            # Max 10 file reads
                            path_already_read = any(
                                f == raw_path or Path(f).name == Path(raw_path).name
                                for f in active_state.read_set
                            )
                            if not path_already_read and len(active_state.read_set) >= 10:
                                verdict = GateVerdict.deny(
                                    reason="Denied by execute gate: Exceeded maximum 10 file reads for Scope 2. Consider calling request_scope.",
                                )
                            else:
                                verdict = GateVerdict.allow(reason="Allowed by execute gate: read_file within Scope 2 limits.")

                    elif tool_name == "list_dir":
                        path = str(arguments.get("path", ".")).strip()
                        depth = int(arguments.get("depth", 1))
                        # Package directory listing or capped root listing
                        if path in (".", "", "./") and depth > 2:
                            rewritten_args = dict(arguments)
                            rewritten_args["depth"] = 2
                            verdict = GateVerdict.rewrite(
                                rewritten_call=ToolCall(tool_name="list_dir", arguments=rewritten_args),
                                reason="Rewritten by execute gate: Root directory listing clamped to depth 2 in Scope 2. Target a package directory for deeper listings.",
                            )
                        else:
                            verdict = GateVerdict.allow(reason="Allowed by execute gate: list_dir allowed in Scope 2.")

                    elif tool_name == "search":
                        path = str(arguments.get("path", ".")).strip()
                        # Package directory search
                        if path in (".", "", "./"):
                            verdict = GateVerdict.deny(
                                reason="Denied by execute gate: Repository-wide search denied in Scope 2. Please specify a package directory (e.g. search(pattern='...', path='mypackage')).",
                            )
                        else:
                            verdict = GateVerdict.allow(reason="Allowed by execute gate: package directory search in Scope 2.")

                    elif tool_name == "run_tests":
                        verdict = GateVerdict.allow(reason="Allowed by execute gate: run_tests allowed in Scope 2.")

                # Default fallback for unhandled tools
                if verdict is None:
                    verdict = GateVerdict.allow(reason=f"Allowed by execute gate: {tool_name} allowed.")

        # 6. Post-processing: Handle Denials & Max Denies
        if verdict.status == GateVerdictStatus.DENY:
            # Store override key with current turn
            denied_calls[override_key] = current_turn
            verdict.metadata["override_key"] = override_key

            # Increment deny counter
            new_denies = int(meta.get("deny_count", 0)) + 1
            meta["deny_count"] = new_denies

            # Switch to observe mode if max_denies_per_task is reached
            if new_denies >= max_denies:
                meta["mode"] = "observe"
                if hasattr(active_state, "mode"):
                    active_state.mode = "observe"
                verdict.metadata["mode_switched"] = "observe"

        # 7. Observe Mode Conversion (log without blocking)
        if active_mode == "observe" and verdict.status != GateVerdictStatus.ALLOW:
            observed_status = verdict.status.value
            observed_reason = verdict.reason
            return GateVerdict.allow(
                reason=f"[OBSERVE MODE] Allowed by execute gate (would have been {observed_status}: {observed_reason})",
                observed_status=observed_status,
                observed_reason=observed_reason,
                original_verdict=verdict.to_dict(),
            )

        return verdict

    except Exception as exc:
        logger.error("Execute gate unexpected error; failing open: %s", exc)
        return GateVerdict.allow(
            reason=f"Allowed by execute gate (fail-open error: {exc})",
            fail_open=True,
            error=str(exc),
        )


class ExecuteGate:
    """
    Evaluates tool calls against predicted task scope and active budget constraints.
    """

    def __init__(self, mode: str = "enforce", max_denies_per_task: int = 3) -> None:
        self.mode = mode
        self.max_denies_per_task = max_denies_per_task

    def check(self, event: BpEvent, state: Optional[TaskState] = None) -> GateVerdict:
        """Evaluate an intercepted BpEvent against active gate policy and task state."""
        return check(event, state, mode=self.mode)

    def evaluate(self, tool_call: ToolCall, scope: ScopePrediction) -> GateVerdict:
        """
        Evaluate an incoming tool call against the predicted scope.
        Preserves compatibility with test_stub_modules_raise_not_implemented.
        """
        raise NotImplementedError("ExecuteGate.evaluate is scheduled for future API integration.")

"""
BudgetPilot hook engine for Claude Code.

Claude Code runs `python -m budgetpilot.adapters.hooks` for the events
registered by `bp install`; that small entry module calls `run()` here unless
BudgetPilot is off. It reads one JSON payload on stdin and may
print one JSON object on stdout. The contract (field names, output forms) is
documented in docs/HOOK_CONTRACT.md and pinned by the fixtures in
tests/fixtures/hooks, which are real captured payloads.

Rules this module never breaks:
- Fail open. Any error, bad input or locked database means exit code 0 and no
  output, so Claude Code proceeds as if BudgetPilot were not installed.
- Append only. It never touches the transcript or earlier tool results. It
  acts on the next tool call (PreToolUse) or on this tool result before the
  model sees it (PostToolUse).
- No network, no model calls, no API keys.
- Mode `off` does nothing except recognise the bp_* commands.

Standard library only; heavier modules are imported only on the paths that
need them, because start-up time is paid on every tool call.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

from budgetpilot.core import state as bp_state

CONTRACT_TEXT = (
    "BudgetPilot output contract: answer tersely; do not restate the task; do not re-print unchanged code; "
    "show diffs, not whole files; no preamble and at most a 3-line summary; edit with the smallest unique "
    "old_string."
)
SCOPE_LABEL = {1: "single-file", 2: "multi-file", 3: "broad"}
TRIM_THRESHOLD_CHARS = 6000
TRIM_HEAD_LINES = 40
TRIM_TAIL_LINES = 40
TRIM_SIDE_CHARS = 2500


# ------------------------------------------------------------------ helpers
def _chars_per_token(cfg: Dict[str, Any]) -> float:
    try:
        return float(cfg.get("chars_per_token") or 3.8)
    except (TypeError, ValueError):
        return 3.8


def _host() -> str:
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT", "") or "claude-code"


def _task_id(payload: Dict[str, Any], conn: Any) -> Optional[str]:
    prompt_id = payload.get("prompt_id")
    if prompt_id:
        return str(prompt_id)
    from budgetpilot.core import hookdb

    task = hookdb.latest_task(conn, session_id=str(payload.get("session_id") or ""))
    return task["task_id"] if task else None


def scope_context(limits: Any, out_budget: int) -> str:
    """The per-task note (kept under ~60 tokens)."""
    label = SCOPE_LABEL.get(limits.scope, "broad")
    if not limits.read_window_lines:
        return (
            f"BudgetPilot scope={limits.scope} ({label}). Limits are relaxed; aim to finish within "
            f"~{out_budget} output tokens."
        )
    return (
        f"BudgetPilot scope={limits.scope} ({label}). Read <={limits.read_window_lines}-line windows; "
        f"<={limits.max_files_outside_named} unnamed files; no repo-wide search or full test suite; "
        f"edits <={limits.edit_max_new_lines} lines; aim to finish within ~{out_budget} output tokens; "
        "if blocked, ask the user to expand scope."
    )


def trim_text(text: str) -> Tuple[str, int]:
    """Head + tail of a long text. Returns (text with a `{marker}` placeholder, lines removed)."""
    lines = text.splitlines()
    removed = 0
    if len(lines) > TRIM_HEAD_LINES + TRIM_TAIL_LINES:
        removed = len(lines) - TRIM_HEAD_LINES - TRIM_TAIL_LINES
        text = "\n".join(lines[:TRIM_HEAD_LINES] + ["{marker}"] + lines[-TRIM_TAIL_LINES:])
    if len(text) > 2 * TRIM_SIDE_CHARS + 200:
        middle = "" if removed else "\n{marker}\n"
        text = text[:TRIM_SIDE_CHARS] + middle + text[-TRIM_SIDE_CHARS:]
        if removed and "{marker}" not in text:
            text = text[:TRIM_SIDE_CHARS] + "\n{marker}\n" + text[-TRIM_SIDE_CHARS:]
    return text, removed


def save_raw(text: str) -> str:
    """Keep the full output on disk so trimming is lossless by reference."""
    folder = bp_state.get_bp_home() / "raw"
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:20]
    path = folder / f"{digest}.txt"
    if not path.exists():
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as f:
            f.write(text)
    return str(path)


def trim_bash_response(response: Dict[str, Any], chars_per_token: float) -> Optional[Tuple[Dict[str, Any], int]]:
    """
    (replacement tool_response, estimated tokens removed) for a noisy Bash
    result, or None when it is short enough. The replacement is the received
    object with only `stdout` / `stderr` changed, so its shape always matches.
    """
    from budgetpilot.core.ccgate import est_tokens

    stdout = response.get("stdout") if isinstance(response.get("stdout"), str) else ""
    stderr = response.get("stderr") if isinstance(response.get("stderr"), str) else ""
    if response.get("isImage") or len(stdout) + len(stderr) <= TRIM_THRESHOLD_CHARS:
        return None
    before = est_tokens(stdout + stderr, chars_per_token)
    raw_path = save_raw(stdout + ("\n--- stderr ---\n" + stderr if stderr else ""))
    updated = dict(response)
    for key, text in (("stdout", stdout), ("stderr", stderr)):
        if len(text) <= TRIM_THRESHOLD_CHARS // 2:
            continue
        shortened, removed_lines = trim_text(text)
        removed_tokens = max(0, est_tokens(text, chars_per_token) - est_tokens(shortened, chars_per_token))
        marker = f"[BudgetPilot trimmed {removed_lines} lines / ~{removed_tokens} tokens; full output: {raw_path}]"
        updated[key] = shortened.replace("{marker}", marker)
    after = est_tokens(str(updated.get("stdout", "")) + str(updated.get("stderr", "")), chars_per_token)
    if after >= before:
        return None
    return updated, before - after


def measured_text(tool: str, response: Any) -> Any:
    """The part of a tool result that becomes model input."""
    if not isinstance(response, dict):
        return response
    if tool == "Bash":
        return str(response.get("stdout") or "") + str(response.get("stderr") or "")
    if tool == "Read":
        file_info = response.get("file")
        if isinstance(file_info, dict):
            return file_info.get("content") or ""
    if tool == "Grep":
        return str(response.get("content") or "") + "\n".join(response.get("filenames") or [])
    if tool == "Glob":
        return "\n".join(response.get("filenames") or [])
    if tool in ("Edit", "MultiEdit", "Write"):
        return ""  # the model already produced this text; the result is a short confirmation
    return response


def _pre_output(decision: Any) -> Optional[Dict[str, Any]]:
    from budgetpilot.core.ccgate import READ_ONLY_TOOLS  # noqa: F401  (documented guard below)

    out: Dict[str, Any] = {"hookEventName": "PreToolUse"}
    if decision.verdict == "rewrite":
        out.update(permissionDecision="allow", permissionDecisionReason=decision.reason,
                   updatedInput=decision.updated_input)
    elif decision.verdict in ("deny", "ask"):
        out.update(permissionDecision=decision.verdict, permissionDecisionReason=decision.reason)
    elif decision.verdict != "context":
        return None
    if decision.context:
        out["additionalContext"] = decision.context
    return {"hookSpecificOutput": out}


def _ingest(conn: Any, payload: Dict[str, Any], retry: bool = False) -> None:
    """Update the token ledger from the transcript. Never raises."""
    try:
        from budgetpilot.core import cc_ledger

        path = payload.get("transcript_path")
        session_id = str(payload.get("session_id") or "")
        if not path or not session_id:
            return
        if retry and not os.path.exists(path):
            time.sleep(0.3)
        cc_ledger.ingest(conn, session_id, path)
    except Exception as exc:
        from budgetpilot.core import hookdb

        hookdb.log_error("ingest", exc)


def _task_output_tokens(conn: Any, task_id: str) -> Optional[int]:
    try:
        row = conn.execute("SELECT COALESCE(SUM(output_tokens), 0) FROM cc_turns WHERE task_id=?", (task_id,)).fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


# ------------------------------------------------------------------ handlers
def on_user_prompt_submit(payload: Dict[str, Any], cfg: Dict[str, Any], mode: str) -> Optional[Dict[str, Any]]:
    from budgetpilot.core import commands

    prompt = payload.get("prompt")
    command = commands.match(prompt)
    if command:
        return {
            "decision": "block",
            "reason": commands.run(*command),
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": True},
        }
    if mode == "off" or not isinstance(prompt, str):
        return None

    from budgetpilot.core import ccgate, hookdb
    from budgetpilot.core.recovery_flow import SLIDER_BUDGET_FACTOR
    from budgetpilot.core.scope import get_limits

    conn = hookdb.open_ready()
    if conn is None:
        return None
    try:
        session_id = str(payload.get("session_id") or "")
        cwd = str(payload.get("cwd") or "")
        hookdb.touch_session(conn, session_id, cwd, str(payload.get("transcript_path") or ""), _host())
        hookdb.heartbeat(conn, "hooks", "hook", {"host": _host(), "cwd": cwd})

        scope, confidence = 2, 0.0
        try:
            from budgetpilot.core.estimator import estimate

            prediction = estimate(prompt, repo_path=cwd or ".", session_id=session_id, persist=False)
            scope, confidence = int(prediction.scope), float(prediction.confidence)
        except Exception as exc:  # estimator trouble must not lose the task row
            hookdb.log_error("estimate", exc)

        slider = bp_state.get_budget_slider(cfg)
        factor = SLIDER_BUDGET_FACTOR.get(slider, 1.0)
        limits = get_limits(scope, cfg)
        cpt = _chars_per_token(cfg)
        out_budget = int(limits.output_budget_tokens * factor)
        task_id = str(payload.get("prompt_id") or f"{session_id}:{int(time.time() * 1000)}")

        context = ""
        if mode == "enforce":
            parts = []
            if not hookdb.contract_sent(conn, session_id):
                parts.append(CONTRACT_TEXT)
                hookdb.mark_contract_sent(conn, session_id)
            parts.append(scope_context(limits, out_budget))
            context = " ".join(parts)

        hookdb.write(conn, "UPDATE tasks_bp SET status='done' WHERE session_id=? AND status='active' AND task_id<>?",
                     (session_id, task_id))
        hookdb.create_task(conn, {
            "task_id": task_id, "session_id": session_id, "ts": time.time(),
            "repo_id": hookdb.repo_id_for(cwd), "cwd": cwd,
            "prompt_tokens_est": ccgate.est_tokens(prompt, cpt), "scope": scope, "scope_conf": confidence,
            "mode": mode, "budget_slider": slider,
            "ctx_budget": int(limits.context_budget_tokens * factor), "out_budget": out_budget,
            "overhead_tokens_est": ccgate.est_tokens(context, cpt), "state": ccgate.init_state(prompt),
        })
        if not context:
            return None
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
    finally:
        conn.close()


def on_session_start(payload: Dict[str, Any], cfg: Dict[str, Any], mode: str) -> Optional[Dict[str, Any]]:
    from budgetpilot.core import hookdb

    conn = hookdb.open_ready()
    if conn is None:
        return None
    try:
        session_id = str(payload.get("session_id") or "")
        cwd = str(payload.get("cwd") or "")
        hookdb.touch_session(conn, session_id, cwd, str(payload.get("transcript_path") or ""), _host())
        hookdb.heartbeat(conn, "claude_code", "host",
                         {"host": _host(), "source": payload.get("source"), "model": payload.get("model")},
                         min_interval=0)
        hookdb.heartbeat(conn, "hooks", "hook", {"host": _host(), "cwd": cwd})
        if mode != "enforce" or payload.get("source") == "resume" or hookdb.contract_sent(conn, session_id):
            return None
        hookdb.mark_contract_sent(conn, session_id)
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": CONTRACT_TEXT}}
    finally:
        conn.close()


def on_pre_tool_use(payload: Dict[str, Any], cfg: Dict[str, Any], mode: str) -> Optional[Dict[str, Any]]:
    from budgetpilot.core import ccgate, hookdb
    from budgetpilot.core.scope import get_limits

    started = time.perf_counter()
    conn = hookdb.open_ready()
    if conn is None:
        return None
    try:
        hookdb.heartbeat(conn, "hooks", "hook", {"host": _host(), "cwd": payload.get("cwd")})
        tool = str(payload.get("tool_name") or "")
        session_id = str(payload.get("session_id") or "")
        output: Optional[Dict[str, Any]] = None
        with hookdb.TaskTxn(conn, _task_id(payload, conn)) as txn:
            task = txn.task
            if task is None:
                return None
            state = task.setdefault("state", {})
            fallback = ccgate.gate_fallback_active(state)
            limits = get_limits(task.get("scope", 1), cfg)
            decision = ccgate.decide(payload, task, limits, hitl=bp_state.hitl_enabled(cfg))
            if decision.verdict == "rewrite" and tool not in ("Read", "Grep"):
                decision.verdict, decision.updated_input = "allow", None  # never auto-approve other tools

            acting = mode == "enforce" and not fallback
            verdict = decision.verdict
            if acting:
                output = _pre_output(decision)
                if tool in ccgate.EDIT_TOOLS and verdict in ("allow", "context"):
                    _, rel = ccgate.locate(str(payload.get("cwd") or ""), (payload.get("tool_input") or {}).get("file_path"))
                    if rel is not None and rel not in state.get("created", []):
                        from budgetpilot.core.recovery_flow import snapshot_file

                        snapshot_file(task["task_id"], str(payload.get("cwd") or ""), rel)
                if output is not None:
                    shown = decision.context + (decision.reason if verdict in ("deny", "ask") else "")
                    task["overhead_tokens_est"] = int(task.get("overhead_tokens_est", 0)) + ccgate.est_tokens(
                        shown, _chars_per_token(cfg))
            elif verdict not in ("allow",):
                verdict = f"would_{verdict}"
            hookdb.log_gate_event(
                conn, session_id, task["task_id"], tool, verdict, decision.rule, decision.reason or decision.context,
                decision.tokens_avoided_est if verdict in ("rewrite", "would_rewrite") else 0,
                (time.perf_counter() - started) * 1000.0,
            )
        return output
    finally:
        conn.close()


def on_post_tool_use(payload: Dict[str, Any], cfg: Dict[str, Any], mode: str) -> Optional[Dict[str, Any]]:
    from budgetpilot.core import ccgate, hookdb

    conn = hookdb.open_ready()
    if conn is None:
        return None
    try:
        tool = str(payload.get("tool_name") or "")
        ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        response = payload.get("tool_response")
        use_id = str(payload.get("tool_use_id") or "")
        session_id = str(payload.get("session_id") or "")
        cpt = _chars_per_token(cfg)
        enforce = mode == "enforce"

        tokens = ccgate.est_tokens(measured_text(tool, response), cpt)
        trimmed_tokens, output = 0, None
        if tool == "Bash" and isinstance(response, dict):
            trimmed = trim_bash_response(response, cpt)
            if trimmed is not None:
                trimmed_tokens = trimmed[1]
                if enforce:
                    output = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": trimmed[0]}}

        _ingest(conn, payload)
        task_id = _task_id(payload, conn)
        if tool == "Grep" and isinstance(response, dict) and response.get("appliedLimit") and task_id:
            # The capped search reports how many lines matched in total, so the
            # saving can be estimated from what was actually cut.
            shown, total = response.get("numLines") or 0, response.get("totalLines") or 0
            if isinstance(shown, int) and isinstance(total, int) and total > shown > 0:
                avoided = int(tokens / shown * (total - shown))
                hookdb.write(
                    conn,
                    "UPDATE gate_events SET tokens_avoided_est=? WHERE id=(SELECT MAX(id) FROM gate_events "
                    "WHERE task_id=? AND rule='grep_head_limit' AND verdict='rewrite' AND tokens_avoided_est=0)",
                    (avoided, task_id),
                )
        with hookdb.TaskTxn(conn, task_id) as txn:
            task = txn.task
            if task is not None:
                state = task.setdefault("state", {})
                task["ctx_used_est"] = int(task.get("ctx_used_est", 0)) + tokens - (trimmed_tokens if enforce else 0)
                measured = _task_output_tokens(conn, task["task_id"])
                if measured is not None:
                    task["out_used"] = measured

                recovery = state.get("recovery")
                if recovery and use_id and recovery.get("ask_id") == use_id:
                    from budgetpilot.core.recovery_flow import human_allowed_retry

                    human_allowed_retry(conn, task)
                if use_id and state.get("budget_ask") == use_id:
                    from budgetpilot.core.recovery_flow import expand_task_scope

                    state.pop("budget_ask", None)
                    expand_task_scope(task, str(task.get("budget_slider") or "balanced"))
                if tool in ccgate.EDIT_TOOLS:
                    state["edit_epoch"] = int(state.get("edit_epoch", 0)) + 1
                    state.setdefault("edits_since_fail", []).append(ccgate.call_key(tool, ti))
                elif tool == "Bash" and ccgate.is_test_command(str(ti.get("command", ""))) and state.get("last_failure"):
                    from budgetpilot.core.recovery_flow import on_test_pass

                    on_test_pass(conn, task)
        hookdb.log_tool_obs(conn, session_id, task_id, tool, tokens, trimmed_tokens,
                            truncated=bool(trimmed_tokens and enforce))
        return output
    finally:
        conn.close()


def on_post_tool_use_failure(payload: Dict[str, Any], cfg: Dict[str, Any], mode: str) -> Optional[Dict[str, Any]]:
    from budgetpilot.core import ccgate, hookdb

    if payload.get("is_interrupt"):
        return None
    conn = hookdb.open_ready()
    if conn is None:
        return None
    try:
        tool = str(payload.get("tool_name") or "")
        ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        error = payload.get("error") if isinstance(payload.get("error"), str) else ""
        session_id = str(payload.get("session_id") or "")
        use_id = str(payload.get("tool_use_id") or "")
        cwd = str(payload.get("cwd") or "")
        cpt = _chars_per_token(cfg)
        tokens = ccgate.est_tokens(error, cpt)
        command = str(ti.get("command", ""))
        is_test = tool == "Bash" and ccgate.is_test_command(command)
        task_id = _task_id(payload, conn)

        prepared = None
        if is_test and task_id:
            snapshot = hookdb.get_task(conn, task_id)
            if snapshot is not None:
                from budgetpilot.core.recovery_flow import prepare_failure

                slider = str(snapshot.get("budget_slider") or "balanced")
                for key, default in ccgate.init_state().items():
                    snapshot["state"].setdefault(key, default)
                prepared = prepare_failure(command, error, cwd, snapshot, slider)

        text = ""
        with hookdb.TaskTxn(conn, task_id) as txn:
            task = txn.task
            if task is not None:
                state = task.setdefault("state", {})
                for key, default in ccgate.init_state().items():
                    state.setdefault(key, default)
                task["ctx_used_est"] = int(task.get("ctx_used_est", 0)) + tokens
                if prepared is not None:
                    from budgetpilot.core import recovery_flow

                    recovery = state.get("recovery")
                    if recovery and use_id and recovery.get("ask_id") == use_id:
                        recovery_flow.human_allowed_retry(conn, task)
                    text = recovery_flow.on_test_failure(
                        conn, prepared, task, cwd, hitl=bp_state.hitl_enabled(cfg),
                        slider=str(task.get("budget_slider") or "balanced"), enforce=mode == "enforce",
                    )
                    if text:
                        task["overhead_tokens_est"] = int(task.get("overhead_tokens_est", 0)) + ccgate.est_tokens(text, cpt)
        hookdb.log_tool_obs(conn, session_id, task_id, tool, tokens)
        if not text:
            return None
        return {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure", "additionalContext": text}}
    finally:
        conn.close()


def on_stop(payload: Dict[str, Any], cfg: Dict[str, Any], mode: str) -> Optional[Dict[str, Any]]:
    """Update the token ledger and close the task. Never prints: output here would make the model continue."""
    from budgetpilot.core import hookdb

    conn = hookdb.open_ready()
    if conn is None:
        return None
    try:
        _ingest(conn, payload, retry=True)
        with hookdb.TaskTxn(conn, _task_id(payload, conn)) as txn:
            if txn.task is not None:
                measured = _task_output_tokens(conn, txn.task["task_id"])
                if measured is not None:
                    txn.task["out_used"] = measured
                if payload.get("hook_event_name") == "Stop":
                    txn.task["status"] = "done"
        hookdb.flush_outbox(conn)
        if payload.get("hook_event_name") == "SessionEnd":
            from budgetpilot.core.recovery_flow import cleanup_snapshots

            cleanup_snapshots()
        return None
    finally:
        conn.close()


HANDLERS = {
    "UserPromptSubmit": on_user_prompt_submit,
    "SessionStart": on_session_start,
    "PreToolUse": on_pre_tool_use,
    "PostToolUse": on_post_tool_use,
    "PostToolUseFailure": on_post_tool_use_failure,
    "Stop": on_stop,
    "SessionEnd": on_stop,
}


def handle(raw: str) -> Optional[Dict[str, Any]]:
    """Route one payload. Returns the JSON object to print, or None for no output."""
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return None
    event = payload.get("hook_event_name")
    handler = HANDLERS.get(event) if isinstance(event, str) else None
    if handler is None:
        return None
    cfg = bp_state.read_config()
    mode = bp_state.get_mode(cfg)
    if mode == "off" and event != "UserPromptSubmit":
        return None
    return handler(payload, cfg, mode)


def run(raw: str) -> int:
    """Handle one payload and print the result. Called by budgetpilot.adapters.hooks.main()."""
    output = handle(raw) if raw.strip() else None
    if output is not None:
        sys.stdout.write(json.dumps(output, ensure_ascii=False))
        sys.stdout.flush()
    return 0

"""
Failure handling for Claude Code sessions: fingerprints, routing, and the
human-in-the-loop choice (the 3E loop).

    Estimate again  -> router action `replan`
    Execute again   -> router action `reflect`
    Expand scope    -> router action `expand_scope`
    Escalate        -> advisory only: BudgetPilot cannot switch the host's model

The hook calls `on_test_failure()` when a test run fails and `on_test_pass()`
when one passes. The MCP tool and the CLI call `choose()` with the human's
decision. Decisions are recorded in `recovery_events`; failures are recorded
in `failure_fingerprints`, which persists across sessions per repository.

Nothing here edits a user's files except `replan`, and only after the human
chose it: it restores the copies BudgetPilot saved before the task's first
edit of each file.

Standard library only at import time; the verifier and router are loaded when
a failure actually happens.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from budgetpilot.core import hookdb
from budgetpilot.core.ccgate import est_tokens, extract_named
from budgetpilot.core.scope import get_limits
from budgetpilot.core.state import get_bp_home, read_config, set_mode

# 3E label for each router action, and every name `choose()` accepts.
LABELS = {"replan": "Estimate", "reflect": "Execute", "expand_scope": "Expand", "escalate": "Escalate"}
CHOICES = {
    "estimate": "replan", "replan": "replan",
    "execute": "reflect", "reflect": "reflect",
    "expand": "expand_scope", "expand_scope": "expand_scope",
    "escalate": "escalate",
    "retry": "retry", "deny": "deny", "mode_off": "mode_off",
}
SLIDER_BUDGET_FACTOR = {"tight": 0.75, "balanced": 1.0, "loose": 1.5}
DEFAULT_MAX_RECOVERY_ATTEMPTS = 3
SNAPSHOT_MAX_BYTES = 2_000_000
SNAPSHOT_KEEP_SECONDS = 86_400


# ------------------------------------------------------------------ analysis
def analyse_failure(command: str, output: str, cwd: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """Classify a failed test run and compute its signature."""
    from budgetpilot.core import verifier

    exit_match = re.match(r"\s*Exit code (-?\d+)", output or "")
    exit_code = int(exit_match.group(1)) if exit_match else 1
    touched = set(state.get("touched") or [])
    if "pytest" in command or "py.test" in command or re.search(r"=+ FAILURES =+|^E\s+\w", output or "", re.M):
        parsed = verifier.parse_pytest_output(output, repo_root=cwd, touched_set=touched)
    else:
        parsed = verifier.parse_generic_output(output, exit_code, repo_root=cwd)
    signature = verifier.compute_signature_hash(parsed["error_type"], parsed["top_in_repo_frame"])
    previous = set(state.get("signatures") or [])
    failure_class, rerun = verifier.classify_failure(
        error_type=parsed["error_type"],
        failure_message=parsed["failure_message"],
        failing_test_ids=parsed["failing_test_ids"],
        top_in_repo_frame=parsed["top_in_repo_frame"],
        outside_frames=parsed["outside_frames"],
        unread_symbols=parsed["unread_symbols"],
        touched_set=touched,
        signature_hash=signature,
        previous_signatures=previous,
        attempt_number=int(state.get("fail_count", 0)) + 1,
        full_output=output or "",
    )
    return {
        "signature": signature,
        "failure_class": failure_class.value,
        "env_noise": bool(rerun),
        "error_type": parsed["error_type"],
        "message": str(parsed["failure_message"])[:300],
        "failing_test_ids": list(parsed["failing_test_ids"])[:5],
        "top_frame": parsed["top_in_repo_frame"],
        "outside_frames": list(parsed["outside_frames"])[:5],
        "unread_symbols": list(parsed["unread_symbols"])[:5],
        "exit_code": exit_code,
    }


def digest(analysis: Dict[str, Any]) -> str:
    """One-line summary of a failure (the full output is already in the tool result)."""
    test = analysis["failing_test_ids"][0] if analysis.get("failing_test_ids") else "test run"
    return (
        f"failure [{analysis['signature'][:8]}, {analysis['failure_class']}] {test}: "
        f"{analysis['message'][:140]}"
    )


# ------------------------------------------------------------------ fingerprints
def upsert_fingerprint(conn: sqlite3.Connection, analysis: Dict[str, Any], repo_id: str, task_id: str) -> Dict[str, Any]:
    """Count this failure in the persistent ledger. Returns the row as it now stands."""
    now = time.time()
    try:
        conn.execute(
            "INSERT INTO failure_fingerprints (signature, repo_id, failure_class, first_seen, last_seen, count, "
            "sample_message, actions_tried, resolved_by, last_task_id) VALUES (?, ?, ?, ?, ?, 1, ?, '[]', '{}', ?) "
            "ON CONFLICT(signature, repo_id) DO UPDATE SET count=count+1, last_seen=excluded.last_seen, "
            "failure_class=excluded.failure_class, sample_message=excluded.sample_message, "
            "last_task_id=excluded.last_task_id",
            (analysis["signature"], repo_id, analysis["failure_class"], now, now, analysis["message"], task_id),
        )
        row = conn.execute(
            "SELECT count, actions_tried, resolved_by, first_seen FROM failure_fingerprints "
            "WHERE signature=? AND repo_id=?", (analysis["signature"], repo_id),
        ).fetchone()
        return {
            "count": row["count"], "first_seen": row["first_seen"],
            "actions_tried": json.loads(row["actions_tried"] or "[]"),
            "resolved_by": json.loads(row["resolved_by"] or "{}"),
        }
    except (sqlite3.Error, ValueError, TypeError) as exc:
        hookdb.log_error("upsert_fingerprint", exc)
        return {"count": 1, "actions_tried": [], "resolved_by": {}}


def _update_fingerprint(conn: sqlite3.Connection, signature: str, repo_id: str, tried: Optional[str] = None,
                        resolved: Optional[str] = None) -> None:
    try:
        row = conn.execute(
            "SELECT actions_tried, resolved_by FROM failure_fingerprints WHERE signature=? AND repo_id=?",
            (signature, repo_id),
        ).fetchone()
        if not row:
            return
        actions = json.loads(row["actions_tried"] or "[]")
        resolved_by = json.loads(row["resolved_by"] or "{}")
        if tried and tried not in actions:
            actions.append(tried)
        if resolved:
            resolved_by[resolved] = int(resolved_by.get(resolved, 0)) + 1
        conn.execute(
            "UPDATE failure_fingerprints SET actions_tried=?, resolved_by=? WHERE signature=? AND repo_id=?",
            (json.dumps(actions), json.dumps(resolved_by), signature, repo_id),
        )
    except (sqlite3.Error, ValueError, TypeError) as exc:
        hookdb.log_error("_update_fingerprint", exc)


# ------------------------------------------------------------------ routing
def route_failure(analysis: Dict[str, Any], task: Dict[str, Any], slider: str) -> Tuple[str, Dict[str, float]]:
    """(recommended router action, utility per action). Falls back to `reflect`."""
    state = task["state"]
    try:
        from budgetpilot.core.router import route
        from budgetpilot.core.types import FailureClass, RecoveryAction, RecoveryContext

        previous = [RecoveryAction(a) for a in state.get("actions", []) if a in LABELS]
        ctx = RecoveryContext(
            failure_class=FailureClass(analysis["failure_class"]),
            error_message=analysis["message"],
            attempt_number=int(state.get("fail_count", 1)),
            files_touched=list(state.get("touched") or []),
            previous_actions=previous,
            tokens_spent=int(task.get("ctx_used_est", 0)) + int(task.get("out_used", 0)),
            session_id=str(task.get("session_id", "")),
            outside_frames=list(analysis.get("outside_frames") or []),
            unread_symbols=list(analysis.get("unread_symbols") or []),
            signature_hash=analysis["signature"],
        )
        decision = route(ctx, budget=slider, db_path=hookdb.db_path())
        scores: Dict[str, float] = {}
        for action, detail in (decision.metadata.get("scores") or {}).items():
            name = getattr(action, "value", str(action))
            utility = detail.get("utility") if isinstance(detail, dict) else detail
            if name in LABELS and isinstance(utility, (int, float)):
                scores[name] = round(float(utility), 3)
        return decision.action.value, scores
    except Exception as exc:
        hookdb.log_error("route_failure", exc)
        return "reflect", {}


# ------------------------------------------------------------------ snapshots
def _snapshot_dir(task_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)[:80]
    return get_bp_home() / "snapshots" / safe


def snapshot_file(task_id: str, cwd: str, rel_path: str) -> bool:
    """Save a file's content before the task's first edit of it (used only by `replan`)."""
    try:
        source = Path(cwd) / rel_path
        if not source.is_file() or source.stat().st_size > SNAPSHOT_MAX_BYTES:
            return False
        folder = _snapshot_dir(task_id)
        folder.mkdir(parents=True, exist_ok=True)
        manifest_path = folder / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        if rel_path in manifest:
            return True
        name = hashlib.sha1(rel_path.encode("utf-8")).hexdigest() + ".orig"
        shutil.copyfile(source, folder / name)
        manifest[rel_path] = name
        manifest_path.write_text(json.dumps(manifest))
        return True
    except (OSError, ValueError) as exc:
        hookdb.log_error("snapshot_file", exc)
        return False


def restore_snapshots(task_id: str, cwd: str, state: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Put edited files back as they were before this task, and remove files the
    task created (only when git reports them untracked). Paths outside the
    repository or inside .git are refused.
    """
    root = Path(cwd).resolve()
    restored: List[str] = []
    removed: List[str] = []
    refused: List[str] = []
    folder = _snapshot_dir(task_id)
    try:
        manifest = json.loads((folder / "manifest.json").read_text())
    except (OSError, ValueError):
        manifest = {}

    def safe_target(rel: str) -> Optional[Path]:
        target = (root / rel).resolve()
        if root not in target.parents or ".git" in Path(rel).parts:
            refused.append(rel)
            return None
        return target

    for rel, name in manifest.items():
        target = safe_target(rel)
        if target is None:
            continue
        try:
            shutil.copyfile(folder / name, target)
            restored.append(rel)
        except OSError as exc:
            hookdb.log_error("restore_snapshots", exc)
    for rel in state.get("created") or []:
        target = safe_target(rel)
        if target is None or not target.is_file():
            continue
        if hookdb.git_untracked(str(root), rel):
            try:
                target.unlink()
                removed.append(rel)
            except OSError as exc:
                hookdb.log_error("restore_snapshots", exc)
    return {"restored": restored, "removed": removed, "refused": refused}


def cleanup_snapshots(max_age: float = SNAPSHOT_KEEP_SECONDS) -> None:
    base = get_bp_home() / "snapshots"
    try:
        if not base.is_dir():
            return
        cutoff = time.time() - max_age
        for folder in base.iterdir():
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
    except OSError:
        pass


# ------------------------------------------------------------------ directives
def expand_task_scope(task: Dict[str, Any], slider: str) -> int:
    """Raise the task one scope level (max 3) and widen its budgets."""
    new_scope = min(3, int(task.get("scope", 1)) + 1)
    limits = get_limits(new_scope)
    factor = SLIDER_BUDGET_FACTOR.get(slider, 1.0)
    task["scope"] = new_scope
    task["ctx_budget"] = max(int(task.get("ctx_budget", 0)), int(limits.context_budget_tokens * factor))
    task["out_budget"] = max(int(task.get("out_budget", 0)), int(limits.output_budget_tokens * factor))
    task["state"].pop("budget_hit", None)
    task["state"].pop("warned80", None)
    return new_scope


def _frontier_files(analysis: Dict[str, Any], task: Dict[str, Any], cwd: str) -> List[str]:
    try:
        from budgetpilot.core.frontier import build_dependency_frontier
        from budgetpilot.core.types import FailureClass, RecoveryContext

        ctx = RecoveryContext(
            failure_class=FailureClass(analysis.get("failure_class", "unknown")),
            error_message=analysis.get("message", ""),
            files_touched=list(task["state"].get("touched") or []),
            outside_frames=list(analysis.get("outside_frames") or []),
            unread_symbols=list(analysis.get("unread_symbols") or []),
        )
        frontier = build_dependency_frontier(ctx, repo_root=cwd, current_scope=int(task.get("scope", 1)))
        return [f for f in frontier.frontier_files if f][:5]
    except Exception as exc:
        hookdb.log_error("_frontier_files", exc)
        return []


def escalation_estimate(conn: Optional[sqlite3.Connection], session_id: str) -> str:
    """Rough cost of switching model: the whole context is re-written to the cache."""
    try:
        row = conn.execute(
            "SELECT context_tokens FROM cc_turns WHERE session_id=? ORDER BY ts DESC LIMIT 1", (session_id,)
        ).fetchone() if conn is not None else None
        if row and row[0]:
            return f"about {int(row[0]):,} context tokens would be re-written to the cache (estimate)"
    except sqlite3.Error:
        pass
    return "the current context would be re-written to the cache"


def directive_for(action: str, task: Dict[str, Any], analysis: Dict[str, Any], cwd: str, slider: str,
                  conn: Optional[sqlite3.Connection] = None, restore: bool = False) -> str:
    """Instruction text for the host agent (kept under ~150 tokens)."""
    state = task["state"]
    if action == "reflect":
        return (
            "BudgetPilot [Execute]: retry with the smallest fix for the failure above. "
            "Do not read new files; re-run only the failing test."
        )
    if action == "replan":
        if restore:
            result = restore_snapshots(task["task_id"], cwd, state)
            changed = ", ".join(result["restored"] + result["removed"]) or "nothing to restore"
            state["edits_since_fail"] = []
            return (
                f"BudgetPilot [Estimate]: files are back to their state before this task ({changed}). "
                "Re-read the task, state a plan in at most 3 lines, then make the smallest change."
            )
        return (
            "BudgetPilot [Estimate]: the current approach is not working. Undo your edits that did not help, "
            "state a new plan in at most 3 lines, then make the smallest change."
        )
    if action == "expand_scope":
        old_scope = int(task.get("scope", 1))
        new_scope = expand_task_scope(task, slider)
        files = _frontier_files(analysis, task, cwd)
        for name in extract_named(" ".join(files)):
            if name not in state["named"]:
                state["named"].append(name)
        listed = ", ".join(files) if files else "the files in the traceback"
        return (
            f"BudgetPilot [Expand]: scope {old_scope} -> {new_scope}. The cause is likely outside the files "
            f"you touched. Read {listed}, then retry."
        )
    if action == "escalate":
        return (
            "BudgetPilot [Escalate]: BudgetPilot cannot switch models. Tell the user this failure may need a "
            f"stronger model (they can run /model); {escalation_estimate(conn, str(task.get('session_id', '')))}. "
            "Then stop and wait."
        )
    return ""


# ------------------------------------------------------------------ hook entry points
def _record_event(conn: sqlite3.Connection, task: Dict[str, Any], analysis: Dict[str, Any], attempt: int,
                  scores: Dict[str, float], recommended: str, chosen: Optional[str] = None,
                  decided_by: Optional[str] = None, outcome: Optional[str] = None) -> Optional[int]:
    try:
        cur = conn.execute(
            "INSERT INTO recovery_events (session_id, task_id, ts, signature, failure_class, attempt, router_scores, "
            "recommended, chosen, decided_by, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task["session_id"], task["task_id"], time.time(), analysis["signature"], analysis["failure_class"],
             attempt, json.dumps(scores), recommended, chosen, decided_by, outcome),
        )
        return cur.lastrowid
    except sqlite3.Error as exc:
        hookdb.log_error("_record_event", exc)
        return None


def options_text(recovery: Dict[str, Any]) -> str:
    """The numbered choices shown to the model, which relays them to the user."""
    recommended = recovery["recommended"]
    others = [a for a in ("reflect", "replan", "expand_scope", "escalate") if a != recommended]
    scores = recovery.get("scores") or {}
    second = max(others, key=lambda a: scores.get(a, -9.0)) if scores else others[0]
    utility = f" U={scores[recommended]}" if recommended in scores else ""
    hint = f" {recovery['resolved_hint']}" if recovery.get("resolved_hint") else ""
    return (
        f"BudgetPilot: {recovery['digest']}.{hint} Before editing again, call AskUserQuestion with these "
        f"options, then call the MCP tool bp_recovery_choose(action): 1) {LABELS[recommended]} "
        f"(recommended{utility}) 2) {LABELS[second]} 3) retry 4) deny (continue unmanaged this task) 5) mode_off."
    )


def prepare_failure(command: str, output: str, cwd: str, task: Dict[str, Any], slider: str) -> Dict[str, Any]:
    """
    Slow, read-only part of failure handling: parse, classify and route.
    Run this BEFORE opening the task transaction: the router writes its own
    decision row on a separate connection and must not wait on our write lock.
    """
    analysis = analyse_failure(command, output, cwd, task["state"])
    if analysis["env_noise"]:
        return {"analysis": analysis, "recommended": "reflect", "scores": {}}
    probe = dict(task)
    probe["state"] = dict(task["state"], fail_count=int(task["state"].get("fail_count", 0)) + 1)
    recommended, scores = route_failure(analysis, probe, slider)
    return {"analysis": analysis, "recommended": recommended, "scores": scores}


def on_test_failure(conn: sqlite3.Connection, prepared: Dict[str, Any], task: Dict[str, Any], cwd: str,
                    hitl: bool, slider: str, enforce: bool) -> str:
    """
    Record a failed test run and return the text to add next to the failure
    (empty in observe mode). Mutates `task` and `task["state"]`; call it inside
    the task transaction with the result of `prepare_failure()`.
    """
    state = task["state"]
    analysis = prepared["analysis"]
    if analysis["env_noise"]:
        state["last_failure"] = {"signature": analysis["signature"], "class": analysis["failure_class"]}
        return "BudgetPilot: this looks like an environment or flaky failure. Re-run once before changing code." \
            if enforce else ""

    # Edits made since the previous failure did not fix it if the signature repeats.
    previous = state.get("last_failure") or {}
    if previous.get("signature") == analysis["signature"] and state.get("edits_since_fail"):
        failed = state["failed_edits"].setdefault(analysis["signature"], [])
        failed.extend(h for h in state["edits_since_fail"] if h not in failed)
    state["edits_since_fail"] = []
    state["fail_count"] = int(state.get("fail_count", 0)) + 1
    attempt = state["fail_count"]
    signatures = state.setdefault("signatures", [])
    if analysis["signature"] not in signatures:
        signatures.append(analysis["signature"])
    state["last_failure"] = {
        "signature": analysis["signature"], "class": analysis["failure_class"], "analysis": analysis,
    }

    fingerprint = upsert_fingerprint(conn, analysis, str(task.get("repo_id", "")), task["task_id"])
    recommended, scores = prepared["recommended"], prepared["scores"]

    if not enforce:
        _record_event(conn, task, analysis, attempt, scores, recommended, outcome="observed")
        return ""
    if state.get("recovery_declined") or state.get("recovery_skipped"):
        _record_event(conn, task, analysis, attempt, scores, recommended, outcome="not_managed")
        return ""

    max_attempts = int((read_config().get("limits") or {}).get("max_recovery_attempts", DEFAULT_MAX_RECOVERY_ATTEMPTS))
    if attempt > max_attempts:
        _record_event(conn, task, analysis, attempt, scores, recommended, chosen="stop", decided_by="auto",
                      outcome="attempt_cap")
        state.pop("recovery", None)
        task["pending_recovery"] = None
        return (
            f"BudgetPilot: {attempt - 1} recovery attempts used on this task. Stop and report to the user "
            "what failed and what you tried."
        )

    resolved_by = fingerprint.get("resolved_by") or {}
    hint = ""
    if resolved_by:
        best = max(resolved_by, key=lambda k: resolved_by[k])
        hint = f"Seen {fingerprint['count']}x in this repo; previously resolved by {LABELS.get(best, best)} " \
               f"({resolved_by[best]}x)."

    if not hitl:
        event_id = _record_event(conn, task, analysis, attempt, scores, recommended, chosen=recommended,
                                 decided_by="auto")
        state["actions"].append(recommended)
        state["applied"] = {"action": recommended, "event_id": event_id, "signature": analysis["signature"]}
        _update_fingerprint(conn, analysis["signature"], str(task.get("repo_id", "")), tried=recommended)
        return directive_for(recommended, task, analysis, cwd, slider, conn=conn, restore=False)

    event_id = _record_event(conn, task, analysis, attempt, scores, recommended)
    recovery = {
        "signature": analysis["signature"], "class": analysis["failure_class"], "recommended": recommended,
        "recommended_label": LABELS[recommended], "scores": scores, "attempt": attempt, "asks": 0,
        "event_id": event_id, "digest": digest(analysis), "resolved_hint": hint,
    }
    state["recovery"] = recovery
    task["pending_recovery"] = analysis["signature"]
    return options_text(recovery)


def on_test_pass(conn: sqlite3.Connection, task: Dict[str, Any]) -> None:
    """A passing run after a failure resolves it and credits the action that was applied."""
    state = task["state"]
    last = state.get("last_failure")
    if not last:
        return
    applied = state.get("applied") or {}
    action = applied.get("action") or "retry"
    try:
        if applied.get("event_id"):
            conn.execute("UPDATE recovery_events SET outcome='resolved' WHERE id=?", (applied["event_id"],))
        else:
            conn.execute(
                "UPDATE recovery_events SET outcome='resolved' WHERE id=(SELECT MAX(id) FROM recovery_events "
                "WHERE task_id=? AND signature=?)", (task["task_id"], last["signature"]),
            )
    except sqlite3.Error as exc:
        hookdb.log_error("on_test_pass", exc)
    _update_fingerprint(conn, last["signature"], str(task.get("repo_id", "")), resolved=action)
    for key in ("last_failure", "recovery", "applied"):
        state.pop(key, None)
    state["edits_since_fail"] = []
    task["pending_recovery"] = None


def human_allowed_retry(conn: sqlite3.Connection, task: Dict[str, Any]) -> None:
    """The user approved the native dialog: the pending recovery is resolved as `retry`."""
    state = task["state"]
    recovery = state.pop("recovery", None)
    task["pending_recovery"] = None
    if not recovery:
        return
    state["applied"] = {"action": "retry", "event_id": recovery.get("event_id"), "signature": recovery["signature"]}
    try:
        conn.execute("UPDATE recovery_events SET chosen='retry', decided_by='human' WHERE id=?",
                     (recovery.get("event_id"),))
    except sqlite3.Error as exc:
        hookdb.log_error("human_allowed_retry", exc)


# ------------------------------------------------------------------ human choice (MCP / CLI)
def pending_options(conn: sqlite3.Connection) -> Dict[str, Any]:
    task = hookdb.latest_task(conn, pending_only=True)
    if not task or not task["state"].get("recovery"):
        return {"pending": False}
    recovery = task["state"]["recovery"]
    return {
        "pending": True, "task_id": task["task_id"], "signature": recovery["signature"][:8],
        "failure_class": recovery["class"], "recommended": LABELS[recovery["recommended"]].lower(),
        "scores": {LABELS[k].lower(): v for k, v in (recovery.get("scores") or {}).items()},
        "choices": ["estimate", "execute", "expand", "escalate", "retry", "deny", "mode_off"],
    }


def choose(conn: sqlite3.Connection, action: str, note: str = "") -> Dict[str, Any]:
    """
    Apply the human's recovery decision to the most recent task with a pending
    recovery. Returns {"ok", "action", "directive"}.
    """
    resolved = CHOICES.get(str(action).strip().lower())
    if resolved is None:
        return {"ok": False, "error": f"unknown action '{action}'", "choices": sorted(set(CHOICES))}
    pending = hookdb.latest_task(conn, pending_only=True)
    if pending is None:
        if resolved == "mode_off":
            set_mode("off")
            return {"ok": True, "action": "mode_off", "directive": "BudgetPilot is OFF."}
        return {"ok": False, "error": "no recovery is pending"}

    slider = str(pending.get("budget_slider") or "balanced")
    with hookdb.TaskTxn(conn, pending["task_id"]) as txn:
        task = txn.task
        if task is None or not task["state"].get("recovery"):
            return {"ok": False, "error": "no recovery is pending"}
        state = task["state"]
        recovery = state.pop("recovery")
        task["pending_recovery"] = None
        analysis = (state.get("last_failure") or {}).get("analysis") or {
            "signature": recovery["signature"], "failure_class": recovery["class"], "message": "",
        }
        cwd = str(task.get("cwd") or "")
        directive = ""
        if resolved == "mode_off":
            set_mode("off")
            directive = "BudgetPilot is OFF. Continue normally."
        elif resolved == "deny":
            state["recovery_declined"] = True
            directive = "BudgetPilot recovery declined for this task. Continue normally."
        elif resolved == "retry":
            directive = "Continue as you were."
        else:
            directive = directive_for(resolved, task, analysis, cwd, slider, conn=conn, restore=True)
            state["actions"].append(resolved)
            _update_fingerprint(conn, recovery["signature"], str(task.get("repo_id", "")), tried=resolved)
        state["applied"] = {"action": resolved, "event_id": recovery.get("event_id"),
                            "signature": recovery["signature"]}
        try:
            conn.execute(
                "UPDATE recovery_events SET chosen=?, decided_by='human', outcome=? WHERE id=?",
                (resolved, (note or None) and note[:200], recovery.get("event_id")),
            )
        except sqlite3.Error as exc:
            hookdb.log_error("choose", exc)
        task["overhead_tokens_est"] = int(task.get("overhead_tokens_est", 0)) + est_tokens(directive)
    return {"ok": True, "action": LABELS.get(resolved, resolved).lower(), "directive": directive}

"""
BudgetPilot user commands: bp_mode_on / bp_mode_off / bp_mode_observe,
bp_budget <dial>, bp_status, bp_dashboard.

One implementation shared by three entry points, so they cannot drift:
- the UserPromptSubmit hook (the prompt is consumed and never reaches the
  model, so switching mode costs zero model tokens),
- the MCP tools (`bp_mode`, `bp_set_budget`, `bp_status`, `bp_dashboard`),
- the CLI (`bp mode`, `bp budget`, `bp status`, `bp dashboard`).

Standard library only.
"""

from __future__ import annotations

import re
import sqlite3
import time

from budgetpilot.core import state

_COMMANDS = (
    (re.compile(r"^/?bp[ _](?:mode[ _])?(on|off|observe)$", re.IGNORECASE), "mode"),
    (re.compile(r"^/?bp[ _]budget[ _](tight|balanced|loose)$", re.IGNORECASE), "budget"),
    (re.compile(r"^/?bp[ _]dashboard$", re.IGNORECASE), "dashboard"),
    (re.compile(r"^/?bp[ _]status$", re.IGNORECASE), "status"),
)

MODE_LABEL = {"enforce": "ON", "observe": "OBSERVE", "off": "OFF"}


def match(prompt: Any) -> Optional[Tuple[str, str]]:
    """(command, argument) if the whole prompt is a BudgetPilot command, else None."""
    if not isinstance(prompt, str) or len(prompt) > 40:
        return None
    text = prompt.strip()
    for pattern, name in _COMMANDS:
        found = pattern.match(text)
        if found:
            return name, (found.group(1).lower() if found.groups() else "")
    return None


def set_mode_text(mode: str) -> str:
    target = state.MODE_ALIASES.get(mode.strip().lower())
    if target is None:
        return f"BudgetPilot: unknown mode '{mode}'. Use on, off or observe."
    if state.env_overrides_mode() and state.get_mode() != target:
        return (
            f"BudgetPilot: the BP_MODE environment variable pins the mode to {MODE_LABEL[state.get_mode()]}. "
            "Unset it to switch."
        )
    state.set_mode(target)
    dial = state.get_budget_slider()
    if target == "enforce":
        return f"BudgetPilot ON · {dial} · gate, output limits and ledger are live. Dashboard: bp_dashboard"
    if target == "observe":
        return "BudgetPilot OBSERVE · measuring only, no intervention. Dashboard: bp_dashboard"
    return "BudgetPilot OFF · hooks are silent."


def set_budget_text(dial: str) -> str:
    try:
        resolved = state.set_budget_slider(dial)
    except ValueError as exc:
        return f"BudgetPilot: {exc}"
    return f"BudgetPilot budget: {resolved} (mode {MODE_LABEL[state.get_mode()]})."


def _fmt(n: Any) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def status_data(conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Compact status for `bp_status` (kept small: tool output is input tokens next turn)."""
    data: Dict[str, Any] = {"mode": state.get_mode(), "budget": state.get_budget_slider()}
    own = False
    try:
        if conn is None:
            from budgetpilot.core import hookdb

            conn = hookdb.open_ready()
            own = True
        if conn is None:
            return data
        since = time.time() - 86_400
        day = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since))
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(cache_write_5m+cache_write_1h),0), "
            "COALESCE(SUM(cache_read),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(billing_units),0) "
            "FROM cc_turns WHERE ts >= ?", (day,),
        ).fetchone()
        data["last_24h"] = {
            "turns": row[0], "input": row[1], "cache_write": row[2], "cache_read": row[3], "output": row[4],
            "billing_units": round(row[5]),
        }
        latest = conn.execute(
            "SELECT billing_mode, billing_provider, billing_confidence, billing_evidence, billing_source, "
            "billing_mixed FROM cc_sessions ORDER BY last_seen DESC LIMIT 1").fetchone()
        if latest:
            from budgetpilot.hosts.billing import from_row
            from budgetpilot.hosts.policy import report_policy

            row_d = dict(zip(("billing_mode", "billing_provider", "billing_confidence", "billing_evidence",
                              "billing_source", "billing_mixed"), latest))
            billing = from_row(row_d)
            policy = report_policy(billing, mixed=bool(row_d["billing_mixed"]))
            data["billing"] = {"mode": billing.mode, "confidence": billing.confidence,
                               "set_by_user": billing.source == "user_override", "mixed": policy["mixed"],
                               "units_label": policy["units_label"]}
        gate = conn.execute(
            "SELECT verdict, COUNT(*), COALESCE(SUM(tokens_avoided_est),0) FROM gate_events WHERE ts >= ? "
            "GROUP BY verdict", (since,),
        ).fetchall()
        if gate:
            data["gate_24h"] = {r[0]: r[1] for r in gate}
            data["avoided_est"] = int(sum(r[2] for r in gate if r[0] in ("rewrite", "deny")))
        task = conn.execute(
            "SELECT scope, ctx_used_est, ctx_budget, out_used, out_budget, pending_recovery FROM tasks_bp "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if task:
            data["task"] = {"scope": task[0], "ctx": f"{task[1]}/{task[2]}", "out": f"{task[3]}/{task[4]}"}
            if task[5]:
                data["task"]["recovery_pending"] = task[5][:8]
    except sqlite3.Error:
        pass
    finally:
        if own and conn is not None:
            conn.close()
    return data


def status_text() -> str:
    data = status_data()
    lines = [f"BudgetPilot {MODE_LABEL[data['mode']]} · budget {data['budget']}"]
    usage = data.get("last_24h")
    if usage and usage["turns"]:
        lines.append(
            f"24h: {usage['turns']} turns · in {_fmt(usage['input'])} · cache write {_fmt(usage['cache_write'])} · "
            f"cache read {_fmt(usage['cache_read'])} · out {_fmt(usage['output'])}"
        )
    billing = data.get("billing")
    if billing:
        lines.append(f"Billing: {billing['mode']} ({billing['confidence']} confidence"
                     + (", set by you" if billing["set_by_user"] else "") + (", mixed" if billing["mixed"] else "")
                     + (")" if billing["mode"] in ("api", "cloud") else "): tokens only, no dollar figures"))
    task = data.get("task")
    if task:
        avoided = f" · avoided ~{_fmt(data['avoided_est'])} tokens (estimated)" if data.get("avoided_est") else ""
        lines.append(f"Task: scope {task['scope']} · context {task['ctx']} · output {task['out']}{avoided}")
    return "\n".join(lines)


def dashboard_text(port: Optional[int] = None, open_browser: bool = False) -> str:
    try:
        from budgetpilot.dashboard import ensure_running

        url = ensure_running(port=port, open_browser=open_browser)
        return f"BudgetPilot dashboard → {url}"
    except Exception as exc:
        return f"BudgetPilot: could not start the dashboard ({type(exc).__name__}: {str(exc)[:120]})."


def run(name: str, arg: str = "") -> str:
    """Execute a command and return the one-to-three-line confirmation."""
    if name == "mode":
        return set_mode_text(arg)
    if name == "budget":
        return set_budget_text(arg)
    if name == "status":
        return status_text()
    if name == "dashboard":
        return dashboard_text()
    return f"BudgetPilot: unknown command '{name}'."

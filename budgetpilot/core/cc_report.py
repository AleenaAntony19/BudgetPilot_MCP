"""
`bp report` for Claude Code usage: tokens per session from the transcript
ledger, with billing units and dollars exactly as `hosts.policy.report_policy`
allows for each session's billing.

Dollar totals include only sessions billed per token whose billing did not
change mid-session.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from budgetpilot.core.cc_ledger import session_totals
from budgetpilot.hosts import get_adapter
from budgetpilot.hosts.billing import from_row
from budgetpilot.hosts.policy import money, report_policy

TOKEN_KEYS = ("turns", "input_tokens", "cache_write", "cache_read", "output_tokens", "billing_units")


def has_data(conn: sqlite3.Connection) -> bool:
    try:
        return conn.execute("SELECT 1 FROM cc_turns LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def build(conn: sqlite3.Connection, session_id: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    where, params = ("WHERE t.session_id = ?", [session_id]) if session_id else ("", [])
    rows = conn.execute(
        "SELECT t.session_id, COALESCE(t.host, 'claude_code') AS host, MIN(t.ts) AS first_ts, MAX(t.ts) AS last_ts, "
        "COUNT(*) AS turns, SUM(t.input_tokens) AS input_tokens, "
        "SUM(t.cache_write_5m + t.cache_write_1h) AS cache_write, SUM(t.cache_read) AS cache_read, "
        "SUM(t.output_tokens) AS output_tokens, 0 AS billing_units, 0 AS ref_usd, "
        "s.billing_mode, s.billing_provider, s.billing_confidence, s.billing_evidence, s.billing_source, "
        "s.billing_mixed "
        f"FROM cc_turns t LEFT JOIN cc_sessions s ON s.session_id = t.session_id {where} "
        "GROUP BY t.session_id ORDER BY last_ts DESC LIMIT ?", params + [limit],
    ).fetchall()
    sessions: List[Dict[str, Any]] = []
    totals = {k: 0 for k in TOKEN_KEYS}
    dollars, dollar_sessions = 0.0, 0
    for row in rows:
        r = dict(zip(("session_id", "host", "first_ts", "last_ts", "turns", "input_tokens", "cache_write",
                      "cache_read", "output_tokens", "billing_units", "ref_usd", "billing_mode", "billing_provider",
                      "billing_confidence", "billing_evidence", "billing_source", "billing_mixed"), row))
        priced = session_totals(conn, session_id=r["session_id"])  # priced from the price book now
        r["billing_units"], r["ref_usd"] = priced["billing_units"], priced["ref_usd"]
        billing = from_row(r)
        policy = report_policy(billing, get_adapter(r["host"] if r["host"] in ("claude_code", "generic_mcp")
                                                    else "claude_code").capabilities(),
                               mixed=bool(r["billing_mixed"]))
        entry = {k: r[k] for k in ("session_id", "host", "first_ts", "last_ts")}
        entry.update({k: (round(r[k] or 0) if k == "billing_units" else int(r[k] or 0)) for k in TOKEN_KEYS})
        entry["billing"] = {"mode": billing.mode, "provider": billing.provider, "confidence": billing.confidence,
                            "set_by_user": billing.source == "user_override", "mixed": bool(r["billing_mixed"])}
        if policy["show_dollars"]:
            entry["ref_usd"] = round(float(r["ref_usd"] or 0), 6)
            dollars += float(r["ref_usd"] or 0)
            dollar_sessions += 1
        entry["policy"] = policy
        sessions.append(entry)
        for k in TOKEN_KEYS:
            totals[k] += entry[k]
    out: Dict[str, Any] = {"report": "claude_code_usage", "sessions": sessions, "totals": totals,
                           "dollar_sessions": dollar_sessions}
    if dollar_sessions:
        out["totals"]["ref_usd"] = round(dollars, 6)
    return out


def _session_line(e: Dict[str, Any]) -> str:
    p = e["policy"]
    b = e["billing"]
    usd = money(e.get("ref_usd"), p)
    billing = b["mode"] + (" (set by you)" if b["set_by_user"] else "") + (" (mixed)" if b["mixed"] else "")
    return (f"  {e['session_id'][:8]}  {str(e['last_ts'] or '')[:16]:<16}  {billing:<22} {e['turns']:>5} "
            f"{e['input_tokens']:>9,} {e['cache_write']:>11,} {e['cache_read']:>12,} {e['output_tokens']:>9,} "
            f"{e['billing_units']:>12,}" + (f"  {usd}" if usd else ""))


def render(data: Dict[str, Any], fmt: str = "terminal") -> str:
    if fmt == "json":
        return json.dumps(data, indent=2)
    t = data["totals"]
    lines = ["BudgetPilot Claude Code usage report", "",
             "  session   last turn         billing                 turns     input  cache write   cache read"
             "    output  billing units"]
    lines += [_session_line(e) for e in data["sessions"]]
    lines += ["", f"  total: {t['turns']:,} turns · input {t['input_tokens']:,} · cache write {t['cache_write']:,} · "
                  f"cache read {t['cache_read']:,} · output {t['output_tokens']:,} · billing units {t['billing_units']:,}"]
    if data["dollar_sessions"]:
        lines.append(f"  API-equivalent estimate: ${t['ref_usd']:.4f}, from the {data['dollar_sessions']} session(s) "
                     "billed per token whose billing did not change")
    lines += ["", "  Billing units weight token classes by their relative API price. For sessions on a plan they are "
                  "relative weighting, not dollars, and no dollar figures are shown."]
    text = "\n".join(lines)
    if fmt in ("md", "markdown"):
        return "```\n" + text + "\n```"
    if fmt == "html":
        import html as _html

        return "<pre>" + _html.escape(text) + "</pre>"
    return text

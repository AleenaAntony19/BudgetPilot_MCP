"""
Drift check: does BudgetPilot's dollar figure agree with the host's own?

Claude Code computes a session cost itself, at list price (status line
`cost.total_cost_usd`, `total_cost_usd` in `claude -p --output-format json`;
docs: "computed client-side at list price ... May differ from your actual
bill"). BudgetPilot records that figure next to its own for the same moment.
If they differ by more than 2% the price book may be stale, and dollar
figures are never labelled "reconciled".

What "reconciled" means here: BudgetPilot's estimate agrees with Claude
Code's estimate. Neither is an invoice; `bp reconcile --console` compares with
an exported usage file.

Known causes of a difference other than stale prices [VERIFY]: subagent
calls (separate transcripts), server-side tool fees (web search), US-only
inference (1.1x) and fast mode, none of which the ledger prices.

Standard library only.
"""

from __future__ import annotations

import json
import sqlite3
import time

TOLERANCE = 0.02
SAMPLES = 3


def record(conn: sqlite3.Connection, session_id: str, host_usd, source: str, transcript_path: str = "") -> dict:
    """Store one (host, BudgetPilot) pair for a session, reading new transcript lines first."""
    from budgetpilot.core import cc_ledger

    if transcript_path:
        try:
            cc_ledger.ingest(conn, session_id, transcript_path)
        except sqlite3.Error:
            pass
    totals = cc_ledger.session_totals(conn, session_id=session_id)
    computed = float(totals["ref_usd"] or 0.0)
    host = float(host_usd or 0.0)
    diff = (computed - host) / host if host > 0 else None
    row = conn.execute("SELECT recent_diffs FROM host_costs WHERE session_id=?", (session_id,)).fetchone()
    recent = []
    if row and row[0]:
        try:
            recent = list(json.loads(row[0]))
        except ValueError:
            recent = []
    if diff is not None:
        recent = (recent + [round(diff, 6)])[-SAMPLES:]
    conn.execute(
        "INSERT INTO host_costs (session_id, ts, source, host_usd, computed_usd, diff_pct, recent_diffs, turns, "
        "unpriced) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET ts=excluded.ts, "
        "source=excluded.source, host_usd=excluded.host_usd, computed_usd=excluded.computed_usd, "
        "diff_pct=excluded.diff_pct, recent_diffs=excluded.recent_diffs, turns=excluded.turns, "
        "unpriced=excluded.unpriced",
        (session_id, time.time(), source, host, computed, diff, json.dumps(recent), totals["turns"],
         int(bool(totals["price_guess"]))),
    )
    return status(conn, session_id) or {}


def status(conn: sqlite3.Connection, session_id: str):
    """
    None when there is nothing to compare. Otherwise the latest pair and a
    verdict. A single sample can be off while the transcript catches up, so
    the verdict uses the closest of the last three samples: a stale price
    table is off in all of them.
    """
    if not session_id:
        return None
    try:
        row = conn.execute("SELECT ts, source, host_usd, computed_usd, diff_pct, recent_diffs, unpriced "
                           "FROM host_costs WHERE session_id=?", (session_id,)).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[2]:
        return None
    try:
        recent = [float(d) for d in json.loads(row[5] or "[]")]
    except (ValueError, TypeError):
        recent = []
    closest = min((abs(d) for d in recent), default=None)
    reconciled = closest is not None and closest <= TOLERANCE and not row[6]
    return {"ts": row[0], "source": row[1], "host_usd": row[2], "computed_usd": row[3], "diff_pct": row[4],
            "closest_diff_pct": closest, "reconciled": reconciled,
            "drift": closest is not None and closest > TOLERANCE, "unpriced_models": bool(row[6]),
            # Seen in practice: a 1-turn plan session differed by 3.6% because Claude Code made a side
            # request (e.g. a title) that its transcript does not record. Name both causes.
            "message": None if reconciled else (
                f"BudgetPilot's estimate differs from Claude Code's by {100 * closest:.1f}%: the price table may be "
                "stale (bp prices refresh), or the session made model calls its transcript does not record "
                "(side requests, subagents; bp gateway sees every call)"
                if closest is not None and closest > TOLERANCE else
                "some models are unpriced" if row[6] else None)}


def compare_runs(runs) -> dict:
    """A/B runs: BudgetPilot's dollars against Claude Code's `total_cost_usd` for the same sessions."""
    diffs = []
    for r in runs:
        host, ours = r.get("total_cost_usd_reported"), r.get("ref_usd")
        if host and ours is not None:
            diffs.append(abs(float(ours) - float(host)) / float(host))
    return {"runs": len(diffs), "max_diff_pct": max(diffs) if diffs else None,
            "reconciled": bool(diffs) and max(diffs) <= TOLERANCE}


# ------------------------------------------------------------------ console export
def read_console_export(path: str, model_col: str = None, cost_col: str = None, date_col: str = None):
    """
    Rows (model, usd, date) from an exported usage file (CSV or JSON list of
    objects). The export format is not documented here [VERIFY with a real
    export]: columns are found by name ("model", a "cost"/"usd" column, a
    "date"/"time" column) unless given explicitly.
    """
    import csv

    with open(path, encoding="utf-8") as f:
        text = f.read()
    if path.lower().endswith(".json") or text.lstrip()[:1] in "[{":
        data = json.loads(text)
        records = data if isinstance(data, list) else data.get("data") or data.get("rows") or []
    else:
        records = list(csv.DictReader(text.splitlines()))
    if not records:
        raise ValueError("no rows in the export")
    columns = list(records[0].keys())

    def pick(given, *needles):
        if given:
            if given not in columns:
                raise ValueError(f"column {given!r} not in {columns}")
            return given
        found = [c for c in columns if any(n in c.lower() for n in needles)]
        if len(found) != 1:
            raise ValueError(f"cannot tell which column holds {needles[0]} ({found or columns}); "
                             f"pass --{needles[0]}-col")
        return found[0]

    mcol, ccol = pick(model_col, "model"), pick(cost_col, "cost", "usd", "amount")
    dcol = None
    try:
        dcol = pick(date_col, "date", "time", "day")
    except ValueError:
        if date_col:
            raise
    out = []
    for rec in records:
        try:
            usd = float(str(rec[ccol]).replace("$", "").replace(",", "") or 0)
        except ValueError:
            continue
        out.append((str(rec[mcol]), usd, str(rec[dcol])[:10] if dcol else None))
    return out


def reconcile_console(conn: sqlite3.Connection, rows) -> dict:
    """Per-model dollars: exported file against BudgetPilot's ledger for the same dates."""
    from budgetpilot.core import cc_ledger

    dates = sorted(d for _, _, d in rows if d)
    since, until = (dates[0], dates[-1]) if dates else (None, None)
    clause, params = "", []
    if since:
        clause, params = "WHERE substr(ts, 1, 10) BETWEEN ? AND ?", [since, until]
    ledger = {}
    for t in cc_ledger.priced_rows(conn.execute(
            "SELECT input_tokens, cache_write_5m, cache_write_1h, cache_read, output_tokens, thinking_tokens, model, "
            f"ts, COALESCE(provider, 'anthropic') FROM cc_turns {clause}", params).fetchall()):
        ledger[t["model"]] = ledger.get(t["model"], 0.0) + float(t["ref_usd"] or 0.0)
    exported = {}
    for model, usd, _ in rows:
        exported[model] = exported.get(model, 0.0) + usd
    models = sorted(set(ledger) | set(exported))
    per_model = []
    for m in models:
        a, b = exported.get(m, 0.0), ledger.get(m, 0.0)
        per_model.append({"model": m, "exported_usd": round(a, 6), "budgetpilot_usd": round(b, 6),
                          "diff_pct": round((b - a) / a * 100, 2) if a else None})
    return {"since": since, "until": until, "per_model": per_model,
            "note": "The export covers every client of the account; the ledger only Claude Code sessions "
                    "BudgetPilot saw, so a model can differ for reasons other than prices."}

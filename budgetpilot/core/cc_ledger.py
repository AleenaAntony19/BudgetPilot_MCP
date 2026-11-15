"""
Token ledger built from Claude Code transcripts.

Claude Code writes every API message to the session transcript (`.jsonl`)
with the usage the API reported. This module reads those rows, keeps one row
per `message.id` (a message is written several times, once per content block)
and stores them in `cc_turns`. On Claude Code 2.1.325 the de-duplicated totals
matched `claude -p --output-format json` exactly (see docs/HOOK_CONTRACT.md).

Two derived numbers:
- billing_units: input-token equivalents,
      input + 1.25*cache_write_5m + 2*cache_write_1h + r*cache_read + 5*output
  where r is the model's cache-read multiplier. Comparable across runs of the
  same model.
- ref_usd: billing_units * input price / 1e6. API-equivalent only: a Pro or
  Max plan is not billed per token.

The ledger is diagnostic. Savings are only ever claimed from the paired A/B
test (`bp ab`).

Standard library only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import statistics

from budgetpilot.core.state import get_bp_home

OUTPUT_MULTIPLIER = 5.0
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0

# (model id prefix, input USD per million tokens, cache-read multiplier).
# First matching prefix wins, so more specific ids come first. Output is 5x
# input, 5-minute cache writes 1.25x and 1-hour cache writes 2x on every row.
# Source: https://platform.claude.com/docs/en/about-claude/pricing, read on
# 2026-11-15. The Haiku 5.5 row also reproduces the `total_cost_usd` Claude
# Code reported for a real run to 8 decimal places. Override or extend under
# "claude_models" in $BP_HOME/prices.json.
DEFAULT_MODEL_PRICES: List[Tuple[str, float, float]] = [
    ("claude-fable-5-2", 8.00, 0.025),
    ("claude-mythos-5-2", 8.00, 0.025),
    ("claude-fable-5-1", 10.00, 0.025),
    ("claude-mythos-5-1", 10.00, 0.025),
    ("claude-fable-5", 10.00, 0.10),
    ("claude-mythos-5", 10.00, 0.10),
    ("claude-opus-5-5", 4.00, 0.05),
    ("claude-opus-5", 5.00, 0.10),
    ("claude-opus-4", 5.00, 0.10),
    ("claude-sonnet-5-5", 2.00, 0.05),
    ("claude-sonnet-5", 2.00, 0.10),
    ("claude-sonnet-4", 3.00, 0.10),
    ("claude-haiku-5-5", 0.10, 0.10),
    ("claude-haiku-4-5", 1.00, 0.10),
]
FALLBACK_PREFIX = "claude-sonnet-5-5"

# Haiku 5.5 is priced 5x higher for prompts over 100,000 tokens.
LONG_PROMPT_RULES = {"claude-haiku-5-5": (100_000, 5.0)}


def model_prices() -> List[Tuple[str, float, float]]:
    """Default prices with any user overrides from prices.json applied first."""
    prices: List[Tuple[str, float, float]] = []
    try:
        data = json.loads((get_bp_home() / "prices.json").read_text(encoding="utf-8"))
        for prefix, row in (data.get("claude_models") or {}).items():
            prices.append((str(prefix), float(row["input_per_million"]), float(row.get("cache_read_multiplier", 0.1))))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return prices + DEFAULT_MODEL_PRICES


def price_for(model: Optional[str], prices: Optional[List[Tuple[str, float, float]]] = None) -> Tuple[float, float, bool]:
    """(input USD per million, cache-read multiplier, guessed). Unknown models use the Sonnet row."""
    table = prices if prices is not None else model_prices()
    name = str(model or "")
    for prefix, input_price, read_mult in table:
        if name.startswith(prefix):
            return input_price, read_mult, False
    for prefix, input_price, read_mult in table:
        if prefix == FALLBACK_PREFIX:
            return input_price, read_mult, True
    return 2.00, 0.05, True


def parse_usage(usage: Dict[str, Any]) -> Dict[str, int]:
    """Token counts by type from a `message.usage` object."""
    def num(value: Any) -> int:
        return int(value) if isinstance(value, (int, float)) else 0

    creation = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    cache_write = num(usage.get("cache_creation_input_tokens"))
    cw_1h = num(creation.get("ephemeral_1h_input_tokens"))
    cw_5m = num(creation.get("ephemeral_5m_input_tokens"))
    if cw_1h + cw_5m != cache_write:  # no split reported: treat as 5-minute writes
        cw_5m, cw_1h = max(0, cache_write - cw_1h), cw_1h
    details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    return {
        "input_tokens": num(usage.get("input_tokens")),
        "cache_write_5m": cw_5m,
        "cache_write_1h": cw_1h,
        "cache_read": num(usage.get("cache_read_input_tokens")),
        "output_tokens": num(usage.get("output_tokens")),
        "thinking_tokens": num(details.get("thinking_tokens")),  # already inside output_tokens
    }


def billing_units(tokens: Dict[str, int], read_multiplier: float) -> float:
    return (
        tokens["input_tokens"]
        + CACHE_WRITE_5M_MULTIPLIER * tokens["cache_write_5m"]
        + CACHE_WRITE_1H_MULTIPLIER * tokens["cache_write_1h"]
        + read_multiplier * tokens["cache_read"]
        + OUTPUT_MULTIPLIER * tokens["output_tokens"]
    )


def _turn_from_row(row: Dict[str, Any], task_id: Optional[str],
                   prices: List[Tuple[str, float, float]]) -> Optional[Dict[str, Any]]:
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    usage = message.get("usage")
    if row.get("type") != "assistant" or not isinstance(usage, dict) or not message.get("id"):
        return None
    tokens = parse_usage(usage)
    input_price, read_mult, guessed = price_for(message.get("model"), prices)
    prompt_tokens = tokens["input_tokens"] + tokens["cache_write_5m"] + tokens["cache_write_1h"] + tokens["cache_read"]
    for prefix, (threshold, factor) in LONG_PROMPT_RULES.items():
        if str(message.get("model") or "").startswith(prefix) and prompt_tokens > threshold:
            input_price *= factor
    units = billing_units(tokens, read_mult)
    tools = [
        block.get("name") for block in (message.get("content") or [])
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name")
    ] if isinstance(message.get("content"), list) else []
    return {
        "message_id": str(message["id"]), "task_id": task_id, "ts": row.get("timestamp"),
        "model": message.get("model"), "effort": row.get("effort") if isinstance(row.get("effort"), str) else None,
        **tokens,
        "context_tokens": tokens["input_tokens"] + tokens["cache_write_5m"] + tokens["cache_write_1h"]
        + tokens["cache_read"],
        "tools_called": tools, "billing_units": units, "ref_usd": units * input_price / 1e6,
        "price_guess": int(guessed),
    }


def read_turns(path: str, offset: int = 0, prompt_id: Optional[str] = None) -> Tuple[List[Dict[str, Any]], int, Optional[str]]:
    """
    Parse transcript bytes from `offset`. Returns (turns, new offset, current
    prompt id). One turn per message id, keeping the row with the largest
    output count. A last line without a newline is still being written, so it
    is left for the next read.
    """
    size = os.path.getsize(path)
    if size < offset:  # file was rewritten or rotated: rescan; the primary key absorbs repeats
        offset, prompt_id = 0, None
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset, prompt_id
    prices = model_prices()
    best: Dict[str, Dict[str, Any]] = {}
    for line in data[: end + 1].split(b"\n"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("type") == "user" and row.get("promptId"):
            prompt_id = str(row["promptId"])
            continue
        turn = _turn_from_row(row, prompt_id, prices)
        if turn is None:
            continue
        current = best.get(turn["message_id"])
        if current is None:
            best[turn["message_id"]] = turn
        else:
            tools = current["tools_called"] + [t for t in turn["tools_called"] if t not in current["tools_called"]]
            if turn["output_tokens"] >= current["output_tokens"]:
                turn["task_id"] = current["task_id"] or turn["task_id"]
                best[turn["message_id"]] = turn
            best[turn["message_id"]]["tools_called"] = tools
    return list(best.values()), offset + end + 1, prompt_id


_UPSERT = (
    "INSERT INTO cc_turns (message_id, session_id, task_id, ts, model, effort, input_tokens, cache_write_5m, "
    "cache_write_1h, cache_read, output_tokens, thinking_tokens, context_tokens, tools_called, billing_units, "
    "ref_usd, price_guess) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(message_id) DO UPDATE SET input_tokens=excluded.input_tokens, "
    "cache_write_5m=excluded.cache_write_5m, cache_write_1h=excluded.cache_write_1h, "
    "cache_read=excluded.cache_read, output_tokens=excluded.output_tokens, "
    "thinking_tokens=excluded.thinking_tokens, context_tokens=excluded.context_tokens, "
    "billing_units=excluded.billing_units, ref_usd=excluded.ref_usd, "
    "tools_called=CASE WHEN excluded.tools_called='[]' THEN cc_turns.tools_called ELSE excluded.tools_called END "
    "WHERE excluded.output_tokens >= cc_turns.output_tokens"
)


def ingest(conn: sqlite3.Connection, session_id: str, path: str) -> int:
    """
    Read new transcript bytes for a session into `cc_turns`. The cursor only
    advances when every row was written, so a locked database or a lagging
    file is simply caught up by the next call. Returns the turns written.
    """
    if not path or not os.path.isfile(path):
        return 0
    row = conn.execute(
        "SELECT byte_offset, prompt_id FROM transcript_cursor WHERE session_id=?", (session_id,)
    ).fetchone()
    offset, prompt_id = (int(row[0] or 0), row[1]) if row else (0, None)
    turns, new_offset, new_prompt = read_turns(path, offset, prompt_id)
    if new_offset == offset and not turns:
        return 0
    conn.execute("BEGIN IMMEDIATE;")
    try:
        for t in turns:
            conn.execute(_UPSERT, (
                t["message_id"], session_id, t["task_id"], t["ts"], t["model"], t["effort"], t["input_tokens"],
                t["cache_write_5m"], t["cache_write_1h"], t["cache_read"], t["output_tokens"], t["thinking_tokens"],
                t["context_tokens"], json.dumps(t["tools_called"]), t["billing_units"], t["ref_usd"], t["price_guess"],
            ))
        conn.execute(
            "INSERT INTO transcript_cursor (session_id, byte_offset, prompt_id) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET byte_offset=excluded.byte_offset, prompt_id=excluded.prompt_id",
            (session_id, new_offset, new_prompt),
        )
        conn.execute("COMMIT;")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK;")
        except sqlite3.Error:
            pass
        raise
    return len(turns)


# ------------------------------------------------------------------ summaries
TOKEN_FIELDS = ("input_tokens", "cache_write_5m", "cache_write_1h", "cache_read", "output_tokens", "thinking_tokens")


def totals(turns: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {name: 0 for name in TOKEN_FIELDS}
    out.update(turns=0, billing_units=0.0, ref_usd=0.0, price_guess=False, models=[])
    for t in turns:
        out["turns"] += 1
        for name in TOKEN_FIELDS:
            out[name] += int(t.get(name) or 0)
        out["billing_units"] += float(t.get("billing_units") or 0.0)
        out["ref_usd"] += float(t.get("ref_usd") or 0.0)
        out["price_guess"] = out["price_guess"] or bool(t.get("price_guess"))
        if t.get("model") and t["model"] not in out["models"]:
            out["models"].append(t["model"])
    out["cache_write"] = out["cache_write_5m"] + out["cache_write_1h"]
    out["cache_hit_ratio"] = cache_hit_ratio(out["input_tokens"], out["cache_write"], out["cache_read"])
    return out


def cache_hit_ratio(input_tokens: float, cache_write: float, cache_read: float) -> Optional[float]:
    denominator = input_tokens + cache_write + cache_read
    return round(cache_read / denominator, 4) if denominator else None


def summarize_transcript(path: str) -> Dict[str, Any]:
    """Whole-file totals without a database (used by `bp ab` and `bp transcript-check`)."""
    turns, _, _ = read_turns(path, 0, None)
    return totals(turns)


def check_transcript(path: str) -> Dict[str, Any]:
    """Field coverage report for `bp transcript-check`: what this transcript actually contains."""
    rows = assistant = with_usage = without_usage = with_thinking = with_split = bad = 0
    ids: Dict[str, int] = {}
    with open(path, "rb") as f:
        for line in f:
            if not line.strip():
                continue
            rows += 1
            try:
                row = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if not isinstance(row, dict) or row.get("type") != "assistant":
                continue
            assistant += 1
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            usage = message.get("usage")
            if not isinstance(usage, dict) or not message.get("id"):
                without_usage += 1
                continue
            with_usage += 1
            ids[message["id"]] = ids.get(message["id"], 0) + 1
            if isinstance(usage.get("output_tokens_details"), dict) and "thinking_tokens" in usage["output_tokens_details"]:
                with_thinking += 1
            if isinstance(usage.get("cache_creation"), dict):
                with_split += 1
    summary = summarize_transcript(path)
    return {
        "path": path, "rows": rows, "unparsable_rows": bad, "assistant_rows": assistant,
        "assistant_rows_with_usage": with_usage, "assistant_rows_without_usage": without_usage,
        "unique_messages": len(ids), "max_rows_per_message": max(ids.values()) if ids else 0,
        "rows_with_thinking_tokens": with_thinking, "rows_with_cache_write_split": with_split,
        "totals": {k: summary[k] for k in ("turns", "input_tokens", "cache_write", "cache_read", "output_tokens",
                                           "thinking_tokens", "cache_hit_ratio")},
        "billing_units": round(summary["billing_units"]), "ref_usd": round(summary["ref_usd"], 6),
        "price_guess": summary["price_guess"], "models": summary["models"],
    }


def session_totals(conn: sqlite3.Connection, session_id: Optional[str] = None,
                   since_ts: Optional[str] = None, task_id: Optional[str] = None) -> Dict[str, Any]:
    where, params = [], []
    for column, value, op in (("session_id", session_id, "="), ("ts", since_ts, ">="), ("task_id", task_id, "=")):
        if value:
            where.append(f"{column} {op} ?")
            params.append(value)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"SELECT input_tokens, cache_write_5m, cache_write_1h, cache_read, output_tokens, thinking_tokens, "
        f"billing_units, ref_usd, price_guess, model FROM cc_turns {clause}", params,
    ).fetchall()
    keys = ("input_tokens", "cache_write_5m", "cache_write_1h", "cache_read", "output_tokens", "thinking_tokens",
            "billing_units", "ref_usd", "price_guess", "model")
    return totals(dict(zip(keys, row)) for row in rows)


def cache_series(conn: sqlite3.Connection, session_id: str, limit: int = 60) -> Dict[str, Any]:
    """Per-turn cache-hit ratio and the turns where it fell by more than 20 points."""
    rows = conn.execute(
        "SELECT ts, input_tokens, cache_write_5m + cache_write_1h, cache_read FROM cc_turns WHERE session_id=? "
        "ORDER BY ts DESC LIMIT ?", (session_id, limit),
    ).fetchall()
    series = [cache_hit_ratio(r[1], r[2], r[3]) or 0.0 for r in reversed(rows)]
    drops = [i for i in range(1, len(series)) if series[i - 1] - series[i] > 0.20]
    return {"ratios": series, "drops": drops}


def calibration(conn: sqlite3.Connection, session_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Estimated versus derived-actual tool-result tokens per task.

    Derived (not measured): context growth between consecutive API messages of
    a task, minus the previous message's output, summed over the task. It also
    contains BudgetPilot's own notes, so the ratio is a rough guide used only
    to tune `chars_per_token`.
    """
    clause, params = ("WHERE session_id=?", [session_id]) if session_id else ("", [])
    rows = conn.execute(
        f"SELECT task_id, context_tokens, output_tokens FROM cc_turns {clause} ORDER BY task_id, ts", params
    ).fetchall()
    derived: Dict[str, int] = {}
    previous: Dict[str, Tuple[int, int]] = {}
    for task_id, context, output in rows:
        if not task_id:
            continue
        if task_id in previous:
            growth = int(context or 0) - previous[task_id][0] - previous[task_id][1]
            if growth > 0:
                derived[task_id] = derived.get(task_id, 0) + growth
        previous[task_id] = (int(context or 0), int(output or 0))
    ratios = []
    for task_id, actual in derived.items():
        est = conn.execute("SELECT COALESCE(SUM(result_tokens_est - CASE WHEN truncated THEN trimmed_tokens_est "
                           "ELSE 0 END), 0) FROM tool_obs WHERE task_id=?", (task_id,)).fetchone()[0]
        if est and actual >= 200:
            ratios.append(est / actual)
    if not ratios:
        return {"tasks": 0, "ratio": None, "spread": None, "ok": None}
    ratio = statistics.median(ratios)
    spread = statistics.pstdev(ratios) if len(ratios) > 1 else 0.0
    return {"tasks": len(ratios), "ratio": round(ratio, 3), "spread": round(spread, 3),
            "ok": abs(ratio - 1.0) <= 0.25, "label": "derived"}


def newest_transcript(base: Optional[Path] = None) -> Optional[str]:
    """Most recently modified transcript under ~/.claude/projects (or $CLAUDE_CONFIG_DIR)."""
    root = base or Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    try:
        files = [p for p in root.glob("*/*.jsonl") if p.is_file()]
        return str(max(files, key=lambda p: p.stat().st_mtime)) if files else None
    except OSError:
        return None

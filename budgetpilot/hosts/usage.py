"""
Normalised usage: turn any adapter's UsageEvents into the ledger's disjoint
token classes and store them in `cc_turns`.

The ledger's classes do not overlap (Anthropic's convention):

    input_tokens     prompt tokens that were neither read from nor written to a cache
    cache_write_5m   prompt tokens written to a 5-minute cache
    cache_write_1h   prompt tokens written to a 1-hour cache
    cache_read       prompt tokens read from a cache
    output_tokens    all generated tokens, reasoning included
    thinking_tokens  the reasoning part of output_tokens (informational)

Providers differ. Some report cached tokens as a subset of the prompt count,
and some report reasoning tokens on top of the output count. The event's
`semantics` flags say which, and `canonical_tokens` removes the overlap. Every
total and billing unit is computed from the canonical classes.

Tokens are the source of truth. Billing units and dollars come from the
price book (core/pricebook.py); reports recompute them from tokens, and a model
without a cited price stays unpriced instead of being guessed.

Standard library only.
"""

from __future__ import annotations

import json
import sqlite3

from budgetpilot.hosts.base import ANTHROPIC_SEMANTICS


def _n(value) -> int:
    return int(value) if isinstance(value, (int, float)) and value > 0 else 0


def canonical_tokens(event) -> dict:
    """Disjoint token classes for one event (see the module docstring)."""
    semantics = event.semantics if isinstance(event.semantics, dict) else ANTHROPIC_SEMANTICS
    cw5, cw1, cr = _n(event.cache_write_5m), _n(event.cache_write_1h), _n(event.cache_read)
    inp, out, reasoning = _n(event.input_tokens), _n(event.output_tokens), _n(event.reasoning_tokens)
    if semantics.get("cached_is_subset_of_input"):
        inp = max(0, inp - cw5 - cw1 - cr)
    if not semantics.get("reasoning_is_subset_of_output", True):
        out += reasoning
    return {
        "input_tokens": inp, "cache_write_5m": cw5, "cache_write_1h": cw1, "cache_read": cr,
        "output_tokens": out, "thinking_tokens": min(reasoning, out),
    }


def priced(event, tokens: dict, prices=None) -> tuple:
    """(billing units or None, API-equivalent USD or None, unpriced) from the price book at the event's time."""
    from budgetpilot.core.cc_ledger import price_tokens

    return price_tokens(event.model, tokens, ts=event.ts, provider=event.provider or "unknown")


_UPSERT = (
    "INSERT INTO cc_turns (message_id, session_id, task_id, ts, model, effort, input_tokens, cache_write_5m, "
    "cache_write_1h, cache_read, output_tokens, thinking_tokens, context_tokens, tools_called, billing_units, "
    "ref_usd, price_guess, host, provider, source) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(message_id) DO UPDATE SET input_tokens=excluded.input_tokens, "
    "cache_write_5m=excluded.cache_write_5m, cache_write_1h=excluded.cache_write_1h, "
    "cache_read=excluded.cache_read, output_tokens=excluded.output_tokens, "
    "thinking_tokens=excluded.thinking_tokens, context_tokens=excluded.context_tokens, "
    "billing_units=excluded.billing_units, ref_usd=excluded.ref_usd, "
    "tools_called=CASE WHEN excluded.tools_called='[]' THEN cc_turns.tools_called ELSE excluded.tools_called END "
    "WHERE excluded.output_tokens >= cc_turns.output_tokens"
)


def ledger_row(event, prices=None) -> tuple:
    tokens = canonical_tokens(event)
    units, usd, guessed = priced(event, tokens, prices)  # cache of the price at ingest; reports re-price from tokens
    extra = event.extra if isinstance(event.extra, dict) else {}
    context = tokens["input_tokens"] + tokens["cache_write_5m"] + tokens["cache_write_1h"] + tokens["cache_read"]
    return (
        str(event.message_id), str(event.session_id), event.task_id, event.ts, event.model,
        extra.get("effort") if isinstance(extra.get("effort"), str) else None,
        tokens["input_tokens"], tokens["cache_write_5m"], tokens["cache_write_1h"], tokens["cache_read"],
        tokens["output_tokens"], tokens["thinking_tokens"], context,
        json.dumps(list(extra.get("tools_called") or [])), units, usd, int(bool(guessed)),
        event.host, event.provider, event.source,
    )


def store_events(conn: sqlite3.Connection, events) -> int:
    """Upsert events into `cc_turns` (no transaction handling; see `ingest`). Returns rows written."""
    prices = None
    count = 0
    for event in events:
        if not event.message_id or not event.session_id:
            continue  # without a dedupe key the write would not be idempotent
        if prices is None and event.provider == "anthropic":
            from budgetpilot.core.cc_ledger import model_prices

            prices = model_prices()
        conn.execute(_UPSERT, ledger_row(event, prices))
        count += 1
    return count


def ingest(conn: sqlite3.Connection, adapter, key: str, path: str = "") -> int:
    """
    Read an adapter's new usage for one stream into the ledger. Events and the
    advanced cursor are committed together, so a failure re-reads from the old
    position and the message-id key absorbs the repeats. Returns rows written.
    """
    cursor = adapter.load_cursor(conn, key, path)
    start = (cursor.position, cursor.state)
    events = list(adapter.iter_usage(cursor))
    if not events and (cursor.position, cursor.state) == start:
        return 0
    conn.execute("BEGIN IMMEDIATE;")
    try:
        written = store_events(conn, events)
        adapter.save_cursor(conn, cursor)
        conn.execute("COMMIT;")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK;")
        except sqlite3.Error:
            pass
        raise
    return written

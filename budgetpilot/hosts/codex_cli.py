"""
Codex CLI importer (tier `observe`): token usage read from Codex's session
logs. BudgetPilot cannot control Codex; it only measures it.

EXPERIMENTAL. No real Codex log was available when this was written. The
format below is taken from Codex's own source (github.com/openai/codex, main at
6eea328, 2026-11-19), not from a captured file. Use `bp capture codex` to save
a redacted sample from a real session and check it.

Format (from the source):
- Files: $CODEX_HOME (default ~/.codex)/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl,
  also archived_sessions/. Files older than 7 days may be compressed to
  .jsonl.zst, which the standard library cannot read: they are skipped and
  listed (codex-rs/rollout/src/compression.rs).
- Each line: {"timestamp": ..., "ordinal"?: ..., "type": <item type>, "payload": {...}}
  (codex-rs/history/src/lib.rs RolloutLine, rollout_payload.rs RolloutItemWire,
  tag "type", snake_case).
- "session_meta": payload.id (thread id), payload.model_provider, payload.cli_version.
- "turn_context": payload.turn_id, payload.model.
- "token_usage_record": one per completed model response: payload.response_id,
  payload.turn_id, payload.usage = TokenUsage. Used when present.
- "event_msg" with payload.type "token_count": payload.info.total_token_usage
  (cumulative TokenUsage). Used only for files without token_usage_record:
  each increase of the cumulative total becomes one record.
- TokenUsage: input_tokens, cached_input_tokens, cache_write_input_tokens,
  output_tokens, reasoning_output_tokens, total_tokens. Codex computes
  non-cached input as input_tokens - cached_input_tokens, and counts
  reasoning inside output (protocol.rs), so cached and reasoning tokens are
  subsets. [VERIFY] cache_write_input_tokens is treated as part of
  input_tokens and as a 5-minute write.

Billing is not recorded in the logs (Codex can use a ChatGPT login or an API
key), so imported sessions are `unknown` (no dollars) unless
billing.codex_cli.mode_override is set in config.json. OpenAI models are
unpriced until `bp prices set openai MODEL ...`.

Standard library only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

from budgetpilot.hosts.base import BillingContext, Cursor, HostAdapter, HostCapabilities, UsageEvent

SEMANTICS = {"cached_is_subset_of_input": True, "reasoning_is_subset_of_output": True}
FORMAT_SOURCE = "github.com/openai/codex@6eea328 (2026-11-19)"
_THREAD = re.compile(r"rollout-.*?-([0-9a-fA-F-]{36})(?:_[^.]*)?\.jsonl$")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def discover(home: Path = None):
    """(readable .jsonl files, compressed .jsonl.zst files that cannot be read)."""
    home = home or codex_home()
    plain, compressed = [], []
    for sub in ("sessions", "archived_sessions"):
        root = home / sub
        if not root.is_dir():
            continue
        plain += sorted(root.rglob("rollout-*.jsonl"))
        compressed += sorted(root.rglob("rollout-*.jsonl.zst"))
    return plain, compressed


def _n(usage, key) -> int:
    v = usage.get(key) if isinstance(usage, dict) else None
    return int(v) if isinstance(v, (int, float)) and v > 0 else 0


def _usage_fields(usage) -> dict:
    return {"input": _n(usage, "input_tokens"), "cached": _n(usage, "cached_input_tokens"),
            "cache_write": _n(usage, "cache_write_input_tokens"), "output": _n(usage, "output_tokens"),
            "reasoning": _n(usage, "reasoning_output_tokens"), "total": _n(usage, "total_tokens")}


class CodexCliAdapter(HostAdapter):
    id = "codex_cli"
    name = "Codex CLI (imported session logs, experimental)"
    tier = "observe"
    provider = "openai"

    open_items = (
        "Log format: from Codex's source, not a real log (send a `bp capture codex` sample)",
        "cache_write_input_tokens: treated as part of input and as 5-minute writes",
    )

    def capabilities(self) -> HostCapabilities:
        return HostCapabilities(
            tier=self.tier,
            usage_source="import",
            usage_exact=True,
            limitations=(
                "Measured only, no control: BudgetPilot cannot see or change Codex's tool calls.",
                "Imported after the fact with `bp import codex`, not live.",
                "Experimental: built from Codex's source, not yet checked against a real log.",
                "Billing is not in the logs, so no dollar figures unless you set billing.codex_cli.mode_override.",
            ),
        )

    def detect_billing(self, env, ctx=None) -> BillingContext:
        cfg = (ctx or {}).get("config") or {}
        section = ((cfg.get("billing") or {}).get("codex_cli") or {}) if isinstance(cfg, dict) else {}
        mode = section.get("mode_override") if isinstance(section, dict) else None
        if mode in ("subscription", "api", "cloud", "unknown"):
            return BillingContext(mode, section.get("provider_override") or "openai", "high",
                                  (f"config:billing.codex_cli.mode_override={mode}",), "user_override")
        return BillingContext("unknown", "openai", "low", ("imported Codex log: billing is not recorded",), "detected")

    def iter_usage(self, since: Cursor):
        """New usage in the rollout file `since.path` after byte `since.position`; advances the cursor."""
        path = since.path
        if not path or not os.path.isfile(path) or path.endswith(".zst"):
            return
        state = {}
        if since.state:
            try:
                state = json.loads(since.state)
            except ValueError:
                state = {}
        size = os.path.getsize(path)
        offset = int(since.position or 0)
        if size < offset:  # rewritten: start again; response ids absorb repeats
            offset, state = 0, {}
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return
        match = _THREAD.search(os.path.basename(path))
        state.setdefault("session", match.group(1) if match else os.path.basename(path))
        state.setdefault("provider", "openai")
        position = offset
        for raw in data[: end + 1].split(b"\n"):
            line_start = position
            position += len(raw) + 1
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            kind, payload = row.get("type"), row.get("payload")
            if not isinstance(payload, dict):
                continue
            if kind == "session_meta":
                state["session"] = str(payload.get("id") or payload.get("session_id") or state["session"])
                state["provider"] = str(payload.get("model_provider") or state["provider"])
            elif kind == "turn_context":
                if payload.get("model"):
                    state["model"] = str(payload["model"])
                    if payload.get("turn_id"):
                        state.setdefault("turn_models", {})[str(payload["turn_id"])] = state["model"]
            elif kind == "token_usage_record" and payload.get("response_id"):
                state["records"] = True
                turn = str(payload.get("turn_id") or "")
                yield self._event(state, str(payload["response_id"]), _usage_fields(payload.get("usage")),
                                  row.get("timestamp"), turn or None,
                                  (state.get("turn_models") or {}).get(turn) or state.get("model"),
                                  f"{os.path.basename(path)}@{line_start}")
            elif kind == "event_msg" and payload.get("type") == "token_count" and not state.get("records"):
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                total = _usage_fields(info.get("total_token_usage"))
                previous = state.get("last_total") or {k: 0 for k in total}
                delta = {k: max(0, total[k] - previous.get(k, 0)) for k in total}
                if total["total"] > previous.get("total", 0):
                    state["last_total"] = total
                    yield self._event(state, f"codex:{state['session']}:t{total['total']}", delta,
                                      row.get("timestamp"), None, state.get("model"),
                                      f"{os.path.basename(path)}@{line_start}")
        since.position = offset + end + 1
        since.state = json.dumps(state)

    def _event(self, state, message_id, f, ts, turn, model, raw_ref) -> UsageEvent:
        return UsageEvent(
            session_id=state["session"], host=self.id, provider=state.get("provider") or "openai", model=model,
            ts=ts, message_id=message_id,
            input_tokens=f["input"], cache_write_5m=f["cache_write"], cache_write_1h=0, cache_read=f["cached"],
            output_tokens=f["output"], reasoning_tokens=f["reasoning"],
            semantics=SEMANTICS, source="import", raw_ref=raw_ref, task_id=turn,
        )


def import_all(conn, paths=None, config=None) -> dict:
    """Import every readable rollout file (or `paths`) into the ledger. Idempotent and incremental."""
    from budgetpilot.core import hookdb
    from budgetpilot.hosts.billing import to_row
    from budgetpilot.hosts.usage import ingest

    adapter = CodexCliAdapter()
    if paths is None:
        paths, compressed = discover()
    else:
        paths, compressed = [p for p in map(Path, paths) if not str(p).endswith(".zst")], \
            [p for p in map(Path, paths) if str(p).endswith(".zst")]
    report = {"files": len(paths), "events": 0, "sessions": set(), "compressed_skipped": [str(p) for p in compressed]}
    billing = adapter.detect_billing({}, {"config": config})
    for path in paths:
        written = ingest(conn, adapter, str(path), str(path))
        report["events"] += written
        cursor = adapter.load_cursor(conn, str(path))
        session = json.loads(cursor.state or "{}").get("session") if cursor.state else None
        if not session:
            continue
        report["sessions"].add(session)
        hookdb.touch_session(conn, session, "", "", adapter.id)
        conn.execute("UPDATE cc_sessions SET host_adapter=? WHERE session_id=? AND host=?",
                     (adapter.id, session, adapter.id))
        if not hookdb.session_billing(conn, session).get("billing_mode"):
            hookdb.record_billing(conn, session, to_row(billing))
    report["sessions"] = sorted(report["sessions"])
    return report


# ------------------------------------------------------------------ redacted sample (`bp capture codex`)
_KEEP = {
    "session_meta": ("id", "session_id", "cli_version", "model_provider", "source", "originator"),
    "turn_context": ("turn_id", "model"),
}


def redacted_sample(path: str, max_lines: int = 400) -> dict:
    """
    A shareable sample of one rollout file: item types in order, ids, model
    names and token numbers. Messages, tool calls, instructions, paths and
    git details are dropped; payloads of other item types are reduced to
    their type name.
    """
    lines, counts = [], {}
    with open(path, "rb") as f:
        for i, raw in enumerate(f):
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            kind = row.get("type") if isinstance(row, dict) else None
            counts[kind] = counts.get(kind, 0) + 1
            if i >= max_lines:
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if kind in _KEEP:
                kept = {k: payload.get(k) for k in _KEEP[kind] if k in payload}
            elif kind == "token_usage_record":
                kept = {k: payload.get(k) for k in ("response_id", "turn_id", "usage", "turn_token_usage",
                                                     "thread_token_usage") if k in payload}
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                kept = {"type": "token_count", "info": {k: info.get(k) for k in
                                                        ("total_token_usage", "last_token_usage",
                                                         "model_context_window") if k in info}}
            elif kind == "event_msg":
                kept = {"type": payload.get("type")}
            else:
                kept = None
            lines.append({"timestamp": row.get("timestamp"), "type": kind, "payload": kept})
    return {"_meta": {"source": "bp capture codex", "file": os.path.basename(path), "format_source": FORMAT_SOURCE,
                      "line_types": counts}, "lines": lines}

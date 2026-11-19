"""Host adapters (Phase A): interface, normalised usage, Claude Code wrapper, generic MCP, migration 5."""

import sqlite3
import subprocess
import sys

import pytest

from budgetpilot.core import cc_ledger, hookdb
from budgetpilot.core.store import LATEST_SCHEMA_VERSION, _migration_5_hosts, get_current_schema_version, init_db
from budgetpilot.hosts import REGISTRY, all_adapters, get_adapter
from budgetpilot.hosts.base import ANTHROPIC_SEMANTICS, TIERS, Cursor, UsageEvent
from budgetpilot.hosts.usage import canonical_tokens, ingest, store_events
from tests.hookutil import TRANSCRIPT

# What `claude -p --output-format json` reported for the session in the fixture transcript.
REPORTED = {"input_tokens": 24, "cache_write": 35929, "cache_read": 465598, "output_tokens": 2003,
            "thinking_tokens": 213, "total_cost_usd": 0.012845680000000003}
LEDGER_COLUMNS = ("message_id", "session_id", "task_id", "ts", "model", "effort", "input_tokens", "cache_write_5m",
                  "cache_write_1h", "cache_read", "output_tokens", "thinking_tokens", "context_tokens",
                  "tools_called", "billing_units", "ref_usd", "price_guess")


def _conn(tmp_path, monkeypatch, name="home"):
    monkeypatch.setenv("BP_HOME", str(tmp_path / name))
    return hookdb.open_ready()


def _event(**kw):
    base = dict(session_id="s", host="test", provider="other", model="m", ts="2026-11-19T00:00:00Z",
                message_id="msg-1", input_tokens=0, cache_write_5m=0, cache_write_1h=0, cache_read=0,
                output_tokens=0, reasoning_tokens=0, semantics=ANTHROPIC_SEMANTICS, source="gateway", raw_ref="r")
    base.update(kw)
    return UsageEvent(**base)


# ---------------------------------------------------------------- registry and capabilities
def test_registry_lists_adapters_with_valid_tiers():
    adapters = all_adapters()
    assert [a.id for a in adapters] == list(REGISTRY) == ["claude_code", "gateway", "generic_mcp", "codex_cli"]
    for adapter in adapters:
        assert adapter.tier in TIERS and adapter.capabilities().tier == adapter.tier
        assert adapter.describe()["tier_label"]
    with pytest.raises(KeyError):
        get_adapter("nope")


def test_capabilities_never_claim_more_than_the_tier_allows():
    for adapter in all_adapters():
        caps = adapter.capabilities()
        if adapter.tier != "enforce":
            assert not (caps.intercept_tool_calls or caps.rewrite_tool_input or caps.deny_tool_calls
                        or caps.trim_tool_output), adapter.id
        if adapter.tier != "gateway":
            assert caps.hard_token_cap is False, adapter.id
        assert caps.limitations, f"{adapter.id} must state its limits"


def test_generic_mcp_is_tools_tier_with_no_usage_source():
    adapter = get_adapter("generic_mcp")
    caps = adapter.capabilities()
    assert adapter.tier == "tools" and caps.proxy_tools and caps.usage_source is None
    assert list(adapter.iter_usage(Cursor("k"))) == []


def test_claude_code_adapter_detects_billing_without_exposing_the_key():
    billing = get_adapter("claude_code").detect_billing({"ANTHROPIC_API_KEY": "sk-ant-secret",
                                                         "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, {"auth": {}})
    assert billing.mode == "api" and "sk-ant-secret" not in repr(billing)


# ---------------------------------------------------------------- normalisation
def test_anthropic_semantics_are_already_disjoint():
    e = _event(input_tokens=10, cache_write_5m=5, cache_write_1h=7, cache_read=100, output_tokens=50,
               reasoning_tokens=20)
    assert canonical_tokens(e) == {"input_tokens": 10, "cache_write_5m": 5, "cache_write_1h": 7, "cache_read": 100,
                                   "output_tokens": 50, "thinking_tokens": 20}


def test_cached_subset_of_input_is_removed_from_input():
    # A provider that reports cached tokens inside the prompt count.
    e = _event(input_tokens=1000, cache_read=800, output_tokens=40, reasoning_tokens=10,
               semantics={"cached_is_subset_of_input": True, "reasoning_is_subset_of_output": True})
    t = canonical_tokens(e)
    assert (t["input_tokens"], t["cache_read"], t["output_tokens"], t["thinking_tokens"]) == (200, 800, 40, 10)
    assert t["input_tokens"] + t["cache_read"] == 1000  # the prompt is counted once


def test_reasoning_reported_separately_is_added_to_output():
    e = _event(input_tokens=100, output_tokens=40, reasoning_tokens=60,
               semantics={"cached_is_subset_of_input": False, "reasoning_is_subset_of_output": False})
    t = canonical_tokens(e)
    assert t["output_tokens"] == 100 and t["thinking_tokens"] == 60


def test_inconsistent_counts_never_go_negative():
    e = _event(input_tokens=5, cache_read=50, output_tokens=-3, reasoning_tokens=9,
               semantics={"cached_is_subset_of_input": True, "reasoning_is_subset_of_output": True})
    t = canonical_tokens(e)
    assert t["input_tokens"] == 0 and t["output_tokens"] == 0 and t["thinking_tokens"] == 0


# ---------------------------------------------------------------- Claude Code adapter
def test_claude_code_adapter_reproduces_the_reported_totals():
    cursor = Cursor("s1", str(TRANSCRIPT))
    events = list(get_adapter("claude_code").iter_usage(cursor))
    assert len(events) == 12 and len({e.message_id for e in events}) == 12
    assert all(e.host == "claude_code" and e.provider == "anthropic" and e.source == "transcript" for e in events)
    assert cursor.position == TRANSCRIPT.stat().st_size
    sums = {k: sum(canonical_tokens(e)[k] for e in events)
            for k in ("input_tokens", "cache_read", "output_tokens", "thinking_tokens")}
    sums["cache_write"] = sum(e.cache_write_5m + e.cache_write_1h for e in events)
    assert sums == {k: REPORTED[k] for k in sums}
    assert all("content" not in str(e.raw_ref) for e in events)  # a reference, never content


def test_adapter_ingest_writes_the_same_rows_as_the_hook_ledger(tmp_path, monkeypatch):
    hook_conn = _conn(tmp_path, monkeypatch, "hooks")
    cc_ledger.ingest(hook_conn, "s1", str(TRANSCRIPT))
    expected = hook_conn.execute(f"SELECT {', '.join(LEDGER_COLUMNS)} FROM cc_turns ORDER BY message_id").fetchall()

    conn = _conn(tmp_path, monkeypatch, "adapter")
    assert ingest(conn, get_adapter("claude_code"), "s1", str(TRANSCRIPT)) == 12
    got = conn.execute(f"SELECT {', '.join(LEDGER_COLUMNS)} FROM cc_turns ORDER BY message_id").fetchall()
    assert [tuple(r) for r in got] == [tuple(r) for r in expected]
    assert [tuple(r) for r in conn.execute("SELECT DISTINCT host, provider, source FROM cc_turns")] == [
        ("claude_code", "anthropic", "transcript")]
    totals = cc_ledger.session_totals(conn, session_id="s1")
    assert totals["ref_usd"] == pytest.approx(REPORTED["total_cost_usd"], rel=1e-9)


def test_adapter_and_hooks_share_one_cursor(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    adapter = get_adapter("claude_code")
    assert cc_ledger.ingest(conn, "s1", str(TRANSCRIPT)) == 12
    assert ingest(conn, adapter, "s1", str(TRANSCRIPT)) == 0          # nothing new for the adapter
    assert conn.execute("SELECT COUNT(*) FROM cc_turns").fetchone()[0] == 12


def test_adapter_ingest_is_idempotent_and_incremental(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    adapter = get_adapter("claude_code")
    data = TRANSCRIPT.read_bytes()
    live = tmp_path / "live.jsonl"
    live.write_bytes(data[: data.index(b"\n", len(data) // 2) + 40])  # ends mid-line
    first = ingest(conn, adapter, "s1", str(live))
    assert 0 < first < 12 and ingest(conn, adapter, "s1", str(live)) == 0
    live.write_bytes(data)
    ingest(conn, adapter, "s1", str(live))
    assert ingest(conn, adapter, "s1", str(live)) == 0
    totals = cc_ledger.session_totals(conn, session_id="s1")
    assert (totals["turns"], totals["output_tokens"], totals["cache_read"]) == (12, 2003, 465598)


def test_missing_transcript_yields_nothing(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    assert ingest(conn, get_adapter("claude_code"), "s1", str(tmp_path / "absent.jsonl")) == 0


# ---------------------------------------------------------------- other providers in the ledger
def test_unpriced_provider_is_stored_with_tokens_and_no_billing_units(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    e = _event(host="gateway", provider="openai", input_tokens=1000, cache_read=800, output_tokens=40,
               semantics={"cached_is_subset_of_input": True, "reasoning_is_subset_of_output": True})
    assert store_events(conn, [e, e._replace(message_id="")]) == 1     # no dedupe key: skipped
    row = conn.execute("SELECT host, provider, source, input_tokens, cache_read, billing_units, ref_usd "
                       "FROM cc_turns").fetchone()
    assert tuple(row) == ("gateway", "openai", "gateway", 200, 800, None, None)
    store_events(conn, [e])                                               # same id again: still one row
    assert conn.execute("SELECT COUNT(*) FROM cc_turns").fetchone()[0] == 1


def test_generic_cursor_table_round_trip(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    adapter = get_adapter("generic_mcp")
    adapter.save_cursor(conn, Cursor("log-a", "/x/a.log", 42, "st"))
    c = adapter.load_cursor(conn, "log-a")
    assert (c.path, c.position, c.state) == ("/x/a.log", 42, "st")
    assert adapter.load_cursor(conn, "other").position == 0


# ---------------------------------------------------------------- migration 5
def _make_v4_database(path):
    conn = init_db(path)
    conn.close()
    raw = sqlite3.connect(path)
    added = [("cc_turns", "host"), ("cc_turns", "provider"), ("cc_turns", "source"), ("cc_sessions", "host_adapter")]
    added += [("cc_sessions", c) for c in ("billing_mode", "billing_provider", "billing_confidence", "billing_evidence",
                                           "billing_source", "billing_mode_end", "billing_mixed", "billing_warned")]
    for table, column in added:
        raw.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    raw.execute("DROP TABLE usage_cursors")
    raw.execute("DROP TABLE IF EXISTS host_costs")
    raw.execute("DELETE FROM schema_version WHERE version >= 5")
    raw.execute("INSERT INTO cc_turns (message_id, session_id, output_tokens) VALUES ('old', 's0', 7)")
    raw.execute("INSERT INTO cc_sessions (session_id, host) VALUES ('s0', 'cli')")
    raw.commit()
    raw.close()


def test_existing_v4_database_migrates_additively(tmp_path):
    path = tmp_path / "bp.db"
    _make_v4_database(path)
    conn = init_db(path)
    try:
        assert get_current_schema_version(conn) == LATEST_SCHEMA_VERSION
        assert conn.execute("SELECT output_tokens, host, provider, source FROM cc_turns").fetchone()[:] == (
            7, "claude_code", "anthropic", "transcript")
        assert conn.execute("SELECT host, host_adapter FROM cc_sessions").fetchone()[:] == ("cli", "claude_code")
        _migration_5_hosts(conn)  # re-running is harmless
        assert conn.execute("SELECT COUNT(*) FROM cc_turns").fetchone()[0] == 1
    finally:
        conn.close()


def test_hook_path_upgrades_an_existing_v4_database(tmp_path, monkeypatch):
    monkeypatch.setenv("BP_HOME", str(tmp_path))
    _make_v4_database(tmp_path / "bp.db")
    conn = hookdb.open_ready()
    assert conn is not None
    assert conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0] == LATEST_SCHEMA_VERSION
    assert conn.execute("SELECT host FROM cc_turns WHERE message_id='old'").fetchone()[0] == "claude_code"


# ---------------------------------------------------------------- hook-path cost
def test_host_modules_import_no_typing_or_dataclasses():
    code = ("import sys; import budgetpilot.hosts, budgetpilot.hosts.base, budgetpilot.hosts.claude_code, "
            "budgetpilot.hosts.usage; print(sorted(m for m in ('typing', 'dataclasses') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"

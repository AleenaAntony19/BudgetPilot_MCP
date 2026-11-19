"""Phase E: Codex CLI importer (observe only, experimental, synthetic fixtures from Codex's source)."""

import json
from pathlib import Path
import shutil

import pytest

from budgetpilot import cc_cli
from budgetpilot.core import cc_ledger, cc_report, hookdb, pricebook, state
from budgetpilot.hosts import get_adapter
from budgetpilot.hosts.base import Cursor
from budgetpilot.hosts.codex_cli import discover, import_all, redacted_sample
from budgetpilot.hosts.policy import report_policy

FIX = Path(__file__).parent / "fixtures" / "codex"
CURRENT = next(FIX.glob("rollout-2026-11-19*.jsonl"))
LEGACY = next(FIX.glob("rollout-2026-10-01*.jsonl"))
SESSION = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
SECRETS = ("SECRET-CODEX-PROMPT", "SECRET-CODEX-ANSWER", "SECRET-CODEX-INSTRUCTIONS", "secret-project")


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    day = home / "sessions" / "2026" / "11" / "19"
    day.mkdir(parents=True)
    old = home / "sessions" / "2026" / "10" / "01"
    old.mkdir(parents=True)
    shutil.copy(CURRENT, day / CURRENT.name)
    shutil.copy(LEGACY, old / LEGACY.name)
    (old / "rollout-2026-09-01T00-00-00-0199a1b2-1111-7e5f-8a9b-0c1d2e3f4a5d.jsonl.zst").write_bytes(b"\x28\xb5\x2f\xfd")
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _rows(conn, session=None):
    sql = ("SELECT message_id, session_id, task_id, model, input_tokens, cache_read, output_tokens, thinking_tokens, "
           "host, provider, source FROM cc_turns" + (" WHERE session_id=?" if session else "") + " ORDER BY ts")
    keys = ("message_id", "session_id", "task_id", "model", "input_tokens", "cache_read", "output_tokens",
            "thinking_tokens", "host", "provider", "source")
    return [dict(zip(keys, r)) for r in conn.execute(sql, (session,) if session else ())]


def test_discover_finds_plain_logs_and_lists_compressed_ones(codex_home):
    plain, compressed = discover()
    assert len(plain) == 2 and len(compressed) == 1


def test_usage_records_become_one_row_per_response(codex_home):
    conn = hookdb.open_ready()
    report = import_all(conn)
    assert report["files"] == 2 and len(report["compressed_skipped"]) == 1
    rows = _rows(conn, SESSION)
    assert [r["message_id"] for r in rows] == ["resp_A", "resp_B"]          # token_count events ignored here
    a = rows[0]
    # cached tokens are part of input in Codex: 12,000 input of which 10,000 cached
    assert (a["input_tokens"], a["cache_read"], a["output_tokens"], a["thinking_tokens"]) == (2000, 10000, 300, 120)
    assert (a["task_id"], a["model"], a["host"], a["provider"], a["source"]) == (
        "turn-1", "gpt-test-codex", "codex_cli", "openai", "import")


def test_older_logs_use_increases_of_the_cumulative_count(codex_home):
    conn = hookdb.open_ready()
    import_all(conn)
    rows = _rows(conn, "0199a1b2-0000-7e5f-8a9b-0c1d2e3f4a5c")
    assert len(rows) == 2                                                    # the repeated total is not a new turn
    second = rows[1]
    assert (second["input_tokens"], second["cache_read"], second["output_tokens"], second["model"]) == (
        1200, 4800, 60, "gpt-old")                                           # 6,000 new input, 4,800 of it cached


def test_import_is_idempotent_and_incremental(codex_home):
    conn = hookdb.open_ready()
    first = import_all(conn)
    assert first["events"] == 4 and import_all(conn)["events"] == 0
    path = next((codex_home / "sessions").rglob(CURRENT.name))
    with open(path, "a") as f:
        f.write(json.dumps({"timestamp": "2026-11-19T10:01:00.000Z", "type": "token_usage_record", "payload": {
            "response_id": "resp_C", "turn_id": "turn-2", "usage": {"input_tokens": 100, "cached_input_tokens": 0,
                                                                    "output_tokens": 5, "reasoning_output_tokens": 0,
                                                                    "total_tokens": 105}}}) + "\n")
    assert import_all(conn)["events"] == 1
    assert [r["message_id"] for r in _rows(conn, SESSION)][-1] == "resp_C"


def test_nothing_but_numbers_ids_and_models_is_stored(codex_home):
    conn = hookdb.open_ready()
    import_all(conn)
    conn.close()
    for path in state.get_bp_home().rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert not any(s.encode() in data for s in SECRETS), path


def test_imported_sessions_are_observe_only_and_show_no_dollars(codex_home, capsys):
    conn = hookdb.open_ready()
    import_all(conn)
    data = cc_report.build(conn, SESSION)
    entry = data["sessions"][0]
    assert entry["host"] == "codex_cli" and entry["billing"]["mode"] == "unknown" and "ref_usd" not in entry
    assert entry["policy"]["tier"] == "observe" and not entry["policy"]["show_gate"]
    # OpenAI models are unpriced until rates are set, so no billing units either
    assert entry["billing_units"] == 0
    pricebook.set_rates("openai", "gpt-test-codex", {"input": 1.0, "cache_write_5m": 1.25, "cache_write_1h": 2.0,
                                                     "cache_read": 0.1, "output": 8.0})
    priced = cc_report.build(conn, SESSION)["sessions"][0]
    assert priced["billing_units"] > 0 and "ref_usd" not in priced             # still unknown billing: no dollars


def test_billing_override_for_codex(codex_home):
    state.update_config(billing={"codex_cli": {"mode_override": "api"}})
    conn = hookdb.open_ready()
    import_all(conn, config=state.read_config())
    assert hookdb.session_billing(conn, SESSION)["billing_mode"] == "api"


def test_adapter_declares_measure_only():
    adapter = get_adapter("codex_cli")
    caps = adapter.capabilities()
    assert adapter.tier == "observe" and caps.usage_source == "import"
    assert not (caps.intercept_tool_calls or caps.deny_tool_calls or caps.hard_token_cap or caps.proxy_tools)
    assert adapter.describe()["tier_label"] == "measured only, no control"
    assert not report_policy(None, caps)["show_recovery"]


def test_compressed_and_missing_files_yield_nothing():
    adapter = get_adapter("codex_cli")
    assert list(adapter.iter_usage(Cursor("k", "/nope/rollout.jsonl.zst"))) == []
    assert list(adapter.iter_usage(Cursor("k", "/nope/missing.jsonl"))) == []


def test_redacted_sample_keeps_shapes_and_drops_text(tmp_path):
    sample = redacted_sample(str(CURRENT))
    text = json.dumps(sample)
    assert not any(s in text for s in SECRETS)
    assert sample["_meta"]["line_types"] == {"session_meta": 1, "turn_context": 1, "response_item": 2,
                                             "token_usage_record": 2, "event_msg": 2}
    record = next(l for l in sample["lines"] if l["type"] == "token_usage_record")
    assert record["payload"]["usage"]["cached_input_tokens"] == 10000
    out = tmp_path / "s.json"
    assert cc_cli.run_capture_codex(str(CURRENT), str(out), out=lambda _: None) == 0 and out.exists()


def test_cli_import(codex_home):
    lines = []
    assert cc_cli.run_import_codex(out=lines.append) == 0
    assert "4 new usage record(s)" in lines[0] and "compressed" in lines[1]

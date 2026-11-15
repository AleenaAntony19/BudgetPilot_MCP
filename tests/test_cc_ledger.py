"""Transcript token ledger: parser, de-duplication, incremental cursor, prices."""

import json
import shutil

import pytest

from budgetpilot.core import cc_ledger, hookdb
from tests.hookutil import TRANSCRIPT, db, fixture, make_repo, run_hook, start_task

# What `claude -p --output-format json` reported for the session this transcript came from.
REPORTED = {"input_tokens": 24, "cache_write": 35929, "cache_read": 465598, "output_tokens": 2003,
            "thinking_tokens": 213, "total_cost_usd": 0.012845680000000003}


def test_deduplicated_totals_match_what_claude_code_reported():
    report = cc_ledger.check_transcript(str(TRANSCRIPT))
    assert report["assistant_rows"] == 15 and report["unique_messages"] == 12 and report["max_rows_per_message"] == 3
    for key in ("input_tokens", "cache_write", "cache_read", "output_tokens", "thinking_tokens"):
        assert report["totals"][key] == REPORTED[key]
    summary = cc_ledger.summarize_transcript(str(TRANSCRIPT))
    assert summary["ref_usd"] == pytest.approx(REPORTED["total_cost_usd"], rel=1e-9)
    assert summary["cache_write_1h"] == 35929 and summary["cache_write_5m"] == 0
    assert summary["price_guess"] is False and summary["models"] == ["claude-haiku-5-5"]


def test_without_deduplication_the_count_would_be_too_high():
    rows = [json.loads(line) for line in TRANSCRIPT.read_text().splitlines()]
    naive = sum(r["message"]["usage"]["output_tokens"] for r in rows if r["type"] == "assistant")
    assert naive > REPORTED["output_tokens"]


def test_billing_units_formula():
    tokens = {"input_tokens": 100, "cache_write_5m": 200, "cache_write_1h": 300, "cache_read": 1000,
              "output_tokens": 50, "thinking_tokens": 20}
    assert cc_ledger.billing_units(tokens, 0.1) == 100 + 250 + 600 + 100 + 250  # thinking is inside output


@pytest.mark.parametrize("model,price,read_mult,guessed", [
    ("claude-haiku-5-5", 0.10, 0.10, False), ("claude-sonnet-5-5-20261001", 2.00, 0.05, False),
    ("claude-opus-5-5", 4.00, 0.05, False), ("claude-fable-5-2", 8.00, 0.025, False),
    ("claude-fable-5-1", 10.00, 0.025, False), ("claude-fable-5", 10.00, 0.10, False),
    ("claude-opus-4-8", 5.00, 0.10, False), ("some-new-model", 2.00, 0.05, True), (None, 2.00, 0.05, True),
])
def test_price_lookup_by_prefix(model, price, read_mult, guessed):
    assert cc_ledger.price_for(model) == (price, read_mult, guessed)


def test_prices_json_override(tmp_path, monkeypatch):
    monkeypatch.setenv("BP_HOME", str(tmp_path))
    (tmp_path / "prices.json").write_text(json.dumps(
        {"claude_models": {"claude-haiku-5-5": {"input_per_million": 0.2, "cache_read_multiplier": 0.5}}}))
    assert cc_ledger.price_for("claude-haiku-5-5") == (0.2, 0.5, False)
    assert cc_ledger.price_for("claude-opus-5-5")[0] == 4.00


def test_haiku_long_prompt_tier():
    row = {"type": "assistant", "message": {"id": "m", "model": "claude-haiku-5-5", "usage": {
        "input_tokens": 10, "cache_read_input_tokens": 150_000, "output_tokens": 100}}}
    turn = cc_ledger._turn_from_row(row, None, cc_ledger.DEFAULT_MODEL_PRICES)
    assert turn["ref_usd"] == pytest.approx(turn["billing_units"] * 0.5 / 1e6)


def test_usage_without_cache_split_counts_as_5_minute_writes():
    tokens = cc_ledger.parse_usage({"input_tokens": 1, "cache_creation_input_tokens": 500, "output_tokens": 2})
    assert (tokens["cache_write_5m"], tokens["cache_write_1h"], tokens["thinking_tokens"]) == (500, 0, 0)


def _conn(tmp_path, monkeypatch):
    monkeypatch.setenv("BP_HOME", str(tmp_path / "home"))
    return hookdb.open_ready()


def test_incremental_ingest_tolerates_partial_lines_and_split_messages(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    data = TRANSCRIPT.read_bytes()
    live = tmp_path / "live.jsonl"
    cut = data.index(b"\n", len(data) // 2) + 40          # ends in the middle of a line
    live.write_bytes(data[:cut])
    first = cc_ledger.ingest(conn, "s1", str(live))
    offset = conn.execute("SELECT byte_offset FROM transcript_cursor").fetchone()[0]
    assert 0 < first < 12 and data[offset - 1:offset] == b"\n" and offset < cut
    assert cc_ledger.ingest(conn, "s1", str(live)) == 0   # nothing new, cursor unchanged
    live.write_bytes(data)
    cc_ledger.ingest(conn, "s1", str(live))
    totals = cc_ledger.session_totals(conn, session_id="s1")
    assert totals["turns"] == 12
    assert (totals["input_tokens"], totals["cache_write"], totals["cache_read"], totals["output_tokens"]) == (
        24, 35929, 465598, 2003)
    assert totals["ref_usd"] == pytest.approx(REPORTED["total_cost_usd"], rel=1e-9)
    assert conn.execute("SELECT COUNT(DISTINCT task_id), MIN(task_id) IS NOT NULL FROM cc_turns").fetchone()[:] == (1, 1)
    assert json.loads(conn.execute("SELECT tools_called FROM cc_turns WHERE tools_called != '[]'").fetchone()[0])


def test_rewritten_transcript_is_rescanned_without_double_counting(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    live = tmp_path / "live.jsonl"
    shutil.copy(TRANSCRIPT, live)
    cc_ledger.ingest(conn, "s1", str(live))
    lines = TRANSCRIPT.read_text().splitlines(keepends=True)
    live.write_text("".join(lines[:10]))                   # shorter than the stored offset
    cc_ledger.ingest(conn, "s1", str(live))
    assert cc_ledger.session_totals(conn, session_id="s1")["output_tokens"] == 2003
    assert conn.execute("SELECT byte_offset FROM transcript_cursor").fetchone()[0] == live.stat().st_size


def test_missing_or_garbage_transcript(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    assert cc_ledger.ingest(conn, "s1", str(tmp_path / "nope.jsonl")) == 0
    junk = tmp_path / "junk.jsonl"
    junk.write_text('not json\n[1,2]\n{"type":"assistant","message":{"id":"a"}}\n{"type":"assistant"}\n')
    assert cc_ledger.ingest(conn, "s1", str(junk)) == 0
    assert cc_ledger.check_transcript(str(junk))["assistant_rows_without_usage"] == 2


def test_cache_hit_ratio_and_drop_flag(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    assert cc_ledger.cache_hit_ratio(10, 90, 900) == 0.9 and cc_ledger.cache_hit_ratio(0, 0, 0) is None
    for i, (write, read) in enumerate([(100, 900), (50, 950), (900, 100), (10, 990)]):
        conn.execute("INSERT INTO cc_turns (message_id, session_id, ts, cache_write_1h, cache_read) VALUES (?, 's', ?, ?, ?)",
                     (f"m{i}", f"2026-01-01T00:00:0{i}Z", write, read))
    series = cc_ledger.cache_series(conn, "s")
    assert series["ratios"] == [0.9, 0.95, 0.1, 0.99] and series["drops"] == [2]


def test_calibration_compares_estimates_with_derived_growth(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    for i, (context, output) in enumerate([(10_000, 100), (12_100, 50), (13_150, 20)]):
        conn.execute("INSERT INTO cc_turns (message_id, session_id, task_id, ts, context_tokens, output_tokens) "
                     "VALUES (?, 's', 't', ?, ?, ?)", (f"m{i}", f"2026-01-01T00:00:0{i}Z", context, output))
    conn.execute("INSERT INTO tool_obs (session_id, task_id, tool, result_tokens_est) VALUES ('s', 't', 'Read', 3000)")
    result = cc_ledger.calibration(conn)
    assert result == {"tasks": 1, "ratio": 1.0, "spread": 0.0, "ok": True, "label": "derived"}
    assert cc_ledger.calibration(conn, session_id="other")["ratio"] is None


def test_stop_hook_writes_the_ledger_and_task_output(tmp_path):
    repo, home = make_repo(tmp_path / "repo"), tmp_path / "home"
    home.mkdir()
    prompt_id = json.loads(TRANSCRIPT.read_text().splitlines()[0])["promptId"]
    start_task(repo, home, "Fix add in pkg/calc.py", mode="observe", prompt_id=prompt_id)
    stop = fixture("Stop", repo, transcript_path=str(TRANSCRIPT), prompt_id=prompt_id)
    assert run_hook(stop, home, mode="observe")[:2] == (0, "")
    assert run_hook(stop, home, mode="observe")[:2] == (0, "")  # a second Stop adds nothing
    conn = db(home)
    row = conn.execute("SELECT COUNT(*), SUM(output_tokens), SUM(ref_usd) FROM cc_turns WHERE session_id='session-1'").fetchone()
    assert (row[0], row[1]) == (12, 2003) and row[2] == pytest.approx(REPORTED["total_cost_usd"], rel=1e-9)
    task = conn.execute("SELECT out_used, status FROM tasks_bp").fetchone()
    assert tuple(task) == (2003, "done")

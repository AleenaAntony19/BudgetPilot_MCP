"""Phase C: price book (parse, resolve, refresh, validate), re-pricing from tokens, drift check, reconcile."""

import io
import json
from pathlib import Path
import time
import urllib.error

import pytest

from budgetpilot import cc_cli
from budgetpilot.core import canary, cc_ledger, hookdb, pricebook, state
from budgetpilot.hosts.base import BillingContext
from budgetpilot.hosts.policy import report_policy
from tests.hookutil import TRANSCRIPT

FIX = Path(__file__).parent / "fixtures" / "pricing"
PRICING = (FIX / "anthropic_pricing_2026-11-19.md").read_text()
MODELS = (FIX / "anthropic_models_overview_2026-11-19.md").read_text()
REPORTED_USD = 0.012845680000000003


def _with_row(name, prices):
    """The pricing page with one model row's five prices replaced (first table only)."""
    lines = PRICING.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith(f"| {name} ") and "MTok" in line:
            lines[i] = f"| {name} | " + " | ".join(f"${p} / MTok" for p in prices) + " |\n"
            return "".join(lines)
    raise AssertionError(name)


def _entries():
    return pricebook.parse_anthropic(PRICING, MODELS, as_of="2026-11-19")


def _fake_fetch(pricing=PRICING, models=MODELS):
    def fetch(url, timeout=20.0):
        return models if "models" in url else pricing
    return fetch


# ---------------------------------------------------------------- parser and bundled snapshot
def test_parser_reads_every_model_row_with_its_long_context_tier_and_aliases():
    entries = {e["model_id"]: e for e in _entries()}
    assert len(entries) == 21
    assert entries["claude-opus-4-1"]["rates"] == {"input": 15.0, "cache_write_5m": 18.75, "cache_write_1h": 30.0,
                                                   "cache_read": 1.5, "output": 75.0}
    assert entries["claude-sonnet-5"]["rates"]["input"] == 2.0          # footnote marker stripped
    haiku = entries["claude-haiku-5-5"]
    assert haiku["rates"]["input"] == 0.10
    assert haiku["long_context"] == {"threshold_tokens": 100000, "rates": {
        "input": 0.5, "cache_write_5m": 0.625, "cache_write_1h": 1.0, "cache_read": 0.05, "output": 2.5}}
    assert "global.anthropic.claude-fable-5-2" in entries["claude-fable-5-2"]["aliases"]
    for e in entries.values():
        pricebook.validate_rates(e["rates"], e["model_id"])


def test_bundled_snapshot_matches_the_cited_page():
    bundled = json.loads(pricebook.BUNDLED.read_text())
    pricebook.validate_book(bundled)
    assert bundled["content_hash"] == pricebook.content_hash(bundled["entries"])
    parsed = {e["model_id"]: e["rates"] for e in _entries()}
    assert {e["model_id"]: e["rates"] for e in bundled["entries"]} == parsed
    assert all(e["source_url"] == pricebook.ANTHROPIC_PRICING_URL for e in bundled["entries"])


@pytest.mark.parametrize("broken,error", [
    (PRICING.replace("## Model pricing", "## Prices"), "section not found"),
    (PRICING.replace("Base input tokens", "Input"), "headers changed"),
    (PRICING.replace("| $15 / MTok            |", "| contact sales |", 1), "no price"),
])
def test_a_changed_page_is_rejected_rather_than_misread(broken, error):
    with pytest.raises(pricebook.PriceBookError, match=error):
        pricebook.parse_anthropic(broken, MODELS)


# ---------------------------------------------------------------- resolution
@pytest.mark.parametrize("model,expected_input", [
    ("claude-haiku-5-5", 0.10),
    ("anthropic.claude-sonnet-5-5", 2.0),             # documented Bedrock ID
    ("claude-sonnet-4-5-20250929", 3.0),              # dated snapshot of an alias
    ("claude-opus-4-1", 15.0),                        # not Opus 4.5's $5: no family guess
    ("claude-opus-4", 15.0),
])
def test_price_for_known_models(model, expected_input):
    assert pricebook.price_for("anthropic", model).rates["input"] == expected_input


@pytest.mark.parametrize("model", ["some-new-model", "claude-opus", "claude-opus-4-1-2025", "claude-sonnet-5-5x", None])
def test_unknown_models_are_unpriced_never_guessed(model):
    assert pricebook.price_for("anthropic", model) is pricebook.UNPRICED
    assert pricebook.price_for("openai", "claude-haiku-5-5") is pricebook.UNPRICED


def test_resolution_by_timestamp():
    old = {"provider": "anthropic", "model_id": "m-x", "aliases": [], "as_of": "2026-01-01", "valid_from": None,
           "source_url": "u", "source": "refresh",
           "rates": {"input": 1, "cache_write_5m": 1.25, "cache_write_1h": 2, "cache_read": 0.1, "output": 5}}
    new = {**old, "valid_from": "2026-06-01T00:00:00Z", "rates": {**old["rates"], "input": 2, "output": 10}}
    late = {**old, "model_id": "m-late", "valid_from": "2026-06-01T00:00:00Z"}
    book = {"entries": [old, new, late]}
    assert pricebook.price_for("anthropic", "m-x", "2026-03-01T00:00:00Z", book).rates["input"] == 1
    assert pricebook.price_for("anthropic", "m-x", "2026-07-01T00:00:00Z", book).rates["input"] == 2
    assert pricebook.price_for("anthropic", "m-late", "2026-03-01T00:00:00Z", book) is pricebook.UNPRICED


def test_units_are_relative_to_each_models_input_rate_and_long_prompts_cost_more_dollars():
    tokens = {"input_tokens": 10, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 150_000, "output_tokens": 100}
    haiku = pricebook.price_for("anthropic", "claude-haiku-5-5")
    units, usd = pricebook.units_and_usd(haiku, tokens)
    assert units == pytest.approx(10 + 0.1 * 150_000 + 5 * 100)
    assert usd == pytest.approx(units * 0.5 / 1e6)                     # long-context tier: 5x the base price
    fable = pricebook.price_for("anthropic", "claude-fable-5-2")
    assert pricebook.units_and_usd(fable, {"cache_read": 1000})[0] == pytest.approx(25.0)   # 0.025x reads


def test_ledger_totals_still_match_what_claude_code_reported():
    totals = cc_ledger.summarize_transcript(str(TRANSCRIPT))
    assert totals["ref_usd"] == pytest.approx(REPORTED_USD, rel=1e-9) and totals["price_guess"] is False


# ---------------------------------------------------------------- refresh and validation
def test_refresh_writes_atomically_and_keeps_a_backup(tmp_path):
    path = tmp_path / "pricebook.json"
    first = pricebook.refresh(fetch=_fake_fetch(), path=path)
    assert first["ok"] and first["changes"] == [] and path.exists()
    assert pricebook.refresh(fetch=_fake_fetch(), path=path)["ok"]
    assert (tmp_path / "pricebook.json.bak").exists()


def test_refresh_rejects_rates_out_of_order_and_keeps_the_old_file(tmp_path):
    path = tmp_path / "pricebook.json"
    pricebook.refresh(fetch=_fake_fetch(), path=path)
    before = path.read_bytes()
    broken = _with_row("Claude Sonnet 5.5", (2, 2.5, 4, 50, 10))    # cache read above input
    report = pricebook.refresh(fetch=_fake_fetch(broken), path=path, confirm=True)
    assert not report["ok"] and "out of order" in report["error"] and path.read_bytes() == before


def test_refresh_needs_confirmation_for_jumps_over_50_percent(tmp_path):
    path = pricebook.user_path()   # the location price_for reads (BP_HOME is a temp dir in tests)
    pricebook.refresh(fetch=_fake_fetch(), path=path)
    before = path.read_bytes()
    doubled = _with_row("Claude Haiku 4.5", (2, 2.5, 4, 0.2, 10))
    assert doubled != PRICING
    rejected = pricebook.refresh(fetch=_fake_fetch(doubled), path=path)
    assert not rejected["ok"] and "more than 50%" in rejected["error"] and path.read_bytes() == before
    accepted = pricebook.refresh(fetch=_fake_fetch(doubled), path=path, confirm=True)
    assert accepted["ok"]
    book = json.loads(path.read_text())
    haiku = sorted((e for e in book["entries"] if e["model_id"] == "claude-haiku-4-5"),
                   key=lambda e: e.get("valid_from") or "")
    assert [e["rates"]["input"] for e in haiku] == [1.0, 2.0]          # old usage keeps the old price
    assert pricebook.price_for("anthropic", "claude-haiku-4-5", "2026-01-01T00:00:00Z").rates["input"] == 1.0
    assert pricebook.price_for("anthropic", "claude-haiku-4-5").rates["input"] == 2.0


def test_offline_refresh_keeps_the_current_file(tmp_path):
    path = tmp_path / "pricebook.json"
    pricebook.refresh(fetch=_fake_fetch(), path=path)
    before = path.read_bytes()

    def offline(url, timeout=20.0):
        raise urllib.error.URLError("no network")

    report = pricebook.refresh(fetch=offline, path=path)
    assert not report["ok"] and "fetch failed" in report["providers"]["anthropic"] and path.read_bytes() == before


def test_unsupported_provider_is_a_stub_with_a_clear_message(tmp_path):
    report = pricebook.refresh(providers=("openai",), fetch=_fake_fetch(), path=tmp_path / "p.json")
    assert not report["ok"] and "bp prices set openai" in report["providers"]["openai"]


def test_stale_after_30_days():
    assert pricebook.is_stale({"fetched_at": time.strftime("%Y-%m-%d", time.gmtime(time.time() - 40 * 86400))})
    assert not pricebook.is_stale({"fetched_at": time.strftime("%Y-%m-%d", time.gmtime())})
    assert pricebook.is_stale({"fetched_at": None})


def test_set_rates_is_validated_wins_from_today_and_does_not_look_like_a_refresh():
    with pytest.raises(pricebook.PriceBookError):
        pricebook.set_rates("openai", "gpt-x", {"input": 1, "cache_write_5m": 0, "cache_write_1h": 0,
                                                "cache_read": 2, "output": 4})
    pricebook.set_rates("openai", "gpt-x", {"input": 1, "cache_write_5m": 1.25, "cache_write_1h": 2,
                                            "cache_read": 0.5, "output": 4}, "https://example.invalid/pricing")
    price = pricebook.price_for("openai", "gpt-x")
    assert price.rates["output"] == 4 and price.source == "user"
    assert pricebook.load()["fetched_at"] == json.loads(pricebook.BUNDLED.read_text())["fetched_at"]


def test_legacy_prices_json_override_still_applies():
    (state.get_bp_home() / "prices.json").write_text(json.dumps(
        {"claude_models": {"claude-haiku-5-5": {"input_per_million": 0.2, "cache_read_multiplier": 0.5}}}))
    price = pricebook.price_for("anthropic", "claude-haiku-5-5")
    assert price.rates["input"] == 0.2 and price.rates["cache_read"] == pytest.approx(0.1)


# ---------------------------------------------------------------- ledger re-pricing
def test_a_session_is_repriced_from_tokens_when_the_book_changes(tmp_path):
    conn = hookdb.open_ready()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    live = tmp_path / "t.jsonl"
    live.write_text(TRANSCRIPT.read_text().replace('"timestamp": "2026-11-14', f'"timestamp": "{today}'))
    cc_ledger.ingest(conn, "s1", str(live))
    before = cc_ledger.session_totals(conn, session_id="s1")["ref_usd"]
    assert before == pytest.approx(REPORTED_USD, rel=1e-9)
    pricebook.set_rates("anthropic", "claude-haiku-5-5", {"input": 0.2, "cache_write_5m": 0.25, "cache_write_1h": 0.4,
                                                          "cache_read": 0.02, "output": 1.0})
    after = cc_ledger.session_totals(conn, session_id="s1")["ref_usd"]
    assert after == pytest.approx(2 * before, rel=1e-6)                # same tokens, new rates


def test_unknown_model_in_a_transcript_is_unpriced(tmp_path):
    data = TRANSCRIPT.read_text().replace("claude-haiku-5-5", "claude-unreleased-9")
    path = tmp_path / "u.jsonl"
    path.write_text(data)
    totals = cc_ledger.summarize_transcript(str(path))
    assert totals["price_guess"] is True and totals["billing_units"] == 0 and totals["ref_usd"] == 0
    assert totals["output_tokens"] == 2003                              # tokens are still counted


# ---------------------------------------------------------------- drift check
def _conn_with_session(tmp_path):
    conn = hookdb.open_ready()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    path = tmp_path / "s.jsonl"
    path.write_text(TRANSCRIPT.read_text().replace('"timestamp": "2026-11-14', f'"timestamp": "{today}'))
    return conn, str(path)


def test_matching_host_cost_reconciles(tmp_path):
    conn, path = _conn_with_session(tmp_path)
    verdict = canary.record(conn, "s1", REPORTED_USD, "statusline", path)
    assert verdict["reconciled"] and not verdict["drift"] and verdict["closest_diff_pct"] < 1e-6
    policy = report_policy(BillingContext("api"), reconciled=verdict["reconciled"])
    assert policy["dollars_label"] == "API-equivalent, reconciled"


def test_persistent_difference_flags_a_stale_price_table(tmp_path):
    conn, path = _conn_with_session(tmp_path)
    for _ in range(3):
        verdict = canary.record(conn, "s1", REPORTED_USD * 1.10, "statusline", path)
    assert verdict["drift"] and not verdict["reconciled"] and "may be stale" in verdict["message"] and "side requests" in verdict["message"]


def test_a_single_lagging_sample_does_not_flag_drift(tmp_path):
    conn, path = _conn_with_session(tmp_path)
    canary.record(conn, "s1", REPORTED_USD * 1.5, "statusline", path)
    verdict = canary.record(conn, "s1", REPORTED_USD, "statusline", path)
    assert verdict["reconciled"]


def test_ab_runs_compare_with_claude_codes_reported_cost():
    archived = json.loads((Path(__file__).parents[1] / "docs" / "results" / "ab_20261115_160545.json").read_text())
    check = canary.compare_runs(archived["runs"])
    assert check["runs"] == 12 and check["reconciled"] and check["max_diff_pct"] < 0.001
    from budgetpilot.bench.ab import render_results

    assert "Price check: BudgetPilot's API-equivalent cost" in render_results(archived)
    assert "within the 2% tolerance" in render_results(archived)


# ---------------------------------------------------------------- status line
def test_statusline_records_the_host_cost_and_prints_one_line(tmp_path, capsys):
    state.set_mode("on")
    _, path = _conn_with_session(tmp_path)
    payload = {"session_id": "s1", "transcript_path": path, "model": {"display_name": "Haiku"},
               "context_window": {"used_percentage": 8}, "cost": {"total_cost_usd": REPORTED_USD}}
    lines = []
    assert cc_cli.run_statusline(io.StringIO(json.dumps(payload)), out=lines.append) == 0
    assert lines == ["BudgetPilot ON · Haiku · context 8%"]
    assert canary.status(hookdb.open_ready(), "s1")["reconciled"]


def test_statusline_in_mode_off_writes_nothing_and_bad_input_never_fails(tmp_path):
    state.set_mode("off")
    lines = []
    assert cc_cli.run_statusline(io.StringIO(json.dumps({"session_id": "x", "cost": {"total_cost_usd": 1}})),
                                 out=lines.append) == 0
    assert lines == ["BudgetPilot OFF"] and not (state.get_bp_home() / "bp.db").exists()
    assert cc_cli.run_statusline(io.StringIO("not json"), out=lines.append) == 0 and lines[-1] == "BudgetPilot"


# ---------------------------------------------------------------- console reconcile and doctor
def test_reconcile_console_export_by_model(tmp_path):
    conn, path = _conn_with_session(tmp_path)
    cc_ledger.ingest(conn, "s1", path)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    export = tmp_path / "usage.csv"
    export.write_text(f"date,model,cost_usd\n{today},claude-haiku-5-5,{REPORTED_USD * 1.25}\n")
    result = canary.reconcile_console(conn, canary.read_console_export(str(export)))
    row = result["per_model"][0]
    assert row["model"] == "claude-haiku-5-5" and row["diff_pct"] == pytest.approx(-20.0, abs=0.01)
    ambiguous = tmp_path / "a.csv"
    ambiguous.write_text("model,cost_a,cost_b\nm,1,2\n")
    with pytest.raises(ValueError, match="cost-col"):
        canary.read_console_export(str(ambiguous))


def test_doctor_warns_about_a_stale_price_book(monkeypatch, capsys):
    monkeypatch.setattr(cc_cli, "find_claude", lambda: None)
    monkeypatch.setattr(pricebook, "is_stale", lambda book=None: True)
    lines = []
    cc_cli._doctor_prices(lambda status, title, detail="", hint="": lines.append((status, title, hint)))
    assert ("WARN", "Price book", "bp prices refresh") in lines

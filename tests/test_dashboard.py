"""Local dashboard: endpoints against a seeded database, page hygiene, process lifecycle."""

import http.client
import json
import re
import sqlite3
import threading
import time

import pytest

from budgetpilot import dashboard
from budgetpilot.core import cc_ledger, hookdb, state
from budgetpilot.dashboard import server as dash
from tests.hookutil import TRANSCRIPT, fixture, make_repo, run_hook, start_task


def get(port, path, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Host": host} if host else {})
    response = conn.getresponse()
    body = response.read().decode("utf-8")
    conn.close()
    return response, body


@pytest.fixture
def served():
    httpd = dash.make_server(18765)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.delenv("BP_MODE", raising=False)
    home = state.get_bp_home()
    repo = make_repo(tmp_path / "repo")
    start_task(repo, home, "Fix the failing addition test.")
    run_hook(fixture("PreToolUse__Grep", repo), home)
    run_hook(fixture("PreToolUse__Bash__pytest_full_suite", repo), home)
    run_hook(fixture("PostToolUse__Bash__huge_output", repo), home)
    run_hook(fixture("PostToolUseFailure__Bash__pytest_fail", repo), home)
    conn = hookdb.open_ready()
    cc_ledger.ingest(conn, "session-1", str(TRANSCRIPT))
    now = time.time()
    for i, (observe, enforce) in enumerate([(1000, 800), (2000, 1500), (1500, 1650)]):
        for arm, units in (("observe", observe), ("enforce", enforce)):
            conn.execute("INSERT INTO ab_runs (run_id, pair_id, arm, task_id, repeat, passed, billing_units, ts) "
                         "VALUES (?, ?, ?, ?, 0, 1, ?, ?)", (f"r{i}{arm}", f"ab_1:t{i}:0", arm, f"t{i}", units, now))
    conn.close()
    state.set_mode("on")
    return home


def test_every_endpoint_on_an_empty_home(served):
    for path in dash.ROUTES:
        response, body = get(served, path)
        assert response.status == 200 and response.getheader("Content-Type") == "application/json"
        assert "error" not in json.loads(body), path
    summary = json.loads(get(served, "/api/summary")[1])
    assert summary["mode"] == "off" and summary["measured"] == {"available": False}
    assert summary["task"] is None and summary["tokens_session"].get("turns", 0) == 0


def test_endpoints_with_data(served, seeded):
    summary = json.loads(get(served, "/api/summary")[1])
    assert re.fullmatch(r"\d\d:\d\d:\d\d", summary["as_of"])
    assert summary["mode"] == "enforce" and summary["budget"] == "balanced" and summary["hooks_last_seen"]
    tokens = summary["tokens_session"]
    assert (tokens["turns"], tokens["input"], tokens["cache_write"], tokens["cache_read"], tokens["output"],
            tokens["thinking"]) == (12, 24, 35929, 465598, 2003, 213)
    assert tokens["cache_hit_ratio"] == 0.9283 and len(summary["cache_ratio_series"]) == 12
    estimated = summary["estimated"]
    assert estimated["avoided_by_trim"] > 5000 and estimated["overhead"] > 0
    assert estimated["net"] == estimated["avoided_by_gate"] + estimated["avoided_by_trim"] - estimated["overhead"]
    measured = summary["measured"]
    assert measured["available"] and measured["pairs"] == 3 and measured["mean_change_pct"] == -11.7
    assert measured["ci_low_pct"] <= measured["mean_change_pct"] <= measured["ci_high_pct"]
    assert (measured["observe_pass"], measured["enforce_pass"]) == (3, 3)
    assert summary["task"]["ctx_budget"] > 0 and summary["task"]["mode"] == "enforce"

    gate = json.loads(get(served, "/api/gate")[1])
    assert gate["counts"]["rewrite"] == 1 and gate["counts"]["deny"] == 1
    assert {r["rule"] for r in gate["top_rules"]} == {"grep_head_limit", "bash_full_suite"}
    assert set(gate["recent"][0]) == {"ts", "tool", "verdict", "rule", "reason", "tokens_avoided_est", "latency_ms"}

    failures = json.loads(get(served, "/api/failures")[1])["fingerprints"]
    assert failures[0]["count"] == 1 and failures[0]["failure_class"] == "assertion_local"
    assert isinstance(failures[0]["actions_tried"], list) and isinstance(failures[0]["resolved_by"], dict)

    recovery = json.loads(get(served, "/api/recovery")[1])["events"]
    assert recovery[0]["decided_by"] == "auto" and isinstance(recovery[0]["router_scores"], dict)

    connections = json.loads(get(served, "/api/connections")[1])
    assert "hooks" in {c["name"] for c in connections["connections"]}
    assert connections["transcript"]["session_id"] == "session-1" and "ratio" in connections["calibration"]

    assert len(json.loads(get(served, "/api/tasks")[1])["tasks"]) == 1
    ab = json.loads(get(served, "/api/ab")[1])
    assert len(ab["runs"]) == 6 and ab["summary"]["experiment"] == "ab_1"


def test_page_has_no_external_urls_and_all_panels(served):
    response, body = get(served, "/")
    assert response.status == 200 and response.getheader("Content-Type").startswith("text/html")
    assert re.findall(r"https?://[^\s\"'<>)]+", body) == []
    assert "Access-Control-Allow-Origin" not in dict(response.getheaders())
    assert "default-src 'none'" in response.getheader("Content-Security-Policy")
    for panel in ("Status", "Tokens", "Savings", "Scope &amp; budget", "Gate", "Failure fingerprints", "Recovery",
                  "Connections"):
        assert f"<h2>{panel}" in body
    assert "prefers-color-scheme: dark" in body and "#e8f5e9" in body and "#2e7d32" in body
    assert "setInterval(refresh,3000)" in body and "estimated" in body.lower()


def test_binds_loopback_only_and_rejects_foreign_hosts(served):
    assert get(served, "/api/summary", host="evil.example.com")[0].status == 403
    assert get(served, "/api/summary", host=f"localhost:{served}")[0].status == 200
    assert get(served, "/api/nope")[0].status == 404
    httpd = dash.make_server(18765)  # port taken by the fixture: the next one is used
    try:
        assert httpd.server_address == ("127.0.0.1", served + 1)
    finally:
        httpd.server_close()


def test_database_is_opened_read_only(seeded):
    conn = dash.connect()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM tasks_bp")
    conn.close()


def test_process_lifecycle_and_bp_dashboard_command(tmp_path):
    home = state.get_bp_home()
    try:
        url = dashboard.ensure_running(port=18790)
        assert re.fullmatch(r"http://127\.0\.0\.1:187\d\d", url)
        pid, port = dashboard.running()
        assert json.loads(get(port, "/api/summary")[1])["version"]
        assert dashboard.ensure_running(port=18790) == url and dashboard.running()[0] == pid   # reused, not respawned
        # typing bp_dashboard in Claude Code returns the same URL without reaching the model
        repo = make_repo(tmp_path / "repo")
        _, _, out = run_hook(fixture("UserPromptSubmit", repo, prompt="bp_dashboard"), home, mode=None)
        assert out["decision"] == "block" and out["reason"] == f"BudgetPilot dashboard → {url}"
    finally:
        assert dashboard.stop() is True
    deadline = time.time() + 5
    while time.time() < deadline and dashboard.running():
        time.sleep(0.1)
    assert dashboard.running() is None and dashboard.stop() is False

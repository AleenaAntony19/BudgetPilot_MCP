"""Phase B: billing detection, the key-set warning, report_policy, and dollars only where allowed."""

import json
import sqlite3
import time

import pytest

from budgetpilot import cc_cli
from budgetpilot.cli import main, run_report
from budgetpilot.core import cc_ledger, hookdb, state
from budgetpilot.hosts import get_adapter
from budgetpilot.hosts.base import BillingContext, HostCapabilities
from budgetpilot.hosts.billing import (KEY_VARS, detect_claude_code, key_warning, read_auth_cache,
                                       save_auth_cache)
from budgetpilot.hosts.policy import money, report_policy, strip_dollars
from tests.hookutil import TRANSCRIPT, db, fixture, make_repo, run_hook

SECRET = "sk-ant-api03-THIS-IS-A-TEST-SECRET-0123456789"
TOKEN = "bearer-TEST-TOKEN-ABCDEFGHIJ-9876543210"
URL = "https://user:p4ss-TEST-URL-SECRET@gateway.example.com/v1"
PLAN = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}
SECRETS = (SECRET, TOKEN, "p4ss-TEST-URL-SECRET", "gateway.example.com")


def _plan(age_s=0):
    return {**PLAN, "checked_at": time.time() - age_s}


def _detect(env, cwd="", config=None, auth=None):
    return detect_claude_code(env, cwd=cwd, config=config, auth={} if auth is None else auth)


# ---------------------------------------------------------------- detection matrix
@pytest.mark.parametrize("env,auth,mode,provider,confidence", [
    ({}, None, "unknown", "unknown", "low"),
    ({}, _plan(), "subscription", "anthropic", "high"),
    ({}, _plan(3 * 86400), "subscription", "anthropic", "medium"),
    ({}, _plan(30 * 86400), "subscription", "anthropic", "low"),
    ({"ANTHROPIC_API_KEY": SECRET}, _plan(), "api", "anthropic", "medium"),
    ({"ANTHROPIC_API_KEY": SECRET, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, _plan(), "api", "anthropic", "high"),
    ({"ANTHROPIC_AUTH_TOKEN": TOKEN}, _plan(), "api", "anthropic", "medium"),
    ({"ANTHROPIC_BASE_URL": URL}, _plan(), "api", "unknown", "low"),
    ({"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_API_KEY": SECRET}, _plan(), "cloud", "bedrock", "high"),
    ({"CLAUDE_CODE_USE_MANTLE": "true"}, None, "cloud", "bedrock", "high"),
    ({"CLAUDE_CODE_USE_VERTEX": "1"}, None, "cloud", "vertex", "high"),
    ({"CLAUDE_CODE_USE_FOUNDRY": "1"}, None, "cloud", "azure", "high"),
    ({"CLAUDE_CODE_USE_ANTHROPIC_AWS": "1"}, None, "cloud", "anthropic", "high"),
    ({"CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD": "1"}, None, "cloud", "anthropic", "high"),
    ({"CLAUDE_CODE_USE_BEDROCK": "0"}, _plan(), "subscription", "anthropic", "high"),
    ({"CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"}, _plan(), "subscription", "anthropic", "low"),
    ({}, {"loggedIn": True, "authMethod": "third_party", "apiProvider": "bedrock"}, "unknown", "unknown", "low"),
])
def test_detection_matrix(env, auth, mode, provider, confidence):
    billing = _detect(env, auth=auth)
    assert (billing.mode, billing.provider, billing.confidence, billing.source) == (mode, provider, confidence,
                                                                                   "detected")
    assert billing.evidence or mode == "unknown"
    text = repr(billing)
    assert not any(s in text for s in SECRETS)


def test_evidence_names_variables_and_never_values():
    billing = _detect({"ANTHROPIC_API_KEY": SECRET, "ANTHROPIC_AUTH_TOKEN": TOKEN, "ANTHROPIC_BASE_URL": URL})
    assert "env:ANTHROPIC_API_KEY present" in billing.evidence
    assert "env:ANTHROPIC_AUTH_TOKEN present" in billing.evidence
    assert "env:ANTHROPIC_BASE_URL present (remote)" in billing.evidence
    local = _detect({"ANTHROPIC_BASE_URL": "http://127.0.0.1:8899"})
    assert "env:ANTHROPIC_BASE_URL present (this machine)" in local.evidence


def test_cloud_flag_wins_over_key_and_lists_both():
    billing = _detect({"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_API_KEY": SECRET})
    assert billing.mode == "cloud" and billing.evidence[0].startswith("env:CLAUDE_CODE_USE_VERTEX=1")


def test_api_key_helper_in_project_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"apiKeyHelper": f"echo {SECRET}"}))
    billing = _detect({}, cwd=str(tmp_path), auth=_plan())
    assert billing.mode == "api" and "settings:project apiKeyHelper present" in billing.evidence
    assert SECRET not in repr(billing)


def test_user_override_always_wins_and_is_marked():
    cfg = {"billing": {"mode_override": "api", "provider_override": "bedrock"}}
    billing = _detect({}, config=cfg, auth=_plan())
    assert (billing.mode, billing.provider, billing.source, billing.confidence) == ("api", "bedrock",
                                                                                    "user_override", "high")
    assert billing.evidence[0] == "config:billing.mode_override=api"
    ignored = _detect({}, config={"billing": {"mode_override": "free"}}, auth=_plan())
    assert ignored.mode == "subscription" and ignored.source == "detected"


def test_auth_cache_keeps_only_safe_fields(tmp_path):
    path = tmp_path / "claude_auth.json"
    saved = save_auth_cache({**PLAN, "email": "someone@example.com", "orgId": "org-123", "orgName": "Acme",
                             "loginExpiresAt": "2027-01-01"}, "2.1.328 (Claude Code)", path=path)
    text = path.read_text()
    assert "example.com" not in text and "org-123" not in text and "Acme" not in text
    assert read_auth_cache(path)["subscriptionType"] == "pro" and saved["claude_version"].startswith("2.1.328")


def test_key_warning_only_when_the_user_said_plan():
    assert key_warning({"ANTHROPIC_API_KEY": SECRET}, {"billing": {"intended_mode": "subscription"}}) == [
        "ANTHROPIC_API_KEY"]
    assert key_warning({"ANTHROPIC_API_KEY": SECRET}, {"billing": {"intended_mode": "api"}}) == []
    assert key_warning({"ANTHROPIC_API_KEY": SECRET}, {}) == []
    assert key_warning({}, {"billing": {"intended_mode": "subscription"}}) == []


# ---------------------------------------------------------------- hooks: SessionStart / SessionEnd
def _hook_env(tmp_path, **extra):
    env = {name: "" for name in KEY_VARS}
    env.update(ANTHROPIC_BASE_URL="", CLAUDE_CONFIG_DIR=str(tmp_path / "claude-config"))
    env.update(extra)
    return env


def _intend(bp_home, mode):
    bp_home.mkdir(parents=True, exist_ok=True)
    (bp_home / "config.json").write_text(json.dumps({"billing": {"intended_mode": mode}}))


def _no_secret_anywhere(bp_home):
    for path in bp_home.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert not any(s.encode() in data for s in SECRETS), path


def test_session_start_warns_once_and_records_billing(tmp_path):
    repo, home = make_repo(tmp_path / "repo"), tmp_path / "home"
    _intend(home, "subscription")
    env = _hook_env(tmp_path, ANTHROPIC_API_KEY=SECRET)
    code, _, out = run_hook(fixture("SessionStart", repo), home, mode="enforce", env_extra=env)
    assert code == 0 and "ANTHROPIC_API_KEY is set" in out["systemMessage"]
    assert "billed per token" in out["hookSpecificOutput"]["additionalContext"]
    _, _, again = run_hook(fixture("SessionStart", repo, source="clear"), home, mode="enforce", env_extra=env)
    assert again is None or "systemMessage" not in again                 # once per session
    row = dict(db(home).execute("SELECT billing_mode, billing_provider, billing_confidence, billing_evidence, "
                                "billing_warned FROM cc_sessions").fetchone())
    assert row["billing_mode"] == "api" and row["billing_warned"] == 1
    assert "env:ANTHROPIC_API_KEY present" in json.loads(row["billing_evidence"])
    _no_secret_anywhere(home)


def test_observe_mode_warns_the_user_without_adding_model_context(tmp_path):
    repo, home = make_repo(tmp_path / "repo"), tmp_path / "home"
    _intend(home, "subscription")
    _, _, out = run_hook(fixture("SessionStart", repo), home, mode="observe",
                         env_extra=_hook_env(tmp_path, ANTHROPIC_API_KEY=SECRET))
    assert "systemMessage" in out and "hookSpecificOutput" not in out


def test_no_warning_without_a_plan_intent_or_without_a_key(tmp_path):
    repo = make_repo(tmp_path / "repo")
    home = tmp_path / "h1"
    _intend(home, "api")
    _, _, out = run_hook(fixture("SessionStart", repo), home, env_extra=_hook_env(tmp_path, ANTHROPIC_API_KEY=SECRET))
    assert out is None or "systemMessage" not in out
    home2 = tmp_path / "h2"
    _intend(home2, "subscription")
    _, _, out2 = run_hook(fixture("SessionStart", repo), home2, env_extra=_hook_env(tmp_path))
    assert out2 is None or "systemMessage" not in out2


def test_billing_change_at_session_end_marks_the_session_mixed(tmp_path):
    repo, home = make_repo(tmp_path / "repo"), tmp_path / "home"
    run_hook(fixture("SessionStart", repo), home, env_extra=_hook_env(tmp_path))
    code, out, _ = run_hook(fixture("SessionEnd", repo), home, env_extra=_hook_env(tmp_path, ANTHROPIC_API_KEY=SECRET))
    assert code == 0 and out == ""                                       # SessionEnd prints nothing
    row = db(home).execute("SELECT billing_mode, billing_mode_end, billing_mixed FROM cc_sessions").fetchone()
    assert tuple(row) == ("unknown", "api", 1)


def test_unchanged_billing_is_not_mixed(tmp_path):
    repo, home = make_repo(tmp_path / "repo"), tmp_path / "home"
    env = _hook_env(tmp_path, ANTHROPIC_API_KEY=SECRET)
    run_hook(fixture("SessionStart", repo), home, env_extra=env)
    run_hook(fixture("SessionEnd", repo), home, env_extra=env)
    assert db(home).execute("SELECT billing_mixed FROM cc_sessions").fetchone()[0] == 0


def test_first_prompt_detects_billing_when_session_start_was_missed(tmp_path):
    repo, home = make_repo(tmp_path / "repo"), tmp_path / "home"
    run_hook(fixture("UserPromptSubmit", repo), home, env_extra=_hook_env(tmp_path, CLAUDE_CODE_USE_VERTEX="1"))
    assert db(home).execute("SELECT billing_mode, billing_provider FROM cc_sessions").fetchone()[:] == (
        "cloud", "vertex")


def test_mode_off_detects_nothing(tmp_path):
    repo, home = make_repo(tmp_path / "repo"), tmp_path / "home"
    _intend(home, "subscription")
    code, out, _ = run_hook(fixture("SessionStart", repo), home, mode="off",
                            env_extra=_hook_env(tmp_path, ANTHROPIC_API_KEY=SECRET))
    assert code == 0 and out == "" and not (home / "bp.db").exists()


# ---------------------------------------------------------------- report_policy
@pytest.mark.parametrize("mode", ["subscription", "unknown"])
def test_policy_hides_dollars_without_per_token_billing(mode):
    p = report_policy(BillingContext(mode))
    assert not p["show_dollars"] and p["dollars_label"] is None and "not dollars" in p["units_label"]
    assert money(1.23, p) is None and strip_dollars({"a": {"ref_usd": 1, "x": 2}}, p) == {"a": {"x": 2}}


@pytest.mark.parametrize("mode", ["api", "cloud"])
def test_policy_shows_estimated_dollars_for_per_token_billing(mode):
    p = report_policy(BillingContext(mode, "anthropic", "high"))
    assert p["show_dollars"] and p["dollars_label"] == "API-equivalent estimate" and not p["reconciled"]
    assert money(1.5, p) == "$1.5000"
    assert report_policy(BillingContext(mode), reconciled=True)["dollars_label"] == "API-equivalent, reconciled"


def test_policy_mixed_session_shows_no_dollars():
    p = report_policy(BillingContext("api"), mixed=True)
    assert not p["show_dollars"] and "changed" in p["note"]


def test_policy_hides_gate_and_recovery_for_hosts_without_hooks():
    assert not report_policy(None, HostCapabilities(tier="tools"))["show_gate"]
    assert report_policy(None, get_adapter("claude_code").capabilities())["show_recovery"]


# ---------------------------------------------------------------- every surface follows the policy
def _ledger_with_sessions(monkeypatch, tmp_path, sessions):
    """sessions: {session_id: (billing_mode, mixed)}; each gets the fixture transcript's 12 turns."""
    monkeypatch.setenv("BP_HOME", str(tmp_path / "home"))
    conn = hookdb.open_ready()
    for i, (sid, (mode, mixed)) in enumerate(sessions.items()):
        today = time.strftime("%Y-%m-%d", time.gmtime())
        data = (TRANSCRIPT.read_text().replace('"id": "msg_', f'"id": "msg_{i}_')
                .replace('"timestamp": "2026-11-14', f'"timestamp": "{today}')
                .replace('"timestamp": "2026-11-15', f'"timestamp": "{today}'))
        path = tmp_path / f"{sid}.jsonl"
        path.write_text(data)
        hookdb.touch_session(conn, sid, str(tmp_path), str(path), "cli")
        conn.execute("UPDATE cc_sessions SET billing_mode=?, billing_mixed=?, last_seen=? WHERE session_id=?",
                     (mode, int(mixed), time.time() + i, sid))
        cc_ledger.ingest(conn, sid, str(path))
    return conn


def _dashboard_summary(conn):
    from budgetpilot.dashboard import server

    return server.api_summary(conn)


@pytest.mark.parametrize("mode", ["subscription", "unknown"])
def test_no_dollar_string_anywhere_for_plan_sessions(mode, tmp_path, monkeypatch, capsys):
    conn = _ledger_with_sessions(monkeypatch, tmp_path, {"s-plan": (mode, False)})
    summary = _dashboard_summary(conn)
    assert "ref_usd" not in summary["tokens_session"] and "ref_usd" not in summary["tokens_24h"]
    assert "$" not in json.dumps({k: summary[k] for k in ("tokens_session", "tokens_24h", "policy", "billing")})

    assert run_report() == 0
    text = capsys.readouterr().out
    assert "Claude Code usage report" in text and "$" not in text
    assert run_report(format_type="json") == 0
    assert "ref_usd" not in capsys.readouterr().out

    from budgetpilot.core.commands import status_text

    assert "$" not in status_text() and "no dollar figures" in status_text()

    from budgetpilot.bench.ab import render_results

    archived = json.loads((TRANSCRIPT.parents[2].parent / "docs" / "results" / "ab_20261115_160545.json").read_text())
    archived["billing"] = {"mode": mode}
    rendered = render_results(archived)
    assert "$" not in rendered and "relative weighting, not dollars" in rendered


def test_api_session_shows_dollars_labelled_as_estimate(tmp_path, monkeypatch, capsys):
    conn = _ledger_with_sessions(monkeypatch, tmp_path, {"s-api": ("api", False)})
    summary = _dashboard_summary(conn)
    assert summary["tokens_session"]["ref_usd"] == pytest.approx(0.01284568, rel=1e-6)
    assert summary["policy"]["dollars_label"] == "API-equivalent estimate"
    assert run_report() == 0
    assert "API-equivalent estimate: $0.0128" in capsys.readouterr().out


def test_mixed_sessions_are_left_out_of_dollar_totals(tmp_path, monkeypatch, capsys):
    conn = _ledger_with_sessions(monkeypatch, tmp_path, {"s-ok": ("api", False), "s-mixed": ("api", True),
                                                         "s-plan": ("subscription", False)})
    summary = _dashboard_summary(conn)
    assert summary["dollar_sessions_24h"] == 1
    assert summary["tokens_24h"]["ref_usd"] == pytest.approx(0.01284568, rel=1e-6)   # one session's worth
    assert summary["tokens_24h"]["turns"] == 36                                         # tokens: all three
    assert run_report(format_type="json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["dollar_sessions"] == 1 and data["totals"]["ref_usd"] == pytest.approx(0.01284568, abs=1e-6)
    mixed = next(s for s in data["sessions"] if s["session_id"] == "s-mixed")
    assert "ref_usd" not in mixed and mixed["billing"]["mixed"] is True


def test_transcript_check_follows_the_policy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    for name in KEY_VARS + ("ANTHROPIC_BASE_URL",):
        monkeypatch.delenv(name, raising=False)
    assert main(["transcript-check", str(TRANSCRIPT)]) == 0
    out = capsys.readouterr().out
    assert "$" not in out and "relative weighting, not dollars" in out
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    assert main(["transcript-check", str(TRANSCRIPT)]) == 0
    out = capsys.readouterr().out
    assert "API-equivalent estimate $0.0128" in out and SECRET not in out
    assert main(["transcript-check", str(TRANSCRIPT), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["policy"]["show_dollars"] is True


def test_legacy_report_is_unchanged_without_claude_code_data(capsys):
    assert run_report() == 0
    assert capsys.readouterr().out.strip() == "No sessions logged."


# ---------------------------------------------------------------- doctor and install
def _quiet_doctor(monkeypatch):
    monkeypatch.setattr(cc_cli, "find_claude", lambda: None)
    monkeypatch.setattr(cc_cli, "measure_hooks", lambda runs: {"ok": True, "errors": [], "read_rewrite_limit": 400,
                                                               "timings": {"x": {"p50": 1.0, "p95": 2.0, "n": 1}}})
    monkeypatch.setattr(cc_cli, "mcp_handshake", lambda: {"ok": True, "tools": ["a"], "schema_tokens_est": 10,
                                                          "longest_description_words": 5})


def test_doctor_fails_the_key_check_by_name_when_the_user_said_plan(tmp_path, monkeypatch, capsys):
    _quiet_doctor(monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    cc_cli.set_intended_billing("subscription")
    assert cc_cli.run_doctor(claude=False) == 1
    out = capsys.readouterr().out
    assert "[FAIL] ANTHROPIC_API_KEY is set" in out and "fix: unset ANTHROPIC_API_KEY" in out
    assert "Billing detection: api" in out and SECRET not in out


def test_doctor_accepts_a_key_when_the_user_said_api(tmp_path, monkeypatch, capsys):
    _quiet_doctor(monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    cc_cli.set_intended_billing("api")
    cc_cli.run_doctor(claude=False)
    assert "[PASS] ANTHROPIC_API_KEY is set" in capsys.readouterr().out


def test_install_records_the_intended_billing(tmp_path, monkeypatch):
    monkeypatch.setattr(cc_cli, "find_claude", lambda: None)
    repo = make_repo(tmp_path / "repo")
    assert cc_cli.run_install(scope="project", repo=str(repo), hooks=True, yes=True, billing="subscription",
                              out=lambda _: None) == 0
    assert state.read_config()["billing"]["intended_mode"] == "subscription"

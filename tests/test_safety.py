"""
Unit and integration tests for BudgetPilot Safety & Hardening (Phase O).
Tests cover:
1. Global kill switch (BP_MODE=off) enforcement and bypass.
2. Automated secret redaction prior to SQLite storage and fallback logging.
3. Hard per-task cap guards (max denials, recovery attempts, turns, escalation USD).
4. Network egress safety audit (only local Ollama host connections permitted).
"""

import json
import os
from pathlib import Path
import socket
import tempfile
import pytest

from budgetpilot.core.gate import check as gate_check
from budgetpilot.core.safety import (
    CapCheckResult,
    CapViolationError,
    NetworkAuditContext,
    TaskCapGuard,
    assert_network_safety,
    enforce_kill_switch,
    is_allowed_network_target,
    is_kill_switch_active,
    is_secret_present,
    redact_data,
    redact_secrets,
    set_kill_switch,
)
from budgetpilot.core.store import get_connection, init_db, log_fallback_error, safe_execute_write
from budgetpilot.core.tracker import TaskState
from budgetpilot.core.types import BpEvent, GateVerdictStatus


# ===========================================================================
# 1. Global Kill Switch (BP_MODE=off) Tests
# ===========================================================================

def test_kill_switch_detection(monkeypatch):
    """Verify is_kill_switch_active detects BP_MODE=off from environment."""
    monkeypatch.setenv("BP_MODE", "enforce")
    assert not is_kill_switch_active()
    assert not enforce_kill_switch()

    monkeypatch.setenv("BP_MODE", "off")
    assert is_kill_switch_active()
    assert enforce_kill_switch()

    monkeypatch.setenv("BP_MODE", "observe")
    assert not is_kill_switch_active()


def test_set_kill_switch():
    """Verify programmatic setting and clearing of kill switch."""
    set_kill_switch(True)
    assert is_kill_switch_active()
    assert os.environ.get("BP_MODE") == "off"

    set_kill_switch(False)
    assert not is_kill_switch_active()


def test_kill_switch_bypasses_gate(monkeypatch):
    """
    Verify that when BP_MODE=off is active, ExecuteGate immediately
    returns ALLOW verdict without enforcing scope restrictions.
    """
    monkeypatch.setenv("BP_MODE", "off")

    state = TaskState(session_id="kill_switch_sess")
    state.metadata["scope"] = 1
    # Add 10 reads (normally Scope 1 permits max 3)
    for i in range(10):
        state.read_set.add(f"file_{i}.py")

    event = BpEvent(
        event_type="read_file",
        payload={"tool_name": "read_file", "arguments": {"path": "unauthorized.py", "start": 1, "end": 1000}},
    )

    verdict = gate_check(event, state)
    assert verdict.status == GateVerdictStatus.ALLOW
    assert "off" in verdict.reason.lower()


# ===========================================================================
# 2. Automated Secret Redaction Tests
# ===========================================================================

def test_redact_aws_keys():
    """Verify detection and redaction of AWS access key IDs and secret keys."""
    raw = "Deploy using AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE and aws_secret_access_key='wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'"
    redacted = redact_secrets(raw)

    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "[REDACTED_AWS_KEY]" in redacted
    assert "[REDACTED_AWS_SECRET]" in redacted
    assert is_secret_present(raw)


def test_redact_github_tokens():
    """Verify detection and redaction of GitHub personal access tokens."""
    token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    raw = f"git clone https://{token}@github.com/org/repo.git"
    redacted = redact_secrets(raw)

    assert token not in redacted
    assert "[REDACTED_GITHUB_TOKEN]" in redacted
    assert is_secret_present(raw)


def test_redact_bearer_tokens():
    """Verify detection and redaction of Authorization Bearer strings."""
    raw = "curl -H 'Authorization: Bearer secret_bearer_token_1234567890' http://api.internal"
    redacted = redact_secrets(raw)

    assert "secret_bearer_token_1234567890" not in redacted
    assert "Bearer [REDACTED_BEARER_TOKEN]" in redacted
    assert is_secret_present(raw)


def test_redact_slack_tokens():
    """Verify detection and redaction of Slack API bot/user tokens."""
    token = "slack_demo_token_1234567890"
    raw = f"Notify Slack channel with token {token}"
    redacted = redact_secrets(raw)

    assert token not in redacted
    assert "[REDACTED_SLACK_TOKEN]" in redacted
    assert is_secret_present(raw)


def test_redact_private_keys():
    """Verify detection and redaction of PEM private keys."""
    raw = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA0Y8v...test_private_key_data...\n"
        "-----END RSA PRIVATE KEY-----"
    )
    redacted = redact_secrets(raw)

    assert "test_private_key_data" not in redacted
    assert "[REDACTED_PRIVATE_KEY]" in redacted
    assert is_secret_present(raw)


def test_redact_data_recursive_structures():
    """Verify recursive redaction across nested dictionaries, lists, and tuples."""
    payload = {
        "user": "developer",
        "env": {
            "AWS_KEY": "AKIA1111222233334444",
            "TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        },
        "commands": [
            "echo Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.do_not_leak",
        ],
        "tuple_val": ("api_key='super_secret_api_key_12345'", 42),
    }

    cleaned = redact_data(payload)

    assert cleaned["env"]["AWS_KEY"] == "[REDACTED_AWS_KEY]"
    assert cleaned["env"]["TOKEN"] == "[REDACTED_GITHUB_TOKEN]"
    assert "Bearer [REDACTED_BEARER_TOKEN]" in cleaned["commands"][0]
    assert "[REDACTED_SECRET]" in cleaned["tuple_val"][0]
    assert cleaned["tuple_val"][1] == 42


def test_sqlite_storage_redaction_guarantee(tmp_path):
    """
    Verify that writes to SQLite through safe_execute_write automatically
    redact secrets, guaranteeing no raw credentials are ever persisted.
    """
    db_file = tmp_path / "safety_test.db"
    init_db(db_file)

    secret_key = "AKIA9999888877776666"
    bearer = "Bearer token_secret_string_9876543210"

    safe_execute_write(
        conn_or_path=db_file,
        sql="INSERT INTO sessions (session_id, mode, status, metadata) VALUES (?, 'enforce', 'active', ?);",
        params=("sess_safe", json.dumps({"aws_key": secret_key, "auth": bearer})),
    )

    conn = get_connection(db_file)
    try:
        row = conn.execute("SELECT metadata FROM sessions WHERE session_id = 'sess_safe';").fetchone()
        assert row is not None
        stored_meta = row["metadata"]

        # Raw secrets MUST NOT exist in database
        assert secret_key not in stored_meta
        assert "token_secret_string_9876543210" not in stored_meta

        # Redacted placeholders MUST exist
        assert "[REDACTED_AWS_KEY]" in stored_meta
        assert "[REDACTED_BEARER_TOKEN]" in stored_meta
    finally:
        conn.close()


def test_fallback_log_redaction_guarantee(tmp_path):
    """Verify that fallback.jsonl error logs redact secrets."""
    fallback_file = tmp_path / "fallback.jsonl"
    secret = "AKIA0000111122223333"

    log_fallback_error(
        error=ValueError(f"Invalid credentials with key {secret}"),
        sql="INSERT INTO test VALUES (?)",
        params=(secret,),
        fallback_path=fallback_file,
    )

    content = fallback_file.read_text(encoding="utf-8")
    assert secret not in content
    assert "[REDACTED_AWS_KEY]" in content


# ===========================================================================
# 3. Hard Per-Task Cap Guards Tests
# ===========================================================================

def test_task_cap_guard_denials():
    """Verify TaskCapGuard checks and enforces max_denials cap."""
    guard = TaskCapGuard(max_denials=3)

    assert guard.check_denials(2).allowed is True
    assert guard.check_denials(3).allowed is True

    # Check without raising
    res = guard.check_denials(4, raise_on_violation=False)
    assert res.allowed is False
    assert res.cap_name == "max_denials"
    assert "Exceeded max denials cap" in res.reason

    # Check with raising
    with pytest.raises(CapViolationError) as exc_info:
        guard.check_denials(4, raise_on_violation=True)
    assert exc_info.value.cap_name == "max_denials"
    assert exc_info.value.limit == 3
    assert exc_info.value.current == 4


def test_task_cap_guard_recovery_attempts():
    """Verify TaskCapGuard checks and enforces max_recovery_attempts cap."""
    guard = TaskCapGuard(max_recovery_attempts=2)

    assert guard.check_recovery_attempts(1).allowed is True
    assert guard.check_recovery_attempts(2).allowed is True

    with pytest.raises(CapViolationError) as exc_info:
        guard.check_recovery_attempts(3)
    assert exc_info.value.cap_name == "max_recovery_attempts"


def test_task_cap_guard_turns():
    """Verify TaskCapGuard checks and enforces max_turns cap."""
    guard = TaskCapGuard(max_turns=10)

    assert guard.check_turns(9).allowed is True
    assert guard.check_turns(10).allowed is True

    with pytest.raises(CapViolationError) as exc_info:
        guard.check_turns(11)
    assert exc_info.value.cap_name == "max_turns"


def test_task_cap_guard_escalation_usd():
    """Verify TaskCapGuard checks and enforces max_escalation_usd cap."""
    guard = TaskCapGuard(max_escalation_usd=0.50)

    assert guard.check_escalation_cost(0.25).allowed is True
    assert guard.check_escalation_cost(0.50).allowed is True

    with pytest.raises(CapViolationError) as exc_info:
        guard.check_escalation_cost(0.51)
    assert exc_info.value.cap_name == "max_escalation_usd"


def test_task_cap_guard_evaluate_task_state():
    """Verify evaluate_task_state composite check."""
    guard = TaskCapGuard(max_denials=5, max_recovery_attempts=3, max_turns=20, max_escalation_usd=1.0)

    # Safe state
    res_safe = guard.evaluate_task_state(denials=2, recovery_attempts=1, turns=8, escalation_cost_usd=0.15)
    assert res_safe.allowed is True

    # Breach turns
    res_breach = guard.evaluate_task_state(denials=2, recovery_attempts=1, turns=25, escalation_cost_usd=0.15)
    assert res_breach.allowed is False
    assert res_breach.cap_name == "max_turns"


# ===========================================================================
# 4. Network Egress Safety Audit Tests
# ===========================================================================

def test_allowed_network_target_validation(monkeypatch):
    """
    Verify that only local/loopback and configured Ollama endpoints are allowed.
    All external third-party hosts must be strictly denied.
    """
    # Local loopbacks are allowed
    assert is_allowed_network_target("localhost") is True
    assert is_allowed_network_target("127.0.0.1") is True
    assert is_allowed_network_target("127.0.0.1:11434") is True
    assert is_allowed_network_target("0.0.0.0") is True
    assert is_allowed_network_target("::1") is True

    # Custom local OLLAMA_HOST
    monkeypatch.setenv("OLLAMA_HOST", "http://my-local-ollama:11434")
    assert is_allowed_network_target("my-local-ollama") is True

    # External unauthorized endpoints MUST be rejected
    assert is_allowed_network_target("api.openai.com") is False
    assert is_allowed_network_target("api.anthropic.com") is False
    assert is_allowed_network_target("telemetry.google.com") is False
    assert is_allowed_network_target("8.8.8.8") is False
    assert is_allowed_network_target("142.250.190.46") is False


def test_assert_network_safety():
    """Verify assert_network_safety raises PermissionError for external hosts."""
    # Local passes without error
    assert_network_safety("localhost", 11434)
    assert_network_safety("127.0.0.1", 11434)

    # External raises PermissionError
    with pytest.raises(PermissionError) as exc_info:
        assert_network_safety("external-analytics.com", 443)
    assert "Unauthorized external network connection" in str(exc_info.value)


def test_network_audit_guard_socket_interception():
    """
    Verify NetworkAuditContext intercepts socket.connect and blocks
    any attempted connection to external non-local addresses.
    """
    with NetworkAuditContext() as audit:
        # 1. Attempt connection to unauthorized external host
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(PermissionError) as exc_info:
                s.connect(("api.external-cloud.com", 443))
            assert "blocked connection to external host" in str(exc_info.value)
        finally:
            s.close()

        # Audit recorded the attempt
        assert any(h == "api.external-cloud.com" for h, p in audit.connections_attempted)

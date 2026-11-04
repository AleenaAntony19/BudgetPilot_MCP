"""
Unit and integration tests for BudgetPilot Verification and Test Diagnostic Subsystem (Phase F).
Tests cover:
1. All 7 failure classes:
   - syntax_or_lint
   - assertion_local
   - symbol_outside_touched
   - regression_elsewhere
   - repeat_signature
   - capability_gap
   - env_or_flaky
2. Pytest standard output parsing (failing test IDs, stack traces, frame paths, failure messages).
3. Generic runner fallback (exit code + last 60 lines).
4. Stderr truncation (4 KB cap) and secret redaction.
5. Deterministic failure signature hashing.
6. RecoveryContext construction.
7. SQLite verifications persistence.
"""

import json
from pathlib import Path
import pytest

from budgetpilot.core.store import get_connection, init_db
from budgetpilot.core.types import FailureClass, RecoveryContext
from budgetpilot.core.verifier import (
    MAX_STDERR_BYTES,
    VerificationResult,
    compute_signature_hash,
    intercept_test_output,
    parse_pytest_output,
    redact_and_truncate_stderr,
    verify,
)

# ---------------------------------------------------------------------------
# Pytest Output Fixtures for the 7 Failure Classes
# ---------------------------------------------------------------------------

PYTEST_SYNTAX_ERROR = """
============================= test session starts ==============================
rootdir: /home/aleena/workspace
collected 0 items / 1 error

==================================== ERRORS ====================================
________________________ ERROR collecting tests/test_calc.py ___________________
calc.py:12: in <module>
    def add(x, y)
E     SyntaxError: expected ':'
=========================== short test summary info ============================
ERROR tests/test_calc.py - SyntaxError: expected ':'
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
=============================== 1 error in 0.05s ===============================
"""

PYTEST_ASSERTION_LOCAL = """
============================= test session starts ==============================
collected 1 item

tests/test_calc.py F                                                     [100%]

=================================== FAILURES ===================================
___________________________________ test_add ___________________________________

def test_add():
>       assert add(1, 2) == 4
E       AssertionError: assert 3 == 4

calc.py:8: AssertionError
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_add - AssertionError: assert 3 == 4
============================== 1 failed in 0.08s ===============================
"""

PYTEST_SYMBOL_OUTSIDE_TOUCHED = """
============================= test session starts ==============================
collected 1 item

tests/test_calc.py F                                                     [100%]

=================================== FAILURES ===================================
___________________________________ test_calc __________________________________

def test_calc():
>       result = calculate_with_helper(10)

calc.py:15: in calculate_with_helper
>       return helper_formatter(x)
E       NameError: name 'helper_formatter' is not defined

calc.py:15: NameError
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_calc - NameError: name 'helper_formatter' is not defined
============================== 1 failed in 0.08s ===============================
"""

PYTEST_REGRESSION_ELSEWHERE = """
============================= test session starts ==============================
collected 2 items

tests/test_auth.py .                                                     [ 50%]
tests/test_billing.py F                                                  [100%]

=================================== FAILURES ===================================
_________________________________ test_invoice _________________________________

def test_invoice():
>       assert generate_invoice() == 100
E       AssertionError: assert None == 100

services/billing.py:42: AssertionError
=========================== short test summary info ============================
FAILED tests/test_billing.py::test_invoice - AssertionError: assert None == 100
========================= 1 failed, 1 passed in 0.12s ==========================
"""

PYTEST_CAPABILITY_GAP = """
============================= test session starts ==============================
collected 1 item

tests/test_media.py F                                                    [100%]

=================================== FAILURES ===================================
_________________________________ test_convert _________________________________

def test_convert():
>       subprocess.run(["ffmpeg", "-i", "input.wav", "out.mp3"])
E       FileNotFoundError: [Errno 2] No such file or directory: 'ffmpeg'

media.py:22: FileNotFoundError
=========================== short test summary info ============================
FAILED tests/test_media.py::test_convert - FileNotFoundError: [Errno 2] No such file or directory: 'ffmpeg'
============================== 1 failed in 0.06s ===============================
"""

PYTEST_ENV_OR_FLAKY = """
============================= test session starts ==============================
collected 1 item

tests/test_api.py F                                                      [100%]

=================================== FAILURES ===================================
_________________________________ test_endpoint ________________________________

def test_endpoint():
>       requests.get("https://api.external.com/status", timeout=5)
E       ConnectionError: Failed to establish a new connection: [Errno 111] Connection refused

api.py:30: ConnectionError
=========================== short test summary info ============================
FAILED tests/test_api.py::test_endpoint - ConnectionError: Failed to establish a new connection: [Errno 111] Connection refused
============================== 1 failed in 5.01s ===============================
"""

GENERIC_RUNNER_OUTPUT = """
Compiling test runner...
Running suite: UnitTests
Starting test 1: test_init ... ok
Starting test 2: test_process ... FAIL
Error in file worker.py line 45:
ValueError: Invalid configuration value encountered
Command failed with exit status 1
"""


def test_classify_syntax_or_lint():
    """Verify classification of syntax errors during collection."""
    res = verify(
        test_command="pytest tests/test_calc.py",
        output=PYTEST_SYNTAX_ERROR,
        exit_code=1,
    )
    assert res.passed is False
    assert res.failure_class == FailureClass.SYNTAX_OR_LINT
    assert "SyntaxError" in res.failure_message
    assert res.rerun_directive is False
    assert res.recovery_context is not None
    assert res.recovery_context.failure_class == FailureClass.SYNTAX_OR_LINT


def test_classify_assertion_local():
    """Verify local assertion failures within touched files."""
    res = verify(
        test_command="pytest tests/test_calc.py",
        output=PYTEST_ASSERTION_LOCAL,
        exit_code=1,
        touched_set={"calc.py"},
    )
    assert res.passed is False
    assert res.failure_class == FailureClass.ASSERTION_LOCAL
    assert "AssertionError" in res.failure_message
    assert "tests/test_calc.py::test_add" in res.failing_test_ids
    assert res.rerun_directive is False


def test_classify_symbol_outside_touched():
    """Verify NameError/ImportError referencing outside unread symbols."""
    res = verify(
        test_command="pytest tests/test_calc.py",
        output=PYTEST_SYMBOL_OUTSIDE_TOUCHED,
        exit_code=1,
        touched_set={"calc.py"},
    )
    assert res.passed is False
    assert res.failure_class == FailureClass.SYMBOL_OUTSIDE_TOUCHED
    assert "NameError" in res.failure_message
    assert "helper_formatter" in res.unread_symbols
    assert "helper_formatter" in res.recovery_context.unread_symbols


def test_classify_regression_elsewhere():
    """Verify failure in untouched module is classified as regression elsewhere."""
    res = verify(
        test_command="pytest",
        output=PYTEST_REGRESSION_ELSEWHERE,
        exit_code=1,
        touched_set={"src/auth.py"},  # Agent only touched auth.py, but billing test failed
    )
    assert res.passed is False
    assert res.failure_class == FailureClass.REGRESSION_ELSEWHERE
    assert len(res.regressions) > 0
    assert "tests/test_billing.py::test_invoice" in res.regressions


def test_classify_repeat_signature():
    """Verify exact match of prior failure signature is classified as repeat_signature."""
    # Compute signature for assertion local fixture
    parsed = parse_pytest_output(PYTEST_ASSERTION_LOCAL)
    sig = compute_signature_hash(parsed["error_type"], parsed["top_in_repo_frame"])

    res = verify(
        test_command="pytest tests/test_calc.py",
        output=PYTEST_ASSERTION_LOCAL,
        exit_code=1,
        touched_set={"calc.py"},
        previous_signatures={sig},  # Signature already occurred in earlier attempt
        attempt_number=2,
    )
    assert res.passed is False
    assert res.failure_class == FailureClass.REPEAT_SIGNATURE
    assert res.signature_hash == sig


def test_classify_capability_gap():
    """Verify missing external system dependencies are classified as capability gap."""
    res = verify(
        test_command="pytest tests/test_media.py",
        output=PYTEST_CAPABILITY_GAP,
        exit_code=1,
    )
    assert res.passed is False
    assert res.failure_class == FailureClass.CAPABILITY_GAP
    assert "ffmpeg" in res.failure_message


def test_classify_env_or_flaky():
    """Verify network drops and timeouts are classified as env_or_flaky with rerun directive."""
    res = verify(
        test_command="pytest tests/test_api.py",
        output=PYTEST_ENV_OR_FLAKY,
        exit_code=1,
    )
    assert res.passed is False
    assert res.failure_class == FailureClass.ENV_OR_FLAKY
    assert res.rerun_directive is True  # Rerun via directive, do not route


def test_generic_runner_fallback():
    """Verify generic test runner fallback extracts last 60 lines and identifies errors."""
    res = verify(
        test_command="./run_tests.sh",
        output=GENERIC_RUNNER_OUTPUT,
        exit_code=1,
    )
    assert res.passed is False
    assert "ValueError" in res.failure_message
    assert "worker.py:45" in res.top_in_repo_frame


def test_stderr_truncation_and_redaction():
    """Verify stderr is truncated to 4 KB and credentials are redacted."""
    sensitive_stderr = (
        "Traceback:\n"
        "api_key='sk-1234567890abcdef1234567890'\n"
        "password='supersecretpass123'\n"
        + "E " * 3000  # Exceeds 4 KB
    )
    redacted = redact_and_truncate_stderr(sensitive_stderr, max_chars=MAX_STDERR_BYTES)

    assert "sk-1234567890" not in redacted
    assert "supersecretpass123" not in redacted
    assert "***REDACTED***" in redacted
    assert len(redacted) <= MAX_STDERR_BYTES + 100  # Accounting for notice suffix


def test_signature_hash_determinism():
    """Verify signature hash is deterministic for error type and top in-repo frame."""
    sig1 = compute_signature_hash("AssertionError", "tests/test_calc.py:42:in test_add")
    sig2 = compute_signature_hash("AssertionError", "tests/test_calc.py:42:in test_add")
    sig3 = compute_signature_hash("TypeError", "tests/test_calc.py:42:in test_add")

    assert sig1 == sig2
    assert sig1 != sig3
    assert len(sig1) == 16


def test_sqlite_persistence_verifications(tmp_path: Path):
    """Verify verification records are persisted to SQLite verifications table."""
    db_file = tmp_path / "test_verify.db"
    init_db(db_file)

    res = verify(
        test_command="pytest tests/test_calc.py",
        output=PYTEST_ASSERTION_LOCAL,
        exit_code=1,
        session_id="session_verify_123",
        db_path=db_file,
    )

    conn = get_connection(db_file)
    try:
        cursor = conn.execute(
            """
            SELECT session_id, test_command, passed, exit_code, failure_class, metadata
            FROM verifications
            WHERE session_id = 'session_verify_123';
            """
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["session_id"] == "session_verify_123"
        assert row["passed"] == 0
        assert row["exit_code"] == 1
        assert row["failure_class"] == FailureClass.ASSERTION_LOCAL.value

        meta = json.loads(row["metadata"])
        assert "signature_hash" in meta
        assert meta["signature_hash"] == res.signature_hash
    finally:
        conn.close()


def test_successful_test_run(tmp_path: Path):
    """Verify exit_code 0 produces passed=True with no failure class."""
    db_file = tmp_path / "test_pass.db"
    init_db(db_file)

    res = verify(
        test_command="pytest tests/test_calc.py",
        output="==== 5 passed in 0.20s ====",
        exit_code=0,
        session_id="session_pass_1",
        db_path=db_file,
    )

    assert res.passed is True
    assert res.failure_class is None
    assert res.exit_code == 0
    assert "passed successfully" in res.failure_message


def test_intercept_test_output_tool_call():
    """Verify intercept_test_output convenience wrapper."""
    res = intercept_test_output(
        output=PYTEST_ASSERTION_LOCAL,
        ok=False,
        meta={"command": "pytest", "exit_code": 1},
        session_id="sess_intercept",
    )
    assert res.passed is False
    assert res.failure_class == FailureClass.ASSERTION_LOCAL
    assert res.exit_code == 1

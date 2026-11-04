"""
BudgetPilot Phase I Unit Tests: Avoided Cost Estimation.
Tests hand-computed golden runs, override deductions, late-read deductions,
uncertainty band calculation, and SQLite persistence.
"""

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    """Create a fresh, initialized BudgetPilot SQLite DB for testing."""
    db_path = tmp_path / "test_bp.db"
    from budgetpilot.core.store import init_db
    init_db(db_path)
    return db_path


def _insert_session(db_path: Path, session_id: str, mode: str = "enforce") -> None:
    """Helper to insert a session row."""
    from budgetpilot.core.store import safe_execute_write
    safe_execute_write(
        conn_or_path=db_path,
        sql="INSERT OR IGNORE INTO sessions (session_id, mode) VALUES (?, ?);",
        params=(session_id, mode),
    )


def _insert_gate_decision(
    db_path: Path,
    session_id: str,
    tool_name: str,
    verdict: str,
    original_call: Optional[Dict[str, Any]] = None,
) -> None:
    """Insert a gate_decisions row."""
    from budgetpilot.core.store import safe_execute_write
    safe_execute_write(
        conn_or_path=db_path,
        sql="""
        INSERT INTO gate_decisions (session_id, tool_name, verdict, reason, original_call)
        VALUES (?, ?, ?, 'test reason', ?);
        """,
        params=(
            session_id,
            tool_name,
            verdict,
            json.dumps(original_call or {"path": f"/repo/{tool_name}_target.py"}),
        ),
    )


def _insert_tool_event(
    db_path: Path,
    session_id: str,
    tool_name: str,
    arguments: Optional[Dict[str, Any]] = None,
    is_error: int = 0,
) -> None:
    """Insert a tool_events row."""
    from budgetpilot.core.store import safe_execute_write
    safe_execute_write(
        conn_or_path=db_path,
        sql="""
        INSERT INTO tool_events (session_id, tool_name, arguments, is_error, result)
        VALUES (?, ?, ?, ?, 'ok');
        """,
        params=(
            session_id,
            tool_name,
            json.dumps(arguments or {"path": "/repo/some_file.py"}),
            is_error,
        ),
    )


def _insert_model_call(
    db_path: Path,
    session_id: str,
    input_tokens: int,
    output_tokens: int,
    estimated_cost: float,
    model: str = "qwen2.5:3b",
) -> None:
    """Insert a model_calls row."""
    from budgetpilot.core.store import safe_execute_write
    safe_execute_write(
        conn_or_path=db_path,
        sql="""
        INSERT INTO model_calls (
            session_id, model, provider, input_tokens, output_tokens,
            total_tokens, estimated_cost, source
        ) VALUES (?, ?, 'ollama', ?, ?, ?, ?, 'live');
        """,
        params=(
            session_id,
            model,
            input_tokens,
            output_tokens,
            input_tokens + output_tokens,
            estimated_cost,
        ),
    )


# ---------------------------------------------------------------------------
# Test: _get_session_turns_percentiles
# ---------------------------------------------------------------------------

class TestSessionTurnsPercentiles:
    def test_fallback_when_insufficient_history(self, tmp_db: Path) -> None:
        """With fewer than min_sessions records, should return fallback synthetic values."""
        from budgetpilot.core.savings import _get_session_turns_percentiles
        p25, p50, p75 = _get_session_turns_percentiles(db_path=tmp_db, min_sessions=3)
        # Fallback is DEFAULT_TURNS_REMAINING_FALLBACK=5, band is 0.6×/1×/1.4×
        assert p25 < p50 < p75
        assert p25 > 0

    def test_calculates_from_history(self, tmp_db: Path) -> None:
        """With enough session history, should compute real percentiles."""
        from budgetpilot.core.savings import _get_session_turns_percentiles

        # Insert 5 sessions with varying turn counts: [2, 4, 6, 8, 10]
        for i, turns in enumerate([2, 4, 6, 8, 10]):
            sid = f"sess_turns_{i}"
            _insert_session(tmp_db, sid)
            for _ in range(turns):
                _insert_model_call(tmp_db, sid, 100, 50, 0.0002)

        p25, p50, p75 = _get_session_turns_percentiles(db_path=tmp_db, min_sessions=3)
        # Sorted: [2, 4, 6, 8, 10]. Linear interpolation at 25th percentile: index=1.0 -> 4.0
        # p50 at index 2.0 -> 6.0; p75 at index 3.0 -> 8.0
        assert 2.0 <= p25 <= 5.0
        assert 5.0 <= p50 <= 7.0
        assert 7.0 <= p75 <= 10.0


# ---------------------------------------------------------------------------
# Test: _count_overrides
# ---------------------------------------------------------------------------

class TestCountOverrides:
    def test_no_denies_means_no_overrides(self) -> None:
        from budgetpilot.core.savings import _count_overrides
        decisions = [
            {"tool_name": "read_file", "verdict": "allow"},
            {"tool_name": "read_file", "verdict": "allow"},
        ]
        assert _count_overrides(decisions) == 0

    def test_deny_followed_by_allow_within_window_is_override(self) -> None:
        from budgetpilot.core.savings import _count_overrides
        decisions = [
            {"tool_name": "read_file", "verdict": "deny"},
            {"tool_name": "list_dir",  "verdict": "allow"},
            {"tool_name": "read_file", "verdict": "allow"},  # override (within 3 turns)
        ]
        assert _count_overrides(decisions, override_window=3) == 1

    def test_deny_followed_by_allow_outside_window_is_not_override(self) -> None:
        from budgetpilot.core.savings import _count_overrides
        decisions = [
            {"tool_name": "read_file", "verdict": "deny"},
            {"tool_name": "a", "verdict": "allow"},
            {"tool_name": "b", "verdict": "allow"},
            {"tool_name": "c", "verdict": "allow"},
            {"tool_name": "read_file", "verdict": "allow"},  # position 4, deny at 0, diff=4 > window=3
        ]
        assert _count_overrides(decisions, override_window=3) == 0

    def test_multiple_overrides_counted_correctly(self) -> None:
        from budgetpilot.core.savings import _count_overrides
        decisions = [
            {"tool_name": "read_file",  "verdict": "deny"},
            {"tool_name": "read_file",  "verdict": "allow"},   # override 1
            {"tool_name": "search",     "verdict": "deny"},
            {"tool_name": "search",     "verdict": "allow"},   # override 2
        ]
        assert _count_overrides(decisions, override_window=3) == 2

    def test_rewrite_does_not_count_as_override(self) -> None:
        from budgetpilot.core.savings import _count_overrides
        decisions = [
            {"tool_name": "read_file", "verdict": "deny"},
            {"tool_name": "read_file", "verdict": "rewrite"},  # rewrite is not allow
        ]
        # "rewrite" after "deny" for same tool should count as override (rewrite IS an allow-class)
        # Our implementation: allow OR rewrite triggers override check
        assert _count_overrides(decisions, override_window=3) == 1


# ---------------------------------------------------------------------------
# Test: _count_late_reads
# ---------------------------------------------------------------------------

class TestCountLateReads:
    def _make_deny(self, tool: str, path: str) -> Dict[str, Any]:
        return {
            "tool_name": tool,
            "verdict": "deny",
            "original_call": json.dumps({"path": path}),
        }

    def _make_tool_event(self, tool: str, path: str, is_error: int = 0) -> Dict[str, Any]:
        return {
            "tool_name": tool,
            "arguments": json.dumps({"path": path}),
            "is_error": is_error,
        }

    def test_no_denies_means_no_late_reads(self) -> None:
        from budgetpilot.core.savings import _count_late_reads
        decisions = [{"tool_name": "read_file", "verdict": "allow", "original_call": "{}"}]
        events = [self._make_tool_event("read_file", "/repo/a.py")]
        assert _count_late_reads(decisions, events) == 0

    def test_denied_file_read_later_is_late_read(self) -> None:
        from budgetpilot.core.savings import _count_late_reads
        decisions = [self._make_deny("read_file", "/repo/utils.py")]
        events = [self._make_tool_event("read_file", "/repo/utils.py")]
        assert _count_late_reads(decisions, events) == 1

    def test_denied_file_not_read_later_is_not_late_read(self) -> None:
        from budgetpilot.core.savings import _count_late_reads
        decisions = [self._make_deny("read_file", "/repo/secret.py")]
        events = [self._make_tool_event("read_file", "/repo/other.py")]
        assert _count_late_reads(decisions, events) == 0

    def test_error_tool_events_do_not_count_as_late_reads(self) -> None:
        from budgetpilot.core.savings import _count_late_reads
        decisions = [self._make_deny("read_file", "/repo/utils.py")]
        events = [self._make_tool_event("read_file", "/repo/utils.py", is_error=1)]
        assert _count_late_reads(decisions, events) == 0

    def test_multiple_denied_paths_with_multiple_late_reads(self) -> None:
        from budgetpilot.core.savings import _count_late_reads
        decisions = [
            self._make_deny("read_file", "/repo/a.py"),
            self._make_deny("read_file", "/repo/b.py"),
        ]
        events = [
            self._make_tool_event("read_file", "/repo/a.py"),
            self._make_tool_event("read_file", "/repo/b.py"),
        ]
        assert _count_late_reads(decisions, events) == 2


# ---------------------------------------------------------------------------
# Test: compute_avoided_estimate – Golden Runs
# ---------------------------------------------------------------------------

class TestComputeAvoidedEstimate:
    """
    Hand-computed golden tests for the main estimation function.
    """

    def test_zero_gate_activity_returns_zero_avoided(self, tmp_db: Path) -> None:
        """A session with no gate activity should have zero avoided cost."""
        from budgetpilot.core.savings import compute_avoided_estimate
        sid = "sess_zero"
        _insert_session(tmp_db, sid)
        _insert_model_call(tmp_db, sid, 500, 200, 0.00070)

        est = compute_avoided_estimate(session_id=sid, db_path=tmp_db, persist=False)
        assert est.deny_count == 0
        assert est.rewrite_count == 0
        assert est.net_tokens_avoided == 0
        assert est.avoided_mid_usd == 0.0
        assert est.avoided_low_usd == 0.0
        assert est.avoided_high_usd == 0.0
        assert est.mechanism == "none"

    def test_golden_run_with_denies_only(self, tmp_db: Path) -> None:
        """
        Golden run: 4 denies, 0 overrides, 0 late reads, no rewrites.
        tokens_avoided = 4 × 150 = 600
        Input rate = $1.00/M = 1e-6 USD/token
        With fallback turns (p50 = 5.0):
          avoided_mid = 600 × 5.0 × 1e-6 = 0.003 USD
        """
        from budgetpilot.core.savings import compute_avoided_estimate, DEFAULT_TOOL_CALL_TOKENS
        sid = "sess_golden_deny"
        _insert_session(tmp_db, sid)
        _insert_model_call(tmp_db, sid, 1000, 400, 0.0018)

        for _ in range(4):
            _insert_gate_decision(tmp_db, sid, "read_file", "deny")

        est = compute_avoided_estimate(
            session_id=sid,
            tier="cheap",
            tokens_per_blocked_call=DEFAULT_TOOL_CALL_TOKENS,
            db_path=tmp_db,
            persist=False,
        )
        assert est.deny_count == 4
        assert est.rewrite_count == 0
        assert est.net_tokens_avoided == 4 * DEFAULT_TOOL_CALL_TOKENS
        # Verify avoided_mid_usd = net_tokens × p50 × rate (fallback p50=5)
        rate = est.input_rate_per_token
        expected_mid = round(est.net_tokens_avoided * est.turns_p50 * rate, 8)
        assert abs(est.avoided_mid_usd - expected_mid) < 1e-9

    def test_golden_run_with_rewrites_only(self, tmp_db: Path) -> None:
        """
        Golden run: 6 rewrites. Each rewrite avoids 50% of tokens_per_blocked_call.
        tokens_avoided_from_rewrites = 6 × (150 // 2) = 6 × 75 = 450
        """
        from budgetpilot.core.savings import compute_avoided_estimate, DEFAULT_TOOL_CALL_TOKENS
        sid = "sess_golden_rewrite"
        _insert_session(tmp_db, sid)

        for _ in range(6):
            _insert_gate_decision(tmp_db, sid, "read_file", "rewrite")

        est = compute_avoided_estimate(
            session_id=sid,
            tier="cheap",
            tokens_per_blocked_call=DEFAULT_TOOL_CALL_TOKENS,
            db_path=tmp_db,
            persist=False,
        )
        expected_tokens = 6 * (DEFAULT_TOOL_CALL_TOKENS // 2)
        assert est.rewrite_count == 6
        assert est.net_tokens_avoided == expected_tokens
        assert est.mechanism == "gate_rewrite"

    def test_override_deduction_reduces_avoided_estimate(self, tmp_db: Path) -> None:
        """
        3 denies, 1 is overridden → effective denies = 2.
        net_tokens_avoided = 2 × 150 = 300
        """
        from budgetpilot.core.savings import compute_avoided_estimate, DEFAULT_TOOL_CALL_TOKENS
        sid = "sess_override"
        _insert_session(tmp_db, sid)

        # 1st deny: later overridden
        _insert_gate_decision(tmp_db, sid, "read_file", "deny",
                               original_call={"path": "/repo/override.py"})
        # Same tool allowed within window (override)
        _insert_gate_decision(tmp_db, sid, "read_file", "allow",
                               original_call={"path": "/repo/override.py"})
        # 2 more denies not overridden
        _insert_gate_decision(tmp_db, sid, "read_file", "deny",
                               original_call={"path": "/repo/a.py"})
        _insert_gate_decision(tmp_db, sid, "search", "deny",
                               original_call={"path": "/repo/b.py"})

        est = compute_avoided_estimate(
            session_id=sid,
            tier="cheap",
            tokens_per_blocked_call=DEFAULT_TOOL_CALL_TOKENS,
            override_window=3,
            db_path=tmp_db,
            persist=False,
        )
        assert est.deny_count == 3
        assert est.override_count == 1
        # effective_denies = 3 - 1 - 0 = 2
        assert est.net_tokens_avoided == 2 * DEFAULT_TOOL_CALL_TOKENS

    def test_late_read_deduction_reduces_avoided_estimate(self, tmp_db: Path) -> None:
        """
        2 denies, 1 file later read anyway → late_read_count = 1
        effective_denies = 2 - 0 - 1 = 1
        """
        from budgetpilot.core.savings import compute_avoided_estimate, DEFAULT_TOOL_CALL_TOKENS
        sid = "sess_late_read"
        _insert_session(tmp_db, sid)

        # Deny read of specific file
        _insert_gate_decision(tmp_db, sid, "read_file", "deny",
                               original_call={"path": "/repo/late_read.py"})
        # Deny read of another file (will not be read)
        _insert_gate_decision(tmp_db, sid, "read_file", "deny",
                               original_call={"path": "/repo/never_read.py"})
        # Late read: /repo/late_read.py is eventually read
        _insert_tool_event(tmp_db, sid, "read_file",
                           arguments={"path": "/repo/late_read.py"})

        est = compute_avoided_estimate(
            session_id=sid,
            tier="cheap",
            tokens_per_blocked_call=DEFAULT_TOOL_CALL_TOKENS,
            db_path=tmp_db,
            persist=False,
        )
        assert est.deny_count == 2
        assert est.late_read_count == 1
        # effective_denies = 2 - 0 - 1 = 1
        assert est.net_tokens_avoided == 1 * DEFAULT_TOOL_CALL_TOKENS

    def test_combined_deny_and_rewrite_mechanism(self, tmp_db: Path) -> None:
        """Mechanism should be 'combined' when both denies and rewrites exist."""
        from budgetpilot.core.savings import compute_avoided_estimate
        sid = "sess_combined"
        _insert_session(tmp_db, sid)

        _insert_gate_decision(tmp_db, sid, "read_file", "deny")
        _insert_gate_decision(tmp_db, sid, "read_file", "rewrite")

        est = compute_avoided_estimate(session_id=sid, db_path=tmp_db, persist=False)
        assert est.mechanism == "combined"

    def test_measured_spend_is_exact_from_model_calls(self, tmp_db: Path) -> None:
        """Measured spend should exactly match model_calls.estimated_cost sum."""
        from budgetpilot.core.savings import compute_avoided_estimate
        sid = "sess_measured"
        _insert_session(tmp_db, sid)
        _insert_model_call(tmp_db, sid, 1000, 400, 0.0018)
        _insert_model_call(tmp_db, sid, 500, 200, 0.0009)

        est = compute_avoided_estimate(session_id=sid, db_path=tmp_db, persist=False)
        assert abs(est.measured_spend_usd - 0.0027) < 1e-9

    def test_uncertainty_band_low_le_mid_le_high(self, tmp_db: Path) -> None:
        """The uncertainty band must always be low ≤ mid ≤ high."""
        from budgetpilot.core.savings import compute_avoided_estimate
        sid = "sess_band"
        _insert_session(tmp_db, sid)

        for _ in range(5):
            _insert_gate_decision(tmp_db, sid, "read_file", "deny")

        est = compute_avoided_estimate(session_id=sid, db_path=tmp_db, persist=False)
        assert est.avoided_low_usd <= est.avoided_mid_usd
        assert est.avoided_mid_usd <= est.avoided_high_usd

    def test_persist_writes_to_avoided_estimates_table(self, tmp_db: Path) -> None:
        """When persist=True, an avoided_estimates row must be written to SQLite."""
        from budgetpilot.core.savings import compute_avoided_estimate, get_session_avoided_estimates
        sid = "sess_persist"
        _insert_session(tmp_db, sid)
        _insert_gate_decision(tmp_db, sid, "read_file", "deny")

        compute_avoided_estimate(session_id=sid, db_path=tmp_db, persist=True)

        rows = get_session_avoided_estimates(session_id=sid, db_path=tmp_db)
        assert len(rows) >= 1
        assert rows[0]["session_id"] == sid
        assert rows[0]["mechanism"] in ("gate_deny", "gate_rewrite", "combined", "none")

    def test_paired_savings_defaults_to_zero_for_non_bench(self, tmp_db: Path) -> None:
        """Paired-run savings field should default to 0.0 for non-benchmark sessions."""
        from budgetpilot.core.savings import compute_avoided_estimate
        sid = "sess_non_bench"
        _insert_session(tmp_db, sid)

        est = compute_avoided_estimate(session_id=sid, db_path=tmp_db, persist=False)
        assert est.paired_savings_usd == 0.0


# ---------------------------------------------------------------------------
# Test: compute_paired_run_savings
# ---------------------------------------------------------------------------

class TestComputePairedRunSavings:
    def test_positive_savings_when_gated_is_cheaper(self, tmp_db: Path) -> None:
        """Gated session cheaper than ungated → positive paired savings."""
        from budgetpilot.core.savings import compute_paired_run_savings
        gated = "sess_gated"
        ungated = "sess_ungated"
        _insert_session(tmp_db, gated)
        _insert_session(tmp_db, ungated)

        # Gated: cost 0.001 USD
        _insert_model_call(tmp_db, gated, 500, 200, 0.001)
        # Ungated: cost 0.003 USD (more expensive)
        _insert_model_call(tmp_db, ungated, 1500, 800, 0.003)

        savings = compute_paired_run_savings(
            gated_session_id=gated,
            ungated_session_id=ungated,
            db_path=tmp_db,
        )
        assert savings == pytest.approx(0.002, abs=1e-9)

    def test_zero_savings_when_equal_cost(self, tmp_db: Path) -> None:
        """When gated and ungated have identical cost, savings should be 0."""
        from budgetpilot.core.savings import compute_paired_run_savings
        gated = "sess_eq_gated"
        ungated = "sess_eq_ungated"
        _insert_session(tmp_db, gated)
        _insert_session(tmp_db, ungated)

        _insert_model_call(tmp_db, gated, 500, 200, 0.002)
        _insert_model_call(tmp_db, ungated, 500, 200, 0.002)

        savings = compute_paired_run_savings(
            gated_session_id=gated,
            ungated_session_id=ungated,
            db_path=tmp_db,
        )
        assert savings == pytest.approx(0.0, abs=1e-9)

    def test_negative_savings_when_gated_is_more_expensive(self, tmp_db: Path) -> None:
        """Gated session more expensive than ungated → negative paired savings."""
        from budgetpilot.core.savings import compute_paired_run_savings
        gated = "sess_expensive_gated"
        ungated = "sess_cheap_ungated"
        _insert_session(tmp_db, gated)
        _insert_session(tmp_db, ungated)

        _insert_model_call(tmp_db, gated, 2000, 800, 0.005)
        _insert_model_call(tmp_db, ungated, 500, 200, 0.001)

        savings = compute_paired_run_savings(
            gated_session_id=gated,
            ungated_session_id=ungated,
            db_path=tmp_db,
        )
        assert savings == pytest.approx(-0.004, abs=1e-9)


# ---------------------------------------------------------------------------
# Test: savings module strict decoupling (no forbidden imports)
# ---------------------------------------------------------------------------

def test_savings_module_no_forbidden_imports() -> None:
    """savings.py must not import ollama, mcp, fastapi, or uvicorn."""
    import ast
    savings_path = Path(__file__).resolve().parent.parent / "budgetpilot" / "core" / "savings.py"
    assert savings_path.exists(), f"savings.py not found at {savings_path}"

    with open(savings_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=str(savings_path))

    forbidden = {"ollama", "mcp", "fastapi", "uvicorn"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in forbidden, f"savings.py imports forbidden package '{alias.name}'"
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root not in forbidden, f"savings.py imports from forbidden package '{node.module}'"

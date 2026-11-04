"""
Unit and integration tests for BudgetPilot Recovery Router (Phases G, H, J, K).
Tests cover:
- Utility maximization U(a) = P_success - lambda * C_norm
- HandSetLambda and CrcLambda operating points
- Action ranking for each FailureClass
- SQLite persistence of recovery decisions
- Laplace smoothing prior estimation
"""

from pathlib import Path
import tempfile
import pytest

from budgetpilot.core.router import (
    ACTION_COSTS,
    BASE_PROBABILITIES,
    CrcLambda,
    HandSetLambda,
    RecoveryRouter,
    _laplace_smooth,
    route,
)
from budgetpilot.core.store import get_connection, init_db
from budgetpilot.core.types import (
    FailureClass,
    RecoveryAction,
    RecoveryContext,
    RouterDecision,
)


def test_hand_set_lambda_operating_points():
    """Verify HandSetLambda values for tight, balanced, loose."""
    assert HandSetLambda.get("tight") == 2.5
    assert HandSetLambda.get("balanced") == 0.5
    assert HandSetLambda.get("loose") == 0.1
    # Fallback to balanced
    assert HandSetLambda.get("unknown") == 0.5


def test_crc_lambda_fallback_when_uncalibrated(tmp_path):
    """Verify CrcLambda falls back to hand-set lambda when DB has no calibrations."""
    db_file = tmp_path / "test_bp.db"
    init_db(db_file)

    lam, source = CrcLambda.get("balanced", db_path=db_file)
    assert lam == 0.5
    assert source == "uncalibrated"

    lam_tight, source_tight = CrcLambda.get("tight", db_path=db_file)
    assert lam_tight == 2.5
    assert source_tight == "uncalibrated"


def test_action_costs_and_ordering():
    """Verify ACTION_COSTS normalization in [0, 1] with expected cost ordering."""
    for action, cost in ACTION_COSTS.items():
        assert 0.0 <= cost <= 1.0

    assert ACTION_COSTS[RecoveryAction.REFLECT] < ACTION_COSTS[RecoveryAction.REPLAN]
    assert ACTION_COSTS[RecoveryAction.REPLAN] < ACTION_COSTS[RecoveryAction.EXPAND_SCOPE]
    assert ACTION_COSTS[RecoveryAction.EXPAND_SCOPE] < ACTION_COSTS[RecoveryAction.ESCALATE]


def test_laplace_smoothing_properties():
    """Verify Laplace smoothing behavior with 0 and increasing counts."""
    # Zero observations with 4 classes should give 1/(0 + 1*4) = 0.25 (uniform)
    p_zero = _laplace_smooth(successes=0, total=0, alpha=1.0, num_classes=4)
    assert p_zero == 0.25

    # 10 successes out of 10 trials
    p_perfect = _laplace_smooth(successes=10, total=10, alpha=1.0, num_classes=4)
    # (10 + 1) / (10 + 4) = 11/14 ≈ 0.7857
    assert round(p_perfect, 4) == 0.7857


def test_route_syntax_error_selects_reflect(tmp_path):
    """Verify that a local syntax or assertion error routes to REFLECT under balanced budget."""
    db_file = tmp_path / "test_bp.db"
    init_db(db_file)

    ctx = RecoveryContext(
        session_id="task_syntax_1",
        failure_class=FailureClass.SYNTAX_OR_LINT,
        attempt_number=1,
    )

    decision = route(ctx, budget="balanced", db_path=db_file)
    assert decision.action == RecoveryAction.REFLECT
    assert decision.target_tier == "cheap"
    assert decision.cost_penalty_lambda == 0.5


def test_route_symbol_outside_touched_selects_expand_scope(tmp_path):
    """Verify that an out-of-scope symbol failure routes to EXPAND_SCOPE."""
    db_file = tmp_path / "test_bp.db"
    init_db(db_file)

    ctx = RecoveryContext(
        session_id="task_scope_1",
        failure_class=FailureClass.SYMBOL_OUTSIDE_TOUCHED,
        attempt_number=1,
    )

    decision = route(ctx, budget="balanced", db_path=db_file)
    assert decision.action == RecoveryAction.EXPAND_SCOPE


def test_route_capability_gap_selects_escalate(tmp_path):
    """Verify that a genuine capability gap routes to ESCALATE."""
    db_file = tmp_path / "test_bp.db"
    init_db(db_file)

    ctx = RecoveryContext(
        session_id="task_gap_1",
        failure_class=FailureClass.CAPABILITY_GAP,
        attempt_number=3,
    )

    decision = route(ctx, budget="loose", db_path=db_file)
    assert decision.action == RecoveryAction.ESCALATE
    assert decision.target_tier == "strong"


def test_recovery_decision_sqlite_persistence(tmp_path):
    """Verify that router decisions are persisted to the recovery_decisions SQLite table."""
    db_file = tmp_path / "test_bp.db"
    init_db(db_file)

    ctx = RecoveryContext(
        session_id="task_persist_1",
        failure_class=FailureClass.ASSERTION_LOCAL,
        attempt_number=1,
    )

    decision = route(ctx, budget="balanced", db_path=db_file)

    with get_connection(db_file) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT session_id, failure_class, action FROM recovery_decisions WHERE session_id = ?", ("task_persist_1",))
        row = cursor.fetchone()

    assert row is not None
    assert row[0] == "task_persist_1"
    assert row[1] == FailureClass.ASSERTION_LOCAL.value
    assert row[2] == decision.action.value


def test_recovery_router_class_decide_interface(tmp_path):
    """Verify RecoveryRouter class decide method forwards to route function."""
    db_file = tmp_path / "test_bp.db"
    init_db(db_file)

    router = RecoveryRouter()
    ctx = RecoveryContext(
        session_id="class_interface_1",
        failure_class=FailureClass.SYNTAX_OR_LINT,
        attempt_number=1,
    )

    decision = router.decide(ctx, budget="balanced", db_path=db_file)
    assert isinstance(decision, RouterDecision)
    assert decision.action == RecoveryAction.REFLECT

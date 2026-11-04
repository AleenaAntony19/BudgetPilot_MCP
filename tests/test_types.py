"""
Unit tests for BudgetPilot core type contracts and serialization (Phase A).
"""

import ast
import json
from pathlib import Path
import pytest

from budgetpilot.core.types import (
    BpEvent,
    FailureClass,
    GateVerdict,
    GateVerdictStatus,
    RecoveryAction,
    RecoveryContext,
    RouterDecision,
    ScopePrediction,
    ToolCall,
)


def test_recovery_action_enum():
    """Verify recovery action enum members match PRD four-action recovery set."""
    actions = {a.value for a in RecoveryAction}
    assert actions == {"reflect", "replan", "expand_scope", "escalate"}
    assert RecoveryAction.EXPAND_SCOPE == "expand_scope"
    assert RecoveryAction("reflect") == RecoveryAction.REFLECT


def test_failure_class_enum():
    """Verify failure class taxonomy."""
    assert FailureClass.SYNTAX_ERROR == "syntax_error"
    assert FailureClass.TEST_FAILURE == "test_failure"
    assert FailureClass.TIMEOUT == "timeout"
    assert FailureClass.IMPORT_ERROR == "import_error"
    assert FailureClass("runtime_error") == FailureClass.RUNTIME_ERROR


def test_tool_call_serialization():
    """Verify ToolCall serialization and deserialization."""
    tc = ToolCall(
        tool_name="read_file",
        arguments={"path": "src/main.py", "start_line": 10},
        call_id="call_123",
        timestamp=1700000000.0,
    )
    d = tc.to_dict()
    assert d["tool_name"] == "read_file"
    assert d["arguments"]["start_line"] == 10
    assert d["call_id"] == "call_123"

    # From dict
    restored = ToolCall.from_dict(d)
    assert restored.tool_name == tc.tool_name
    assert restored.arguments == tc.arguments
    assert restored.call_id == tc.call_id
    assert restored.timestamp == tc.timestamp

    # From JSON
    json_str = tc.to_json()
    restored_json = ToolCall.from_json(json_str)
    assert restored_json.tool_name == tc.tool_name
    assert restored_json.arguments == tc.arguments


def test_gate_verdict_allow_deny_rewrite():
    """Verify GateVerdict constructors, comparisons, and serialization."""
    # Allow
    v_allow = GateVerdict.allow(reason="Allowed simple read")
    assert v_allow.status == GateVerdictStatus.ALLOW
    assert v_allow == GateVerdict.ALLOW
    assert v_allow == "allow"
    assert v_allow.rewritten_call is None

    # Deny
    v_deny = GateVerdict.deny(reason="Excessive file exploration")
    assert v_deny.status == GateVerdictStatus.DENY
    assert v_deny == GateVerdict.DENY

    # Rewrite
    orig_tc = ToolCall(tool_name="read_file", arguments={"path": "huge.log"})
    rewritten_tc = ToolCall(tool_name="read_file", arguments={"path": "huge.log", "max_bytes": 1000})
    v_rewrite = GateVerdict.rewrite(
        rewritten_call=rewritten_tc,
        reason="Truncated read to minimum viable slice",
    )
    assert v_rewrite.status == GateVerdictStatus.REWRITE
    assert v_rewrite.rewritten_call is not None
    assert v_rewrite.rewritten_call.arguments["max_bytes"] == 1000

    # Serialization roundtrip with rewritten ToolCall
    json_str = v_rewrite.to_json()
    restored = GateVerdict.from_json(json_str)
    assert restored.status == GateVerdictStatus.REWRITE
    assert restored.rewritten_call is not None
    assert restored.rewritten_call.arguments["max_bytes"] == 1000
    assert restored.reason == v_rewrite.reason


def test_scope_prediction_serialization():
    """Verify ScopePrediction dataclass and serialization."""
    sp = ScopePrediction(
        scope=2,
        confidence=0.85,
        budget_tokens=6000,
        max_read_files=5,
        rationale="Multi-file dependency edit detected",
        metadata={"analyzer": "lexical"},
    )
    d = sp.to_dict()
    assert d["scope"] == 2
    assert d["confidence"] == 0.85
    assert d["budget_tokens"] == 6000

    restored = ScopePrediction.from_dict(d)
    assert restored.scope == 2
    assert restored.confidence == 0.85
    assert restored.rationale == "Multi-file dependency edit detected"
    assert restored.metadata["analyzer"] == "lexical"

    json_str = sp.to_json()
    restored_json = ScopePrediction.from_json(json_str)
    assert restored_json.scope == sp.scope


def test_recovery_context_serialization():
    """Verify RecoveryContext handles enums and lists cleanly in serialization."""
    ctx = RecoveryContext(
        failure_class=FailureClass.TEST_FAILURE,
        error_message="AssertionError: 2 != 3",
        test_command="pytest tests/test_calc.py",
        stderr="FAILED tests/test_calc.py::test_add",
        attempt_number=2,
        files_touched=["src/calc.py"],
        previous_actions=[RecoveryAction.REFLECT, RecoveryAction.EXPAND_SCOPE],
        tokens_spent=1500,
        cost_spent_usd=0.0035,
        session_id="sess_abc123",
        metadata={"flaky": False},
    )

    d = ctx.to_dict()
    assert d["failure_class"] == "test_failure"
    assert d["previous_actions"] == ["reflect", "expand_scope"]
    assert d["attempt_number"] == 2
    assert d["cost_spent_usd"] == 0.0035

    # Restoring should correctly cast enums
    restored = RecoveryContext.from_dict(d)
    assert restored.failure_class == FailureClass.TEST_FAILURE
    assert restored.previous_actions == [
        RecoveryAction.REFLECT,
        RecoveryAction.EXPAND_SCOPE,
    ]
    assert restored.session_id == "sess_abc123"

    json_str = ctx.to_json()
    restored_json = RecoveryContext.from_json(json_str)
    assert restored_json.previous_actions[1] == RecoveryAction.EXPAND_SCOPE


def test_router_decision_serialization():
    """Verify RouterDecision dataclass and serialization."""
    decision = RouterDecision(
        action=RecoveryAction.EXPAND_SCOPE,
        target_tier="cheap",
        cost_penalty_lambda=0.45,
        confidence=0.92,
        rationale="Error traces point to indirect caller in core",
        metadata={"rule": "scope_expansion_rule_1"},
    )
    d = decision.to_dict()
    assert d["action"] == "expand_scope"
    assert d["cost_penalty_lambda"] == 0.45

    restored = RouterDecision.from_dict(d)
    assert restored.action == RecoveryAction.EXPAND_SCOPE
    assert restored.cost_penalty_lambda == 0.45
    assert restored.target_tier == "cheap"

    json_str = decision.to_json()
    restored_json = RouterDecision.from_json(json_str)
    assert restored_json.action == RecoveryAction.EXPAND_SCOPE


def test_bp_event_serialization():
    """Verify BpEvent telemetry and audit logging data container."""
    event = BpEvent(
        event_type="execute_gate_intercept",
        session_id="session_456",
        payload={"tool": "read_file", "verdict": "allow"},
        cost_usd=0.00015,
        latency_seconds=0.042,
    )
    d = event.to_dict()
    assert d["event_type"] == "execute_gate_intercept"
    assert d["cost_usd"] == 0.00015
    assert "event_id" in d

    restored = BpEvent.from_dict(d)
    assert restored.event_id == event.event_id
    assert restored.session_id == "session_456"
    assert restored.latency_seconds == 0.042

    json_str = event.to_json()
    restored_json = BpEvent.from_json(json_str)
    assert restored_json.event_id == event.event_id


def test_strict_rule_no_forbidden_imports_in_core():
    """
    STRICT RULE: budgetpilot/core/ must NOT import ollama, mcp, or fastapi.
    Inspects AST of every .py file in budgetpilot/core/ to statically guarantee compliance.
    """
    core_dir = Path(__file__).resolve().parent.parent / "budgetpilot" / "core"
    assert core_dir.exists(), f"Core directory {core_dir} not found"

    forbidden_packages = {"ollama", "mcp", "fastapi", "uvicorn"}

    for py_file in core_dir.glob("*.py"):
        with open(py_file, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(py_file))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_pkg = alias.name.split(".")[0]
                    assert root_pkg not in forbidden_packages, (
                        f"Strict rule violated: {py_file.name} imports '{alias.name}'"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root_pkg = node.module.split(".")[0]
                    assert root_pkg not in forbidden_packages, (
                        f"Strict rule violated: {py_file.name} imports from '{node.module}'"
                    )


def test_stub_modules_raise_not_implemented():
    """Verify that stub modules raise NotImplementedError when invoked."""
    from budgetpilot.core.gate import ExecuteGate
    from budgetpilot.core.estimate import ScopeEstimator
    from budgetpilot.core.verify import VerificationHook
    from budgetpilot.core.router import RecoveryRouter
    from budgetpilot.core.ledger import CostLedger
    from budgetpilot.core.calibrate import CRCCalibrator
    from budgetpilot.mcp.server import run_server
    from budgetpilot.mcp.tools import intercept_tool
    from budgetpilot.recovery.actions import execute_recovery_action

    with pytest.raises(NotImplementedError):
        ExecuteGate().evaluate(ToolCall(tool_name="test"), ScopePrediction(scope=1))

    with pytest.raises(NotImplementedError):
        ScopeEstimator().predict("test")

    with pytest.raises(NotImplementedError):
        VerificationHook().verify("pytest")

    with pytest.raises(NotImplementedError):
        RecoveryRouter().route(
            RecoveryContext(failure_class=FailureClass.SYNTAX_ERROR, error_message="syntax error")
        )

    with pytest.raises(NotImplementedError):
        CostLedger().record_event(BpEvent())

    with pytest.raises(NotImplementedError):
        CRCCalibrator().calibrate("")

    with pytest.raises(NotImplementedError):
        run_server()

    with pytest.raises(NotImplementedError):
        intercept_tool("read_file", {})

    with pytest.raises(NotImplementedError):
        execute_recovery_action(
            RecoveryAction.REFLECT,
            RecoveryContext(failure_class=FailureClass.UNKNOWN, error_message=""),
        )


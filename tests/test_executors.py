"""
Unit tests for BudgetPilot Recovery Action Directives & Executors (Phase G & H).
Tests cover:
- execute_reflect: local repair directive generation
- execute_replan: 3-line replan directive and snapshot restoration
- execute_expand_scope: dependency frontier generation and scope bump (+1)
- execute_recovery_directive: unified action dispatcher
- DirectiveString: behavior as str with structured metadata inspection
"""

from pathlib import Path
import tempfile
import pytest

from budgetpilot.core.executors import (
    DirectiveString,
    execute_expand_scope,
    execute_recovery_directive,
    execute_reflect,
    execute_replan,
)
from budgetpilot.core.frontier import DependencyFrontier
from budgetpilot.core.tracker import TaskState
from budgetpilot.core.types import (
    FailureClass,
    RecoveryAction,
    RecoveryContext,
)


def test_directive_string_subclass():
    """Verify DirectiveString acts as standard string while exposing metadata attributes."""
    ds = DirectiveString(
        directive="Sample directive text",
        action=RecoveryAction.REFLECT,
        metadata={"key": "val"},
        files_restored=["file1.py"],
    )

    # Behaves as string
    assert "Sample directive text" in ds
    assert len(ds) == len("Sample directive text")
    assert ds.startswith("Sample")

    # Has metadata attributes
    assert ds.action == RecoveryAction.REFLECT
    assert ds.metadata["key"] == "val"
    assert ds.files_restored == ["file1.py"]


def test_execute_reflect():
    """Verify execute_reflect produces local repair instructions matching the failure class."""
    ctx = RecoveryContext(
        session_id="task_refl_1",
        failure_class=FailureClass.SYNTAX_OR_LINT,
        files_touched=["budgetpilot/cli.py"],
        stderr="SyntaxError: invalid syntax",
    )

    result = execute_reflect(ctx, test_output="FAILED test_cli.py")
    assert isinstance(result, DirectiveString)
    assert result.action == RecoveryAction.REFLECT
    assert "REFLECT (Local Repair)" in result
    assert "budgetpilot/cli.py" in result
    assert "SyntaxError: invalid syntax" in str(result) or "FAILED test_cli.py" in str(result)


def test_execute_replan(tmp_path):
    """Verify execute_replan emits the exact 3-line replan directive structure."""
    ctx = RecoveryContext(
        session_id="task_rep_1",
        failure_class=FailureClass.REGRESSION_ELSEWHERE,
        files_touched=["dummy_module.py"],
    )

    result = execute_replan(ctx, repo_root=str(tmp_path))
    assert isinstance(result, DirectiveString)
    assert result.action == RecoveryAction.REPLAN
    assert "[REPLAN 1/3]" in result
    assert "[REPLAN 2/3]" in result
    assert "[REPLAN 3/3]" in result
    assert "regression_elsewhere" in result


def test_execute_expand_scope():
    """Verify execute_expand_scope bumps the scope and builds frontier."""
    state = TaskState(session_id="expand_sess_1")
    state.metadata["scope"] = 1

    ctx = RecoveryContext(
        session_id="task_exp_1",
        failure_class=FailureClass.SYMBOL_OUTSIDE_TOUCHED,
        files_touched=["budgetpilot/core/gate.py"],
        stderr="ImportError: cannot import name 'foo' from 'budgetpilot.core.types'",
    )

    frontier = execute_expand_scope(ctx, repo_root=".", current_scope=1, state=state)
    assert isinstance(frontier, DependencyFrontier)
    assert frontier.new_scope == 2
    assert state.metadata.get("scope") == 2
    assert "EXPAND SCOPE" in frontier.directive


def test_execute_recovery_directive_dispatcher():
    """Verify execute_recovery_directive dispatches to each action correctly."""
    ctx = RecoveryContext(
        session_id="task_disp_1",
        failure_class=FailureClass.ASSERTION_LOCAL,
    )

    # Reflect
    refl = execute_recovery_directive(RecoveryAction.REFLECT, ctx)
    assert isinstance(refl, DirectiveString)
    assert refl.action == RecoveryAction.REFLECT

    # Replan
    rep = execute_recovery_directive(RecoveryAction.REPLAN, ctx)
    assert isinstance(rep, DirectiveString)
    assert rep.action == RecoveryAction.REPLAN

    # Expand scope
    exp = execute_recovery_directive(RecoveryAction.EXPAND_SCOPE, ctx, current_scope=1)
    assert isinstance(exp, DependencyFrontier)
    assert exp.new_scope == 2

    # Escalate
    esc = execute_recovery_directive(RecoveryAction.ESCALATE, ctx)
    assert isinstance(esc, DirectiveString)
    assert esc.action == RecoveryAction.ESCALATE
    assert "ESCALATE" in esc

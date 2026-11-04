"""
BudgetPilot Recovery Action Executors.
Implementation Phase: Phase C (Week 3).

Executes concrete recovery actions:
- reflect: local repair using execution feedback
- replan: fresh attempt, same scope
- expand_scope: widen the scope estimate and retry
- escalate: hand off to a stronger model
"""

from typing import Any, Dict
from budgetpilot.core.types import RecoveryAction, RecoveryContext


def execute_recovery_action(action: RecoveryAction, context: RecoveryContext) -> Dict[str, Any]:
    """
    Execute the assigned recovery action against the agent runtime.

    Raises:
        NotImplementedError: Scheduled for implementation in Phase C / Week 3.
    """
    raise NotImplementedError(
        f"Recovery action '{action}' executor is scheduled for Phase C implementation."
    )

"""
BudgetPilot Calibration Package.
Phase J: Failure state generators and rollout collection for empirical prior estimation.

Strict Rule: This package must NOT import ollama, mcp, fastapi, or uvicorn.
"""
from budgetpilot.core.calibration.failures import (
    FailureState,
    FailureKind,
    generate_under_scoped_failure,
    generate_repairable_failure,
    generate_capability_gap_failure,
    load_failure_states,
    save_failure_states,
)
from budgetpilot.core.calibration.rollouts import (
    RolloutRecord,
    collect_rollouts,
    load_rollout_counts,
)

__all__ = [
    "FailureState",
    "FailureKind",
    "generate_under_scoped_failure",
    "generate_repairable_failure",
    "generate_capability_gap_failure",
    "load_failure_states",
    "save_failure_states",
    "RolloutRecord",
    "collect_rollouts",
    "load_rollout_counts",
]

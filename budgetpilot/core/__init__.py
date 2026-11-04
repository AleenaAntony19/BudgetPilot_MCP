"""
BudgetPilot Core Engine.
Phase A: Foundation and type contracts.

Strict Rule: Modules in budgetpilot.core must NOT import ollama, mcp, or fastapi.
"""

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
    normalize_tool_call,
)
from budgetpilot.core.store import (
    CORE_TABLES,
    init_db,
    get_connection,
    safe_execute_write,
    migrate_legacy_interactions,
)
from budgetpilot.core.tracker import (
    TaskState,
    TaskStatus,
    TaskTracker,
    create_git_snapshot,
)
from budgetpilot.core.executors import (
    DirectiveString,
    execute_expand_scope,
    execute_recovery_directive,
    execute_reflect,
    execute_replan,
    restore_snapshot_files,
)
from budgetpilot.core.frontier import (
    DependencyFrontier,
    apply_frontier_to_state,
    build_dependency_frontier,
)
from budgetpilot.core.gate import check as gate_check
from budgetpilot.core.router import (
    ACTION_COSTS,
    BASE_PROBABILITIES,
    HandSetLambda,
    compute_adjusted_probabilities,
    route,
)
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

__all__ = [
    "BpEvent",
    "FailureClass",
    "GateVerdict",
    "GateVerdictStatus",
    "RecoveryAction",
    "RecoveryContext",
    "RouterDecision",
    "ScopePrediction",
    "ToolCall",
    "normalize_tool_call",
    "CORE_TABLES",
    "init_db",
    "get_connection",
    "safe_execute_write",
    "migrate_legacy_interactions",
    "TaskState",
    "TaskStatus",
    "TaskTracker",
    "create_git_snapshot",
    "gate_check",
    "route",
    "HandSetLambda",
    "ACTION_COSTS",
    "BASE_PROBABILITIES",
    "compute_adjusted_probabilities",
    "build_dependency_frontier",
    "apply_frontier_to_state",
    "DependencyFrontier",
    "execute_reflect",
    "execute_replan",
    "execute_expand_scope",
    "execute_recovery_directive",
    "restore_snapshot_files",
    "DirectiveString",
    "is_kill_switch_active",
    "set_kill_switch",
    "enforce_kill_switch",
    "redact_secrets",
    "redact_data",
    "is_secret_present",
    "TaskCapGuard",
    "CapViolationError",
    "CapCheckResult",
    "assert_network_safety",
    "is_allowed_network_target",
    "NetworkAuditContext",
]



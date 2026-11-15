"""
BudgetPilot core engine.

Strict rule: modules in budgetpilot.core must NOT import ollama, mcp, or fastapi.

Names are resolved lazily (PEP 562). Importing one submodule, for example
`budgetpilot.core.state` from the hook process, must not load the whole engine:
hook start-up time is paid on every tool call.
"""

from importlib import import_module

_EXPORTS = {
    "BpEvent": ("budgetpilot.core.types", "BpEvent"),
    "FailureClass": ("budgetpilot.core.types", "FailureClass"),
    "GateVerdict": ("budgetpilot.core.types", "GateVerdict"),
    "GateVerdictStatus": ("budgetpilot.core.types", "GateVerdictStatus"),
    "RecoveryAction": ("budgetpilot.core.types", "RecoveryAction"),
    "RecoveryContext": ("budgetpilot.core.types", "RecoveryContext"),
    "RouterDecision": ("budgetpilot.core.types", "RouterDecision"),
    "ScopePrediction": ("budgetpilot.core.types", "ScopePrediction"),
    "ToolCall": ("budgetpilot.core.types", "ToolCall"),
    "normalize_tool_call": ("budgetpilot.core.types", "normalize_tool_call"),
    "CORE_TABLES": ("budgetpilot.core.store", "CORE_TABLES"),
    "init_db": ("budgetpilot.core.store", "init_db"),
    "get_connection": ("budgetpilot.core.store", "get_connection"),
    "safe_execute_write": ("budgetpilot.core.store", "safe_execute_write"),
    "migrate_legacy_interactions": ("budgetpilot.core.store", "migrate_legacy_interactions"),
    "TaskState": ("budgetpilot.core.tracker", "TaskState"),
    "TaskStatus": ("budgetpilot.core.tracker", "TaskStatus"),
    "TaskTracker": ("budgetpilot.core.tracker", "TaskTracker"),
    "create_git_snapshot": ("budgetpilot.core.tracker", "create_git_snapshot"),
    "DirectiveString": ("budgetpilot.core.executors", "DirectiveString"),
    "execute_expand_scope": ("budgetpilot.core.executors", "execute_expand_scope"),
    "execute_recovery_directive": ("budgetpilot.core.executors", "execute_recovery_directive"),
    "execute_reflect": ("budgetpilot.core.executors", "execute_reflect"),
    "execute_replan": ("budgetpilot.core.executors", "execute_replan"),
    "restore_snapshot_files": ("budgetpilot.core.executors", "restore_snapshot_files"),
    "DependencyFrontier": ("budgetpilot.core.frontier", "DependencyFrontier"),
    "apply_frontier_to_state": ("budgetpilot.core.frontier", "apply_frontier_to_state"),
    "build_dependency_frontier": ("budgetpilot.core.frontier", "build_dependency_frontier"),
    "gate_check": ("budgetpilot.core.gate", "check"),
    "ACTION_COSTS": ("budgetpilot.core.router", "ACTION_COSTS"),
    "BASE_PROBABILITIES": ("budgetpilot.core.router", "BASE_PROBABILITIES"),
    "HandSetLambda": ("budgetpilot.core.router", "HandSetLambda"),
    "compute_adjusted_probabilities": ("budgetpilot.core.router", "compute_adjusted_probabilities"),
    "route": ("budgetpilot.core.router", "route"),
    "CapCheckResult": ("budgetpilot.core.safety", "CapCheckResult"),
    "CapViolationError": ("budgetpilot.core.safety", "CapViolationError"),
    "NetworkAuditContext": ("budgetpilot.core.safety", "NetworkAuditContext"),
    "TaskCapGuard": ("budgetpilot.core.safety", "TaskCapGuard"),
    "assert_network_safety": ("budgetpilot.core.safety", "assert_network_safety"),
    "enforce_kill_switch": ("budgetpilot.core.safety", "enforce_kill_switch"),
    "is_allowed_network_target": ("budgetpilot.core.safety", "is_allowed_network_target"),
    "is_kill_switch_active": ("budgetpilot.core.safety", "is_kill_switch_active"),
    "is_secret_present": ("budgetpilot.core.safety", "is_secret_present"),
    "redact_data": ("budgetpilot.core.safety", "redact_data"),
    "redact_secrets": ("budgetpilot.core.safety", "redact_secrets"),
    "set_kill_switch": ("budgetpilot.core.safety", "set_kill_switch"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'budgetpilot.core' has no attribute '{name}'")
    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list:
    return sorted(list(globals()) + list(_EXPORTS))

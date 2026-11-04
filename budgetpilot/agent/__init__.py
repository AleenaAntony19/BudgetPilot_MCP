"""
BudgetPilot Agent and Model Integration Subsystem.
Phase A: LLM invocation wrapper and runtime client integration.
"""

from budgetpilot.agent.escalation import EscalationResult, run_escalation
from budgetpilot.agent.loop import AgentLoopResult, run_agent_loop
from budgetpilot.agent.model import ModelResult, chat
from budgetpilot.agent.tools import (
    TOOL_MAP,
    TOOL_SCHEMAS,
    edit_file,
    execute_tool,
    get_repo_root,
    list_dir,
    read_file,
    resolve_safe_path,
    run_tests,
    search,
    set_repo_root,
)

__all__ = [
    "AgentLoopResult",
    "run_agent_loop",
    "ModelResult",
    "chat",
    "EscalationResult",
    "run_escalation",
    "list_dir",
    "search",
    "read_file",
    "edit_file",
    "run_tests",
    "TOOL_MAP",
    "TOOL_SCHEMAS",
    "execute_tool",
    "resolve_safe_path",
    "set_repo_root",
    "get_repo_root",
]



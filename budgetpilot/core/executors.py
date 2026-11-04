"""
BudgetPilot Recovery Action Directives & Executors.
Phase G: Implements recovery directives and actions:
- reflect: Append local repair directive to test output.
- replan: Restore touched files from task git snapshot, send 3-line replan directive.
- expand_scope: Build dependency frontier, bump scope level +1, expand allowed file budget.

Strict Rule: This module must NOT import ollama, mcp, fastapi, or uvicorn.
"""

from dataclasses import dataclass, field
import logging
from pathlib import Path
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from budgetpilot.core.frontier import (
    DependencyFrontier,
    apply_frontier_to_state,
    build_dependency_frontier,
)
from budgetpilot.core.types import (
    FailureClass,
    RecoveryAction,
    RecoveryContext,
)

logger = logging.getLogger(__name__)


class DirectiveString(str):
    """
    A string subclass that holds action directive text along with structured execution metadata.
    Enables both standard string assertions (e.g. `assert "REFLECT" in result`) and object inspection
    (e.g. `result.action`, `result.files_restored`, `result.frontier`).
    """
    directive: str
    action: RecoveryAction
    metadata: Dict[str, Any]
    files_restored: List[str]
    frontier: Optional[DependencyFrontier]

    def __new__(
        cls,
        directive: str,
        action: RecoveryAction,
        metadata: Optional[Dict[str, Any]] = None,
        files_restored: Optional[List[str]] = None,
        frontier: Optional[DependencyFrontier] = None,
    ):
        obj = super().__new__(cls, directive)
        obj.directive = directive
        obj.action = action
        obj.metadata = metadata or {}
        obj.files_restored = files_restored or []
        obj.frontier = frontier
        return obj


def execute_reflect(
    ctx: RecoveryContext,
    test_output: Optional[str] = None,
) -> DirectiveString:
    """
    Action Directive: reflect
    Appends a local repair directive to the test failure output, instructing the agent
    to localize its repair strictly to the files already modified without expanding scope.
    """
    base = test_output if test_output is not None else (ctx.stderr or ctx.error_message or "Test execution failed.")
    touched_display = ", ".join(ctx.files_touched) if ctx.files_touched else "currently modified files"

    repair_directive = (
        "[BUDGETPILOT RECOVERY DIRECTIVE: REFLECT (Local Repair)]\n"
        f"Failure Class: {ctx.failure_class.value}\n"
        f"Target Scope: Confine changes strictly to touched files ({touched_display}).\n"
        "Instructions:\n"
        "1. Inspect the test failure output above carefully.\n"
        "2. Formulate a minimal, targeted repair strictly within the already touched files.\n"
        "3. Do NOT modify external modules or alter unrelated interfaces.\n"
        "4. Fix the localized defect while preserving all existing test contracts."
    )

    combined = f"{base.rstrip()}\n\n{repair_directive}"
    return DirectiveString(
        directive=combined,
        action=RecoveryAction.REFLECT,
        metadata={
            "failure_class": ctx.failure_class.value,
            "files_touched": list(ctx.files_touched),
            "repair_directive": repair_directive,
        },
    )


def restore_snapshot_files(
    files: Sequence[str],
    repo_root: Union[str, Path] = ".",
    snapshot_ref: Optional[str] = None,
) -> List[str]:
    """
    Restores the specified files to their state in the given git snapshot reference.
    Handles modified files via checkout and newly created untracked files via removal.
    """
    root = Path(repo_root).expanduser().resolve()
    if not (root / ".git").exists():
        logger.debug("Cannot restore files: %s is not a git repository", root)
        return []

    git_bin = shutil.which("git")
    if not git_bin:
        return []

    target_ref = snapshot_ref or "HEAD"
    restored: List[str] = []

    for file_path in files:
        if not file_path:
            continue
        p = Path(file_path)
        norm_path = str(p.relative_to(root)) if p.is_absolute() and p.is_relative_to(root) else str(p)

        # 1. Try git checkout <target_ref> -- <norm_path>
        proc = subprocess.run(
            [git_bin, "checkout", target_ref, "--", norm_path],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            restored.append(norm_path)
        else:
            # 2. If checkout failed, file might be an untracked new file created during the run
            status_proc = subprocess.run(
                [git_bin, "status", "--porcelain", norm_path],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
            )
            target_abs = root / norm_path
            if target_abs.exists() and (status_proc.stdout.startswith("??") or not status_proc.stdout.strip()):
                try:
                    if target_abs.is_file():
                        target_abs.unlink()
                        restored.append(norm_path)
                except Exception as del_exc:
                    logger.debug("Failed to remove untracked file %s: %s", target_abs, del_exc)

    return restored


def execute_replan(
    ctx: RecoveryContext,
    repo_root: Union[str, Path] = ".",
    snapshot_ref: Optional[str] = None,
) -> DirectiveString:
    """
    Action Directive: replan
    Restores touched files from the task git snapshot and emits an exact 3-line replan directive:
    Line 1: Restoration of touched files and discarding flawed implementation.
    Line 2: Root-cause re-evaluation discarding invalid assumptions.
    Line 3: 3-step minimal plan formulation before executing new code edits.
    """
    target_ref = (
        snapshot_ref
        or ctx.metadata.get("snapshot_ref")
        or ctx.metadata.get("initial_head")
        or "HEAD"
    )

    restored_files = restore_snapshot_files(
        files=ctx.files_touched,
        repo_root=repo_root,
        snapshot_ref=target_ref,
    )

    count_touched = len(ctx.files_touched)
    count_restored = len(restored_files) if restored_files else count_touched

    line1 = f"[REPLAN 1/3] Restored {count_restored} touched files to snapshot ({target_ref}); previous attempt discarded."
    line2 = f"[REPLAN 2/3] Re-evaluate failure '{ctx.failure_class.value}' and discard assumptions that caused the regression."
    line3 = "[REPLAN 3/3] Formulate a fresh 3-step minimal plan before modifying any repository files."

    three_line_directive = f"{line1}\n{line2}\n{line3}"

    return DirectiveString(
        directive=three_line_directive,
        action=RecoveryAction.REPLAN,
        files_restored=restored_files,
        metadata={
            "snapshot_ref": target_ref,
            "lines": [line1, line2, line3],
            "restored_count": len(restored_files),
        },
    )


def execute_expand_scope(
    ctx: RecoveryContext,
    repo_root: Union[str, Path] = ".",
    current_scope: int = 1,
    state: Optional[Any] = None,
) -> DependencyFrontier:
    """
    Action Directive: expand_scope
    Builds the dependency frontier (traceback frames outside touched files, symbol definition/reference
    files via ripgrep, and related tests), bumps scope level (+1), and expands the allowed file budget.
    Updates TaskState if provided.
    """
    frontier = build_dependency_frontier(
        ctx=ctx,
        repo_root=repo_root,
        current_scope=current_scope,
    )

    if state is not None:
        apply_frontier_to_state(frontier, state)

    return frontier


def execute_recovery_directive(
    action: Union[RecoveryAction, str],
    ctx: RecoveryContext,
    repo_root: Union[str, Path] = ".",
    current_scope: int = 1,
    state: Optional[Any] = None,
    test_output: Optional[str] = None,
    snapshot_ref: Optional[str] = None,
) -> Union[DirectiveString, DependencyFrontier]:
    """
    Unified dispatcher for recovery directives across all actions:
    reflect, replan, expand_scope, escalate.
    """
    act_enum = action if isinstance(action, RecoveryAction) else RecoveryAction(str(action).lower())

    if act_enum == RecoveryAction.REFLECT:
        return execute_reflect(ctx, test_output=test_output)
    elif act_enum == RecoveryAction.REPLAN:
        return execute_replan(ctx, repo_root=repo_root, snapshot_ref=snapshot_ref)
    elif act_enum == RecoveryAction.EXPAND_SCOPE:
        return execute_expand_scope(ctx, repo_root=repo_root, current_scope=current_scope, state=state)
    elif act_enum == RecoveryAction.ESCALATE:
        directive = (
            "[BUDGETPILOT RECOVERY DIRECTIVE: ESCALATE]\n"
            "Challenging failure mode detected. Initiating bounded escalation sub-run on 'strong' tier "
            "within an isolated git worktree."
        )
        return DirectiveString(
            directive=directive,
            action=RecoveryAction.ESCALATE,
            metadata={"target_tier": "strong"},
        )
    else:
        raise ValueError(f"Unknown recovery action: {action}")

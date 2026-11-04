"""
BudgetPilot Strong Tier Worktree Escalation Subsystem.
Phase H: Launches a bounded recovery sub-run on the 'strong' model tier inside an
isolated git worktree, enforcing USD and turn caps, and returning a unified diff patch.
"""

from dataclasses import asdict, dataclass, field
import json
import logging
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Union
import uuid

from budgetpilot.agent.model import ModelResult, chat
from budgetpilot.agent.tools import AGENT_TOOL_SCHEMAS, execute_tool
from budgetpilot.config import get_bp_home, load_config
from budgetpilot.core.ledger import CostLedger, record_model_call, record_tool_event
from budgetpilot.core.store import init_db, log_fallback_error, safe_execute_write
from budgetpilot.core.types import RecoveryAction, RecoveryContext

logger = logging.getLogger(__name__)


@dataclass
class EscalationResult:
    """
    Standardized result emitted by an escalation sub-run in an isolated worktree.
    """
    succeeded: bool = False
    patch: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    total_tokens: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    tier: str = "strong"
    status: str = "completed"  # "completed", "max_turns_reached", "max_usd_exceeded", "error"
    error: Optional[str] = None
    worktree_path: Optional[str] = None
    worktree_cleaned: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Forward and backward compatibility aliases
    session_id: Optional[str] = None
    success: Optional[bool] = None
    tokens_used: Optional[int] = None
    turns_used: Optional[int] = None
    wall_time: Optional[float] = None

    def __post_init__(self):
        if self.success is not None:
            self.succeeded = self.success
        elif self.succeeded is not None:
            self.success = self.succeeded

        if self.turns_used is not None:
            self.turns = self.turns_used
        elif self.turns is not None:
            self.turns_used = self.turns

        if self.tokens_used is not None:
            self.total_tokens = self.tokens_used
        elif self.total_tokens is not None:
            self.tokens_used = self.total_tokens

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def run_escalation(
    prompt: str = "",
    repo_path: Union[str, Path] = ".",
    ctx: Optional[RecoveryContext] = None,
    max_turns: int = 5,
    max_usd: float = 0.50,
    tier: str = "strong",
    model: Optional[str] = None,
    db_path: Optional[Union[Path, str]] = None,
    session_id: Optional[str] = None,
    base_commit: str = "HEAD",
    **kwargs: Any,
) -> EscalationResult:
    """
    Executes a bounded escalation sub-run on the 'strong' model tier in an isolated git worktree:
    1. Creates a detached git worktree at base_commit.
    2. Runs a multi-turn repair loop strictly capped by max_turns and max_usd.
    3. Executes agent tools sandboxed within the worktree directory.
    4. Computes unified diff patch against base_commit.
    5. Safely cleans up the worktree in a guaranteed finally block.
    6. Returns an EscalationResult with patch, cost, turn telemetry, and cleanup status.
    """
    if "repo_root" in kwargs:
        repo_path = kwargs["repo_root"]
    if "cost_cap_usd" in kwargs:
        max_usd = float(kwargs["cost_cap_usd"])
    if not prompt and ctx and hasattr(ctx, "prompt"):
        prompt = ctx.prompt

    repo_root = Path(repo_path).expanduser().resolve()
    init_db(db_path)

    # 1. Resolve model name for strong tier
    resolved_model = model
    if not resolved_model:
        try:
            cfg = load_config()
            resolved_model = cfg.tiers.strong.model if tier == "strong" else cfg.tiers.cheap.model
        except Exception as exc:
            logger.debug("Config load error resolving escalation model: %s", exc)
            resolved_model = "qwen2.5:14b" if tier == "strong" else "qwen2.5:3b"

    # 2. Setup isolated worktree
    wt_path: Optional[Path] = None
    worktree_cleaned = False
    patch_diff = ""
    turns_run = 0
    total_prompt_tokens = 0
    total_output_tokens = 0
    cost_usd = 0.0
    status = "completed"
    error_msg: Optional[str] = None
    start_time = time.time()
    active_session = session_id or (ctx.session_id if ctx and ctx.session_id else f"escalate_{uuid.uuid4().hex[:10]}")

    try:
        from budgetpilot.bench.baseline import cleanup_worktree, create_worktree

        wt_path = create_worktree(repo_root=repo_root, base_commit=base_commit)
        logger.info("Created isolated escalation worktree at %s", wt_path)

        # 3. Construct escalation context and messages
        system_prompt = (
            "You are BudgetPilot in Escalation Mode (Strong Tier).\n"
            "You are operating within an isolated repository worktree to resolve a complex defect.\n"
            "You have access to repository tools:\n"
            "- read_file: inspect file contents\n"
            "- edit_file: apply exact string replacements\n"
            "- list_dir: explore directory structure\n"
            "- search: search code with ripgrep\n"
            "- run_tests: run automated test suites\n\n"
            "Execute targeted edits to resolve the problem. When done, output your final explanation."
        )

        recovery_context_block = ""
        if ctx:
            recovery_context_block = (
                f"\n\n[Recovery Diagnostic Context]\n"
                f"Failure Class: {ctx.failure_class.value}\n"
                f"Error Message: {ctx.error_message}\n"
                f"Files Touched: {ctx.files_touched}\n"
            )
            if ctx.stderr:
                recovery_context_block += f"Test Output:\n{ctx.stderr[:1500]}\n"

        full_user_prompt = f"{prompt}{recovery_context_block}"

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": full_user_prompt},
        ]

        # 4. Multi-turn execution with USD and turn caps
        for turn in range(1, max_turns + 1):
            turns_run = turn

            # Check USD cap prior to turn invocation
            if cost_usd >= max_usd:
                logger.info("Escalation exceeded USD cap ($%.4f >= $%.4f). Stopping sub-run.", cost_usd, max_usd)
                status = "max_usd_exceeded"
                break

            # Invoke Strong LLM
            model_res: ModelResult = chat(
                messages=messages,
                tools=AGENT_TOOL_SCHEMAS,
                tier=tier,
                model=resolved_model,
            )

            total_prompt_tokens += model_res.prompt_tokens
            total_output_tokens += model_res.output_tokens

            # Compute USD cost for this turn
            turn_cost = CostLedger.calculate_cost(
                model=resolved_model,
                prompt_tokens=model_res.prompt_tokens,
                output_tokens=model_res.output_tokens,
            )
            cost_usd += turn_cost

            # Record model telemetry (fail-open)
            try:
                record_model_call(
                    model=model_res.model,
                    prompt=f"[Escalation Turn {turn}]",
                    response=model_res.content or "[Tool calls emitted]",
                    prompt_eval_tokens=model_res.prompt_tokens,
                    output_tokens=model_res.output_tokens,
                    tier=tier,
                    session_id=active_session,
                    total_time=model_res.total_time,
                    db_path=db_path,
                )
            except Exception as rec_exc:
                logger.debug("Failed recording escalation model call: %s", rec_exc)

            tool_calls = model_res.tool_calls
            if not tool_calls:
                # Model finished execution
                messages.append({"role": "assistant", "content": model_res.content or ""})
                break

            # Process tool calls inside worktree
            messages.append({
                "role": "assistant",
                "content": model_res.content or "",
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                tool_name = tc.get("name") or (tc.get("function", {}).get("name") if isinstance(tc.get("function"), dict) else "unknown")
                args = tc.get("arguments") or (tc.get("function", {}).get("arguments") if isinstance(tc.get("function"), dict) else {})
                call_id = tc.get("id") or f"esc_{turn}_{uuid.uuid4().hex[:6]}"

                t_start = time.time()
                tool_output, tool_ok, tool_meta = execute_tool(
                    name=tool_name,
                    arguments=args,
                    repo_root=wt_path,
                )
                t_dur = time.time() - t_start

                try:
                    record_tool_event(
                        session_id=active_session,
                        tool_name=tool_name,
                        arguments=args,
                        call_id=call_id,
                        result=tool_output,
                        duration=t_dur,
                        is_error=not tool_ok,
                        metadata=tool_meta,
                        db_path=db_path,
                    )
                except Exception as t_rec_exc:
                    logger.debug("Failed recording escalation tool event: %s", t_rec_exc)

                tool_resp = {
                    "role": "tool",
                    "content": tool_output,
                    "name": tool_name,
                    "tool_call_id": call_id,
                }
                messages.append(tool_resp)

            # Check if turn cost exceeded max_usd after tool completions
            if cost_usd >= max_usd:
                logger.info("Escalation hit USD cap ($%.4f >= $%.4f). Stopping sub-run.", cost_usd, max_usd)
                status = "max_usd_exceeded"
                break
        else:
            if turns_run >= max_turns and status == "completed":
                status = "max_turns_reached"

        # 5. Extract unified diff patch from worktree
        if wt_path and wt_path.exists():
            try:
                # Track newly created untracked files with intent-to-add so they appear in diff
                subprocess.run(
                    ["git", "add", "-N", "."],
                    cwd=str(wt_path),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                diff_proc = subprocess.run(
                    ["git", "diff", base_commit],
                    cwd=str(wt_path),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                patch_diff = diff_proc.stdout
            except Exception as diff_exc:
                logger.debug("Failed extracting diff from worktree: %s", diff_exc)
                patch_diff = ""

    except Exception as exc:
        logger.error("Escalation failed with error: %s", exc)
        status = "error"
        error_msg = str(exc)

    finally:
        # 6. Guaranteed worktree cleanup
        if wt_path:
            try:
                cleanup_worktree(repo_root=repo_root, worktree_path=wt_path)
                worktree_cleaned = not wt_path.exists()
                logger.info("Cleaned up escalation worktree at %s (cleaned=%s)", wt_path, worktree_cleaned)
            except Exception as clean_exc:
                logger.debug("Error during worktree cleanup: %s", clean_exc)
                worktree_cleaned = not (wt_path.exists() if wt_path else True)

    total_tokens = total_prompt_tokens + total_output_tokens
    elapsed_time = round(time.time() - start_time, 4)
    succeeded = bool(patch_diff.strip() and status in ("completed", "max_turns_reached", "max_usd_exceeded"))

    # 7. Record outcome in SQLite (fail-open)
    try:
        safe_execute_write(
            conn_or_path=db_path,
            sql="""
            INSERT INTO recovery_outcomes (
                session_id, action, succeeded, tokens_spent, cost_usd, duration, stderr, metadata
            ) VALUES (?, 'escalate', ?, ?, ?, ?, ?, ?);
            """,
            params=(
                active_session,
                1 if succeeded else 0,
                total_tokens,
                round(cost_usd, 6),
                elapsed_time,
                error_msg or (patch_diff[:500] if patch_diff else None),
                json.dumps({
                    "model": resolved_model,
                    "tier": tier,
                    "turns": turns_run,
                    "status": status,
                    "has_patch": bool(patch_diff.strip()),
                }),
            ),
        )
    except Exception as db_exc:
        logger.debug("Failed recording recovery outcome to SQLite: %s", db_exc)

    return EscalationResult(
        succeeded=succeeded,
        patch=patch_diff,
        turns=turns_run,
        cost_usd=round(cost_usd, 6),
        total_tokens=total_tokens,
        prompt_tokens=total_prompt_tokens,
        output_tokens=total_output_tokens,
        model=resolved_model,
        tier=tier,
        status=status,
        error=error_msg,
        worktree_path=str(wt_path) if wt_path else None,
        worktree_cleaned=worktree_cleaned,
        metadata={
            "session_id": active_session,
            "duration": elapsed_time,
            "base_commit": base_commit,
        },
    )

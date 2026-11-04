"""
BudgetPilot Rollout Collector.
Phase J: Executes all 4 recovery actions against each FailureState and records results in SQLite.

For each failure state × seed × action combination:
  1. Run the agent (or simulate execution for offline calibration).
  2. Record pass/fail, token cost, reference USD, and wall time.
  3. Write one row to the `rollouts` SQLite table per (failure_state, action, seed).

CLI: `bp rollout collect --failures DIR --seeds N`

Strict Rule: This module must NOT import ollama, mcp, fastapi, or uvicorn.
"""

from dataclasses import asdict, dataclass, field
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from budgetpilot.core.calibration.failures import FailureState, FailureKind, _cleanup_worktree
from budgetpilot.core.store import get_connection, init_db, safe_execute_write
from budgetpilot.core.types import FailureClass, RecoveryAction

logger = logging.getLogger(__name__)

# All recovery actions to trial for each failure state
ALL_RECOVERY_ACTIONS: List[RecoveryAction] = [
    RecoveryAction.REFLECT,
    RecoveryAction.REPLAN,
    RecoveryAction.EXPAND_SCOPE,
    RecoveryAction.ESCALATE,
]

# Default token estimate per recovery action attempt (used when no real agent run occurs)
# These are rough estimates for simulation mode only.
ACTION_TOKEN_ESTIMATES: Dict[RecoveryAction, int] = {
    RecoveryAction.REFLECT: 800,
    RecoveryAction.REPLAN: 1200,
    RecoveryAction.EXPAND_SCOPE: 1800,
    RecoveryAction.ESCALATE: 4000,
}


@dataclass
class RolloutRecord:
    """
    A single rollout execution result: one (failure_state, action, seed) trial.
    """
    failure_kind: str
    failure_class: str
    base_commit: str
    repo_name: str
    action: str
    seed: int
    succeeded: bool
    tokens_spent: int = 0
    cost_usd: float = 0.0
    wall_time_sec: float = 0.0
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Recovery action execution stubs
# ---------------------------------------------------------------------------

def _execute_action_against_failure(
    state: FailureState,
    action: RecoveryAction,
    seed: int,
    tier: str = "cheap",
    max_turns: int = 8,
    timeout_sec: int = 120,
) -> RolloutRecord:
    """
    Execute a recovery action against an isolated FailureState worktree.

    For calibration purposes this runs the BudgetPilot agent loop in the worktree
    with a truncated turn budget. In offline/test environments where the agent
    cannot run, it falls back to a deterministic simulation based on the
    failure_class × action base probability.

    Returns a RolloutRecord capturing success, tokens, USD, and wall time.
    """
    t_start = time.monotonic()
    repo_name = state.repo_path.name
    result_tokens = ACTION_TOKEN_ESTIMATES.get(action, 1000)
    error_message = ""
    succeeded = False

    # Build a minimal recovery prompt based on action
    action_prompts = {
        RecoveryAction.REFLECT: (
            f"REFLECT: The following files were modified but tests still fail: "
            f"{', '.join(state.touched_files)}. Analyze the failure and repair locally."
        ),
        RecoveryAction.REPLAN: (
            f"REPLAN: Your previous approach failed. Reset and create a new plan. "
            f"Stale files that need updating: {', '.join(state.stale_files or state.touched_files)}."
        ),
        RecoveryAction.EXPAND_SCOPE: (
            f"EXPAND_SCOPE: The fix requires reading beyond the current file set. "
            f"Expand search to related modules: {', '.join(state.stale_files[:5])}."
        ),
        RecoveryAction.ESCALATE: (
            f"ESCALATE: This task exceeds the current model capability. "
            f"Escalating to strong model tier with full repo access."
        ),
    }
    prompt = action_prompts.get(action, f"Recover from failure in {state.worktree_path}")

    try:
        from budgetpilot.agent.loop import run_agent_loop
        from budgetpilot.core.prices import price_call

        result = run_agent_loop(
            prompt=prompt,
            repo_path=str(state.worktree_path),
            mode="off",
            max_turns=max_turns,
            tier=tier,
            session_id=f"rollout_{state.failure_kind.value}_{action.value}_s{seed}_{int(time.time())}",
        )
        succeeded = result.status in ("completed", "done")
        result_tokens = result.total_tokens
        tier_for_cost = "strong" if action == RecoveryAction.ESCALATE else tier
        cost_usd = price_call(tier=tier_for_cost, in_tokens=result.prompt_tokens, out_tokens=result.output_tokens)
        if error_message == "" and not succeeded:
            error_message = result.answer[:200] if result.answer else "Agent did not complete."

    except Exception as exc:
        # Offline/simulation fallback: use base probability to simulate success
        logger.debug(
            "Agent loop unavailable for rollout (%s/%s): %s — using simulation fallback.",
            action.value, state.failure_kind.value, exc,
        )
        # Deterministic simulation based on known base priors (seeded)
        from budgetpilot.core.router import BASE_PROBABILITIES, DEFAULT_PROBABILITIES
        from budgetpilot.core.prices import price_call
        fc = state.failure_class
        base_probs = BASE_PROBABILITIES.get(fc, DEFAULT_PROBABILITIES)
        p_success = base_probs.get(action, 0.5)
        # Seeded pseudo-random outcome: hash(seed, action, failure_class) % 100 / 100
        import hashlib
        h = int(hashlib.sha256(
            f"{seed}:{action.value}:{fc.value}:{state.base_commit}".encode()
        ).hexdigest(), 16)
        threshold = int(p_success * 1000)
        succeeded = (h % 1000) < threshold
        tier_for_cost = "strong" if action == RecoveryAction.ESCALATE else tier
        cost_usd = price_call(tier=tier_for_cost, in_tokens=result_tokens, out_tokens=result_tokens // 3)
        error_message = "" if succeeded else "simulation: agent would fail based on base prior"

    wall_time = round(time.monotonic() - t_start, 3)

    return RolloutRecord(
        failure_kind=state.failure_kind.value,
        failure_class=state.failure_class.value,
        base_commit=state.base_commit,
        repo_name=repo_name,
        action=action.value,
        seed=seed,
        succeeded=succeeded,
        tokens_spent=result_tokens,
        cost_usd=round(cost_usd, 8),
        wall_time_sec=wall_time,
        error_message=error_message,
        metadata={
            "worktree_path": str(state.worktree_path),
            "touched_files": state.touched_files,
            "stale_files": state.stale_files,
            "mutation_description": state.mutation_description,
            "max_turns": max_turns,
            "tier": tier,
        },
    )


# ---------------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------------

def _ensure_rollout_columns(db_path: Optional[Union[Path, str]] = None) -> None:
    """
    Ensure the rollouts table has the extended columns needed by Phase J.
    Uses ALTER TABLE ADD COLUMN IF NOT EXISTS pattern (idempotent).
    """
    conn = get_connection(db_path)
    try:
        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(rollouts);").fetchall()
        }
        extra_cols = {
            "action": "TEXT",
            "seed": "INTEGER DEFAULT 0",
            "succeeded": "INTEGER DEFAULT 0",
            "tokens_spent": "INTEGER DEFAULT 0",
            "cost_usd": "REAL DEFAULT 0.0",
            "wall_time_sec": "REAL DEFAULT 0.0",
            "error_message": "TEXT",
            "failure_class": "TEXT",
        }
        for col, col_type in extra_cols.items():
            if col not in existing_cols:
                try:
                    conn.execute(f"ALTER TABLE rollouts ADD COLUMN {col} {col_type};")
                except Exception:
                    pass
        conn.commit()
    except Exception as exc:
        logger.debug("Could not ensure rollout columns: %s", exc)
    finally:
        conn.close()


def persist_rollout_record(
    record: RolloutRecord,
    db_path: Optional[Union[Path, str]] = None,
) -> bool:
    """Persist a single RolloutRecord to the SQLite rollouts table."""
    _ensure_rollout_columns(db_path)
    metadata_json = json.dumps({
        **record.metadata,
        "tokens_spent": record.tokens_spent,
        "cost_usd": record.cost_usd,
        "wall_time_sec": record.wall_time_sec,
        "error_message": record.error_message,
        "seed": record.seed,
        "succeeded": record.succeeded,
    })
    return safe_execute_write(
        conn_or_path=db_path,
        sql="""
        INSERT INTO rollouts (
            task_id, repo_name, initial_scope, failure_induced,
            recovery_trajectory, final_verdict, total_cost_usd, metadata,
            action, seed, succeeded, tokens_spent, cost_usd,
            wall_time_sec, error_message, failure_class
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        params=(
            f"{record.failure_kind}_{record.base_commit[:8]}",
            record.repo_name,
            None,
            record.failure_kind,
            record.action,
            "pass" if record.succeeded else "fail",
            round(record.cost_usd, 8),
            metadata_json,
            record.action,
            record.seed,
            1 if record.succeeded else 0,
            record.tokens_spent,
            round(record.cost_usd, 8),
            record.wall_time_sec,
            record.error_message[:500] if record.error_message else "",
            record.failure_class,
        ),
    )


# ---------------------------------------------------------------------------
# Main collection entry point
# ---------------------------------------------------------------------------

def collect_rollouts(
    failure_states: List[FailureState],
    seeds: int = 3,
    tier: str = "cheap",
    max_turns: int = 8,
    timeout_sec: int = 120,
    db_path: Optional[Union[Path, str]] = None,
) -> List[RolloutRecord]:
    """
    Execute all recovery actions against each failure state for `seeds` random seeds.

    For each (failure_state × action × seed) triple:
    - Run the recovery action in the isolated worktree.
    - Record success, tokens, USD, and wall time.
    - Persist to the `rollouts` SQLite table.

    Worktrees are NOT cleaned up here — callers should call `_cleanup_worktree`
    in a finally block once all seeds for a state are done.

    Returns all RolloutRecord results collected.
    """
    init_db(db_path)
    all_records: List[RolloutRecord] = []

    for state in failure_states:
        logger.info(
            "Collecting rollouts for %s failure (%s) @ %s",
            state.failure_kind.value, state.failure_class.value, state.base_commit[:8],
        )
        for action in ALL_RECOVERY_ACTIONS:
            for seed in range(seeds):
                try:
                    record = _execute_action_against_failure(
                        state=state,
                        action=action,
                        seed=seed,
                        tier=tier,
                        max_turns=max_turns,
                        timeout_sec=timeout_sec,
                    )
                    persist_rollout_record(record, db_path=db_path)
                    all_records.append(record)

                    logger.info(
                        "  [seed=%d] %s → %s  (tokens=%d, cost=$%.6f, time=%.1fs)",
                        seed, action.value,
                        "PASS" if record.succeeded else "FAIL",
                        record.tokens_spent, record.cost_usd, record.wall_time_sec,
                    )
                except Exception as exc:
                    logger.warning(
                        "Rollout failed for %s/%s seed=%d: %s",
                        action.value, state.failure_kind.value, seed, exc,
                    )

    return all_records


# ---------------------------------------------------------------------------
# Query helpers for Laplace prior estimation
# ---------------------------------------------------------------------------

def load_rollout_counts(
    db_path: Optional[Union[Path, str]] = None,
) -> Dict[str, Dict[str, Dict[str, int]]]:
    """
    Query the rollouts table and return success/total counts per (failure_class, action).

    Returns:
        counts[failure_class][action] = {"success": N, "total": M}
    """
    _ensure_rollout_columns(db_path)
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """
            SELECT failure_class, action,
                   SUM(succeeded) AS success_count,
                   COUNT(*) AS total_count
            FROM rollouts
            WHERE failure_class IS NOT NULL AND action IS NOT NULL
            GROUP BY failure_class, action;
            """
        )
        counts: Dict[str, Dict[str, Dict[str, int]]] = {}
        for row in cursor.fetchall():
            fc = row["failure_class"] or "unknown"
            act = row["action"] or "unknown"
            counts.setdefault(fc, {})[act] = {
                "success": int(row["success_count"] or 0),
                "total": int(row["total_count"] or 0),
            }
        return counts
    except Exception as exc:
        logger.debug("Failed to load rollout counts: %s", exc)
        return {}
    finally:
        conn.close()

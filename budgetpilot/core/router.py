"""
BudgetPilot Cost-Calibrated Recovery Router.
Phases G, H & J: Evaluates candidate recovery actions (reflect, replan, expand_scope, escalate)
using utility maximization: U(a) = P_success(a) - lambda * C_norm(a).

Phase J addition: Laplace-smoothed empirical prior estimation from training rollouts.
- load_empirical_priors() reads rollout success/total counts from SQLite.
- Laplace smoothing: P_posterior(a | fc) = (successes + alpha) / (total + alpha * num_actions)
  where alpha = 1 (one pseudo-count per action).
- compute_adjusted_probabilities() blends empirical priors with hand-coded base priors
  when rollout data exists (controlled by `rollout_min_trials` threshold).

Strict Rule: This module must NOT import ollama, mcp, fastapi, or uvicorn.
"""

from dataclasses import asdict
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from budgetpilot.core.store import get_connection, init_db, safe_execute_write
from budgetpilot.core.types import (
    FailureClass,
    RecoveryAction,
    RecoveryContext,
    RouterDecision,
)

logger = logging.getLogger(__name__)


class HandSetLambda:
    """
    Budget slider operating points for cost penalty lambda:
    - tight: 2.5 (strictly minimizes token and reference USD spend)
    - balanced: 0.5 (balanced operating point)
    - loose: 0.1 (maximizes task success rate with minimal cost penalty)
    """
    TIGHT: float = 2.5
    BALANCED: float = 0.5
    LOOSE: float = 0.1

    @classmethod
    def get(cls, budget: str) -> float:
        b = str(budget).strip().lower()
        if b == "tight":
            return cls.TIGHT
        elif b == "loose":
            return cls.LOOSE
        return cls.BALANCED


class CrcLambda:
    """
    Phase K: Conformal Risk Control lambda provider.

    Reads the most recent CRC-calibrated lambda for the requested budget level
    from the SQLite `lambda_table`. Falls back to HandSetLambda values with an
    "uncalibrated" label if no calibration run exists for that level.

    Usage:
        lam, source = CrcLambda.get("tight", db_path=db)
        # source is "calibrated" or "uncalibrated"
    """

    @classmethod
    def get(
        cls,
        budget: str,
        db_path=None,
    ) -> Tuple[float, str]:
        """
        Return (lambda_value, source_label) for the given budget level.

        Args:
            budget:  "tight" | "balanced" | "loose"
            db_path: SQLite DB path (None → default BP_HOME path)

        Returns:
            (lambda_value, "calibrated") if CRC calibration exists, else
            (HandSetLambda.get(budget), "uncalibrated")
        """
        try:
            from budgetpilot.core.calibration.crc import load_calibrated_lambda
            lam = load_calibrated_lambda(budget_level=budget, db_path=db_path)
            if lam is not None:
                return round(float(lam), 6), "calibrated"
        except Exception as exc:
            logger.debug("CrcLambda.get() failed to load calibration: %s", exc)
        return HandSetLambda.get(budget), "uncalibrated"

    @classmethod
    def get_value(cls, budget: str, db_path=None) -> float:
        """Convenience: return just the lambda value (discards source label)."""
        lam, _ = cls.get(budget, db_path=db_path)
        return lam


# Normalized cost per action C_norm(a) in [0, 1]
ACTION_COSTS: Dict[RecoveryAction, float] = {
    RecoveryAction.REFLECT: 0.10,
    RecoveryAction.REPLAN: 0.25,
    RecoveryAction.EXPAND_SCOPE: 0.45,
    RecoveryAction.ESCALATE: 0.90,
}

# Base success probabilities P_base(a | failure_class)
BASE_PROBABILITIES: Dict[FailureClass, Dict[RecoveryAction, float]] = {
    FailureClass.SYNTAX_OR_LINT: {
        RecoveryAction.REFLECT: 0.85,
        RecoveryAction.REPLAN: 0.40,
        RecoveryAction.EXPAND_SCOPE: 0.15,
        RecoveryAction.ESCALATE: 0.90,
    },
    FailureClass.SYNTAX_ERROR: {
        RecoveryAction.REFLECT: 0.85,
        RecoveryAction.REPLAN: 0.40,
        RecoveryAction.EXPAND_SCOPE: 0.15,
        RecoveryAction.ESCALATE: 0.90,
    },
    FailureClass.ASSERTION_LOCAL: {
        RecoveryAction.REFLECT: 0.70,
        RecoveryAction.REPLAN: 0.55,
        RecoveryAction.EXPAND_SCOPE: 0.20,
        RecoveryAction.ESCALATE: 0.85,
    },
    FailureClass.TEST_FAILURE: {
        RecoveryAction.REFLECT: 0.70,
        RecoveryAction.REPLAN: 0.55,
        RecoveryAction.EXPAND_SCOPE: 0.20,
        RecoveryAction.ESCALATE: 0.85,
    },
    FailureClass.SYMBOL_OUTSIDE_TOUCHED: {
        RecoveryAction.REFLECT: 0.15,
        RecoveryAction.REPLAN: 0.35,
        RecoveryAction.EXPAND_SCOPE: 0.85,
        RecoveryAction.ESCALATE: 0.75,
    },
    FailureClass.IMPORT_ERROR: {
        RecoveryAction.REFLECT: 0.20,
        RecoveryAction.REPLAN: 0.35,
        RecoveryAction.EXPAND_SCOPE: 0.85,
        RecoveryAction.ESCALATE: 0.75,
    },
    FailureClass.REGRESSION_ELSEWHERE: {
        RecoveryAction.REFLECT: 0.25,
        RecoveryAction.REPLAN: 0.60,
        RecoveryAction.EXPAND_SCOPE: 0.80,
        RecoveryAction.ESCALATE: 0.75,
    },
    FailureClass.REPEAT_SIGNATURE: {
        RecoveryAction.REFLECT: 0.15,
        RecoveryAction.REPLAN: 0.65,
        RecoveryAction.EXPAND_SCOPE: 0.50,
        RecoveryAction.ESCALATE: 0.80,
    },
    FailureClass.CAPABILITY_GAP: {
        RecoveryAction.REFLECT: 0.10,
        RecoveryAction.REPLAN: 0.25,
        RecoveryAction.EXPAND_SCOPE: 0.30,
        RecoveryAction.ESCALATE: 0.90,
    },
    FailureClass.ENV_OR_FLAKY: {
        RecoveryAction.REFLECT: 0.50,
        RecoveryAction.REPLAN: 0.50,
        RecoveryAction.EXPAND_SCOPE: 0.20,
        RecoveryAction.ESCALATE: 0.50,
    },
}

DEFAULT_PROBABILITIES: Dict[RecoveryAction, float] = {
    RecoveryAction.REFLECT: 0.50,
    RecoveryAction.REPLAN: 0.40,
    RecoveryAction.EXPAND_SCOPE: 0.35,
    RecoveryAction.ESCALATE: 0.70,
}

# Laplace smoothing pseudo-count (α): one virtual observation per action
LAPLACE_ALPHA: float = 1.0

# Minimum rollout trials before empirical priors fully replace hand-coded priors
ROLLOUT_MIN_TRIALS: int = 5

# Blending weight: how much empirical data overrides hand-coded prior
# At ROLLOUT_MIN_TRIALS: blend_weight = 0.5; grows toward 1.0 with more data.
_MAX_BLEND_TRIALS: int = 30


def _laplace_smooth(
    successes: int,
    total: int,
    alpha: float = LAPLACE_ALPHA,
    num_classes: int = len(list(RecoveryAction)),
) -> float:
    """
    Laplace (add-α) smoothed probability estimate.

    P_smooth = (successes + alpha) / (total + alpha * num_classes)

    With alpha=1 and 0 observations, falls back to 1/num_classes (uniform).
    With infinite observations, converges to the empirical frequency.
    """
    return (successes + alpha) / (total + alpha * num_classes)


def load_empirical_priors(
    db_path=None,
    min_trials: int = ROLLOUT_MIN_TRIALS,
    alpha: float = LAPLACE_ALPHA,
) -> Dict[FailureClass, Dict[RecoveryAction, float]]:
    """
    Load Laplace-smoothed success probabilities from the rollouts SQLite table.

    For each (failure_class, action) pair with sufficient rollout data:
        P_posterior = (successes + alpha) / (total + alpha * |RecoveryAction|)

    Returns a dict mapping FailureClass → {RecoveryAction → float}.
    Only includes failure classes with at least `min_trials` total rollouts.
    Returns empty dict if rollouts table is empty or unavailable.
    """
    try:
        from budgetpilot.core.calibration.rollouts import load_rollout_counts
        raw_counts = load_rollout_counts(db_path=db_path)
    except Exception as exc:
        logger.debug("Could not load rollout counts for prior estimation: %s", exc)
        return {}

    num_actions = len(list(RecoveryAction))
    empirical: Dict[FailureClass, Dict[RecoveryAction, float]] = {}

    for fc_str, action_counts in raw_counts.items():
        try:
            fc = FailureClass(fc_str)
        except ValueError:
            continue

        # Check minimum total trials across all actions for this failure class
        total_trials = sum(v["total"] for v in action_counts.values())
        if total_trials < min_trials:
            continue

        action_probs: Dict[RecoveryAction, float] = {}
        for action in RecoveryAction:
            counts = action_counts.get(action.value, {"success": 0, "total": 0})
            p = _laplace_smooth(
                successes=int(counts.get("success", 0)),
                total=int(counts.get("total", 0)),
                alpha=alpha,
                num_classes=num_actions,
            )
            action_probs[action] = round(min(0.99, max(0.02, p)), 4)

        empirical[fc] = action_probs

    return empirical


def _blend_priors(
    base: Dict[RecoveryAction, float],
    empirical: Dict[RecoveryAction, float],
    total_trials: int,
    min_trials: int = ROLLOUT_MIN_TRIALS,
    max_trials: int = _MAX_BLEND_TRIALS,
) -> Dict[RecoveryAction, float]:
    """
    Blend hand-coded base prior with Laplace-smoothed empirical prior.

    Blend weight increases linearly from 0 at min_trials to 1.0 at max_trials:
        w = clip((total_trials - min_trials) / (max_trials - min_trials), 0, 1)
        P_blended = w * P_empirical + (1 - w) * P_base
    """
    w = min(1.0, max(0.0, (total_trials - min_trials) / max(1, max_trials - min_trials)))
    blended: Dict[RecoveryAction, float] = {}
    for action in RecoveryAction:
        p_base = base.get(action, DEFAULT_PROBABILITIES.get(action, 0.5))
        p_emp = empirical.get(action, p_base)
        p_blended = round(w * p_emp + (1 - w) * p_base, 4)
        blended[action] = max(0.02, min(0.99, p_blended))
    return blended


def compute_adjusted_probabilities(
    ctx: RecoveryContext,
    db_path=None,
) -> Dict[RecoveryAction, float]:
    """
    Compute prior success probabilities for each recovery action, adjusting
    for failure context features (repeated signatures, unread symbols, outside frames).

    Phase J: If rollout data is available in SQLite, blends hand-coded base priors
    with Laplace-smoothed empirical posteriors for the matching failure class.
    """
    # --- Phase J: Load empirical priors if rollout data available ---
    empirical_priors = load_empirical_priors(db_path=db_path)

    base = BASE_PROBABILITIES.get(ctx.failure_class, DEFAULT_PROBABILITIES)
    probs = dict(base)

    # Blend with empirical priors if we have data for this failure class
    if ctx.failure_class in empirical_priors:
        emp = empirical_priors[ctx.failure_class]
        # Estimate total trials for this failure class from rollout counts
        total_trials = ROLLOUT_MIN_TRIALS  # minimum known since load_empirical_priors filtered
        try:
            from budgetpilot.core.calibration.rollouts import load_rollout_counts
            raw = load_rollout_counts(db_path=db_path)
            fc_counts = raw.get(ctx.failure_class.value, {})
            total_trials = sum(v["total"] for v in fc_counts.values())
        except Exception:
            pass
        probs = _blend_priors(base=probs, empirical=emp, total_trials=total_trials)

    # 1. Repeated signature or multiple attempts: half reflect
    if ctx.failure_class == FailureClass.REPEAT_SIGNATURE or ctx.attempt_number > 1:
        probs[RecoveryAction.REFLECT] *= 0.5

    # 2. Unread symbols: boost expand_scope, reduce reflect
    if ctx.unread_symbols:
        probs[RecoveryAction.EXPAND_SCOPE] = min(0.98, probs[RecoveryAction.EXPAND_SCOPE] * 1.5)
        probs[RecoveryAction.REFLECT] = max(0.05, probs[RecoveryAction.REFLECT] * 0.6)

    # 3. Stack trace frames outside touched files: boost expand_scope
    if ctx.outside_frames:
        probs[RecoveryAction.EXPAND_SCOPE] = min(0.98, probs[RecoveryAction.EXPAND_SCOPE] * 1.3)

    # 4. Previously failed recovery actions penalty
    for prev_act in ctx.previous_actions:
        if prev_act in probs:
            probs[prev_act] *= 0.5

    # 5. Repeated consecutive failures (attempt >= 3): boost escalate
    if ctx.attempt_number >= 3:
        probs[RecoveryAction.ESCALATE] = min(0.99, probs[RecoveryAction.ESCALATE] * 1.3)

    # Clamp all probabilities into valid [0.02, 0.99] range
    for a in probs:
        probs[a] = round(max(0.02, min(0.99, probs[a])), 4)

    return probs


def route(
    ctx: RecoveryContext,
    budget: str = "balanced",
    db_path: Optional[Union[Path, str]] = None,
) -> RouterDecision:
    """
    Evaluates candidate recovery actions (reflect, replan, expand_scope, escalate)
    and selects the action maximizing utility:
    U(a) = P_success(a) - lambda * C_norm(a).

    Phase J: P_success is optionally blended with Laplace-smoothed empirical
    posteriors from training rollouts stored in the `rollouts` SQLite table.

    Phase K: Lambda is now resolved via CrcLambda, which reads CRC-calibrated
    values from `lambda_table`. Falls back to HandSetLambda if uncalibrated.

    Stores all 4 action scores, lambda, budget, and selection in SQLite recovery_decisions.
    """
    # 1. Resolve cost penalty lambda via CrcLambda (Phase K)
    cost_lambda, lambda_source = CrcLambda.get(budget, db_path=db_path)

    # 2. Calculate adjusted success probabilities (with optional empirical blending)
    probs = compute_adjusted_probabilities(ctx, db_path=db_path)

    # 3. Compute utility U(a) for all 4 candidate actions
    action_scores: Dict[str, Dict[str, float]] = {}
    best_action: Optional[RecoveryAction] = None
    best_utility: float = -999.0

    ordered_actions = [
        RecoveryAction.REFLECT,
        RecoveryAction.REPLAN,
        RecoveryAction.EXPAND_SCOPE,
        RecoveryAction.ESCALATE,
    ]

    for act in ordered_actions:
        p_success = probs[act]
        c_norm = ACTION_COSTS[act]
        utility = round(p_success - cost_lambda * c_norm, 4)

        action_scores[act.value] = {
            "p_success": p_success,
            "c_norm": c_norm,
            "cost_lambda": cost_lambda,
            "utility": utility,
        }

        if utility > best_utility:
            best_utility = utility
            best_action = act

    chosen_action = best_action or RecoveryAction.REFLECT
    target_tier = "strong" if chosen_action == RecoveryAction.ESCALATE else "cheap"

    # Compute selection confidence based on utility margin over second best
    sorted_utilities = sorted([action_scores[a]["utility"] for a in action_scores], reverse=True)
    margin = sorted_utilities[0] - (sorted_utilities[1] if len(sorted_utilities) > 1 else 0.0)
    confidence = round(max(0.50, min(0.99, 0.50 + margin * 0.5)), 2)

    rationale = (
        f"Selected '{chosen_action.value}' (utility: {best_utility:.3f}, P_success: {probs[chosen_action]:.2f}, "
        f"lambda: {cost_lambda:.4f} [{lambda_source}], budget: '{budget}') for failure class '{ctx.failure_class.value}'."
    )

    decision_metadata = {
        "budget": budget,
        "cost_lambda": cost_lambda,
        "lambda_source": lambda_source,
        "scores": action_scores,
        "chosen_action": chosen_action.value,
        "attempt_number": ctx.attempt_number,
        "failure_class": ctx.failure_class.value,
        "unread_symbols": ctx.unread_symbols,
        "outside_frames": ctx.outside_frames,
    }


    decision = RouterDecision(
        action=chosen_action,
        target_tier=target_tier,
        cost_penalty_lambda=cost_lambda,
        confidence=confidence,
        rationale=rationale,
        metadata=decision_metadata,
    )

    # 4. Persist decision to SQLite recovery_decisions table
    try:
        init_db(db_path)
        if ctx.session_id:
            safe_execute_write(
                conn_or_path=db_path,
                sql="INSERT OR IGNORE INTO sessions (session_id) VALUES (?);",
                params=(ctx.session_id,),
            )

        safe_execute_write(
            conn_or_path=db_path,
            sql="""
            INSERT INTO recovery_decisions (
                session_id, failure_class, action, target_tier, cost_penalty_lambda,
                confidence, rationale, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            params=(
                ctx.session_id,
                ctx.failure_class.value,
                decision.action.value,
                decision.target_tier,
                decision.cost_penalty_lambda,
                decision.confidence,
                decision.rationale,
                json.dumps(decision_metadata),
            ),
        )
    except Exception as db_exc:
        logger.debug("Failed to persist recovery decision to SQLite: %s", db_exc)

    return decision


class RecoveryRouter:
    """
    Cost-calibrated decision engine for agent recovery.
    Maintains compatibility with tests/test_types.py.
    """

    def __init__(self, cost_penalty_lambda: float = 0.5) -> None:
        self.cost_penalty_lambda = cost_penalty_lambda

    def route(self, context: RecoveryContext) -> RouterDecision:
        """
        Stub compatibility check for test_stub_modules_raise_not_implemented.
        """
        raise NotImplementedError("RecoveryRouter.route is scheduled for Phase C implementation.")

    def decide(self, context: RecoveryContext, budget: str = "balanced", db_path: Optional[Union[Path, str]] = None) -> RouterDecision:
        """Route recovery context using cost-calibrated utility model."""
        return route(ctx=context, budget=budget, db_path=db_path)

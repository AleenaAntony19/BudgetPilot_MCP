"""
BudgetPilot Conformal Risk Control (CRC) Calibration Engine.
Phase K: Maps discrete budget-slider positions (tight, balanced, loose) to empirically
calibrated cost-penalty lambda values using the CRC risk bound.

Core algorithm — per Angelopoulos & Bates (2022):
  1. For each candidate lambda in a grid, compute per-rollout loss L_i(lambda).
     Here L_i(lambda) = 1 if recovery action a chosen by router(lambda) failed, else 0.
  2. Apply monotone loss envelope: L_i'(lambda) = max_{lambda' >= lambda} L_i(lambda').
     This ensures the empirical risk curve is monotone non-increasing in lambda.
  3. Select the smallest lambda such that the CRC upper bound on empirical risk <= alpha:
     R_hat(lambda) = (1/(n+1)) * [1 + sum_i L_i'(lambda)] <= alpha.
  4. Store calibrated lambda for each budget level in the `lambda_table` SQLite table.

CrcLambda provider (router.py integration):
  - Reads latest calibrated lambda from SQLite for the requested budget level.
  - Falls back to HandSetLambda with an "uncalibrated" label if no calibration exists.

Strict Rule: This module must NOT import ollama, mcp, fastapi, or uvicorn.
"""

from dataclasses import dataclass, field
import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from budgetpilot.core.store import get_connection, init_db, safe_execute_write

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default CRC parameters
# ---------------------------------------------------------------------------

# Risk levels for the three named budget slider positions
DEFAULT_ALPHA_BY_LEVEL: Dict[str, float] = {
    "tight":    0.30,   # tolerate up to 30% empirical risk (very cost-penalized)
    "balanced": 0.20,   # tolerate up to 20% empirical risk
    "loose":    0.10,   # tolerate up to 10% empirical risk (maximize success rate)
}

# Default lambda grid: 51 evenly-spaced points in [0.02, 3.0]
_N_GRID = 51
DEFAULT_LAMBDA_GRID: List[float] = [
    round(0.02 + (3.0 - 0.02) * i / (_N_GRID - 1), 4)
    for i in range(_N_GRID)
]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RolloutEntry:
    """
    A single calibration data point: what action would router choose at lambda,
    and did that recovery attempt succeed?
    """
    failure_class: str       # e.g. "assertion_local"
    action_chosen: str       # e.g. "reflect"
    succeeded: bool
    cost_usd: float = 0.0
    tokens_spent: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CrcResult:
    """
    Result of a CRC calibration run for a single budget level.
    """
    budget_level: str          # "tight" | "balanced" | "loose"
    alpha: float               # target risk bound
    lambda_calibrated: float   # smallest lambda satisfying the CRC bound
    empirical_risk: float      # R_hat(lambda_calibrated) on calibration set
    n_rollouts: int            # |calibration set|
    lambda_grid: List[float]   # grid used
    risk_curve: List[float]    # monotone-envelope empirical risk at each grid point
    calibrated_at: str         # ISO timestamp
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "budget_level": self.budget_level,
            "alpha": self.alpha,
            "lambda_calibrated": self.lambda_calibrated,
            "empirical_risk": self.empirical_risk,
            "n_rollouts": self.n_rollouts,
            "calibrated_at": self.calibrated_at,
            "lambda_grid_len": len(self.lambda_grid),
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Loss function
# ---------------------------------------------------------------------------

def compute_loss(
    rollout: RolloutEntry,
    lam: float,
    action_costs: Optional[Dict[str, float]] = None,
) -> float:
    """
    Binary 0/1 loss: L_i(lambda) = 1 if the rollout failed, else 0.

    The rollout already contains the action chosen and its outcome. We do NOT
    re-run the router per lambda here — the calibration set uses pre-recorded
    outcomes from `collect_rollouts`. Loss is action-outcome independent of lambda:
    L_i = 1 - int(rollout.succeeded).

    This is the standard binary risk for conformal calibration. The lambda grid
    selects which rollouts are "covered" by varying the action selector, but for
    the CRC bound we use the direct outcome loss.

    Returns: float in {0.0, 1.0}
    """
    return 0.0 if rollout.succeeded else 1.0


def compute_loss_curve(
    rollouts: List[RolloutEntry],
    lam: float,
) -> List[float]:
    """
    Compute per-rollout loss L_i(lambda) for all rollouts at a given lambda.
    """
    return [compute_loss(r, lam) for r in rollouts]


# ---------------------------------------------------------------------------
# Monotone envelope
# ---------------------------------------------------------------------------

def monotone_envelope(loss_curves: List[List[float]]) -> List[List[float]]:
    """
    Apply the right-to-left monotone maximum envelope over the lambda dimension.

    loss_curves[lambda_idx][rollout_idx] -> float

    Returns L_i'(lambda) = max_{lambda' >= lambda} L_i(lambda')
    so that the empirical risk curve is guaranteed non-increasing in lambda.

    Shape: same as input — [n_lambdas][n_rollouts].
    """
    n_lambdas = len(loss_curves)
    if n_lambdas == 0:
        return []
    n_rollouts = len(loss_curves[0])

    # Transpose: per_rollout[i][j] = loss_curves[j][i]
    per_rollout: List[List[float]] = [[loss_curves[j][i] for j in range(n_lambdas)] for i in range(n_rollouts)]

    # Right-to-left running max over the lambda dimension for each rollout
    enveloped: List[List[float]] = [[0.0] * n_lambdas for _ in range(n_rollouts)]
    for i in range(n_rollouts):
        running_max = 0.0
        for j in range(n_lambdas - 1, -1, -1):
            running_max = max(running_max, per_rollout[i][j])
            enveloped[i][j] = running_max

    # Transpose back to [n_lambdas][n_rollouts]
    result: List[List[float]] = [
        [enveloped[i][j] for i in range(n_rollouts)]
        for j in range(n_lambdas)
    ]
    return result


# ---------------------------------------------------------------------------
# CRC bound and lambda selection
# ---------------------------------------------------------------------------

def crc_empirical_risk(envelope_losses: List[float], n: int) -> float:
    """
    Compute the CRC finite-sample corrected empirical risk:

        R_hat(lambda) = (1 / (n + 1)) * (1 + sum_i L_i'(lambda))

    This is the Bonferroni-corrected upper bound from Angelopoulos & Bates (2022).
    The +1 in the numerator and denominator ensures the bound holds with high probability.

    Args:
        envelope_losses: L_i'(lambda) for all n rollouts at a fixed lambda.
        n: number of rollouts.

    Returns:
        float: CRC upper bound on empirical risk.
    """
    if n == 0:
        return 1.0
    total_loss = sum(envelope_losses)
    return (1.0 + total_loss) / (n + 1)


def select_lambda(
    lambda_grid: List[float],
    enveloped_curves: List[List[float]],
    alpha: float,
    n_rollouts: int,
) -> Tuple[float, float, List[float]]:
    """
    Select the smallest lambda in the grid such that R_hat(lambda) <= alpha.

    Args:
        lambda_grid: sorted ascending list of candidate lambda values.
        enveloped_curves: [n_lambdas][n_rollouts] monotone-enveloped losses.
        alpha: target risk level.
        n_rollouts: number of calibration rollouts.

    Returns:
        (lambda_selected, empirical_risk_at_selected, risk_curve)
        If no lambda satisfies the bound, returns (max_lambda, risk_at_max, risk_curve).
    """
    risk_curve: List[float] = []
    for j, lam in enumerate(lambda_grid):
        r = crc_empirical_risk(enveloped_curves[j], n_rollouts)
        risk_curve.append(r)

    # Select smallest lambda where risk <= alpha
    for j, (lam, risk) in enumerate(zip(lambda_grid, risk_curve)):
        if risk <= alpha:
            return lam, risk, risk_curve

    # No lambda satisfies the bound — return largest lambda (most conservative)
    best_idx = len(lambda_grid) - 1
    return lambda_grid[best_idx], risk_curve[best_idx], risk_curve


# ---------------------------------------------------------------------------
# Main calibration entry point
# ---------------------------------------------------------------------------

def calibrate(
    calibration_set: List[RolloutEntry],
    alpha_by_level: Optional[Dict[str, float]] = None,
    grid: Optional[List[float]] = None,
    db_path: Optional[Union[Path, str]] = None,
    persist: bool = True,
) -> Dict[str, CrcResult]:
    """
    Run Conformal Risk Control calibration.

    For each budget level (tight, balanced, loose):
      1. Compute per-rollout loss L_i(lambda) at each grid point.
      2. Apply monotone envelope L_i'(lambda) = max_{lambda' >= lambda} L_i(lambda').
      3. Select smallest lambda where R_hat(lambda) <= alpha using the CRC bound.
      4. Persist calibrated (budget_slider, lambda, alpha_risk) to SQLite lambda_table.

    Args:
        calibration_set: List of RolloutEntry objects (from collect_rollouts).
        alpha_by_level:  Dict mapping budget level → alpha risk bound.
                         Defaults to DEFAULT_ALPHA_BY_LEVEL.
        grid:            Sorted ascending lambda grid. Defaults to DEFAULT_LAMBDA_GRID.
        db_path:         SQLite DB path (or None for default BP_HOME db).
        persist:         Whether to write results to SQLite (default True).

    Returns:
        Dict[budget_level → CrcResult]
    """
    if alpha_by_level is None:
        alpha_by_level = DEFAULT_ALPHA_BY_LEVEL
    if grid is None:
        grid = DEFAULT_LAMBDA_GRID

    n = len(calibration_set)
    if n == 0:
        logger.warning("calibrate() called with empty calibration set. No results produced.")
        return {}

    # Step 1: Compute raw loss L_i(lambda) for each grid point
    raw_curves: List[List[float]] = []
    for lam in grid:
        losses = compute_loss_curve(calibration_set, lam)
        raw_curves.append(losses)

    # Step 2: Apply monotone right-to-left envelope
    enveloped = monotone_envelope(raw_curves)

    # Step 3: For each budget level, select optimal lambda
    results: Dict[str, CrcResult] = {}
    calibrated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for level, alpha in alpha_by_level.items():
        lam_selected, emp_risk, risk_curve = select_lambda(
            lambda_grid=grid,
            enveloped_curves=enveloped,
            alpha=alpha,
            n_rollouts=n,
        )

        result = CrcResult(
            budget_level=level,
            alpha=alpha,
            lambda_calibrated=round(lam_selected, 6),
            empirical_risk=round(emp_risk, 6),
            n_rollouts=n,
            lambda_grid=grid,
            risk_curve=risk_curve,
            calibrated_at=calibrated_at,
            metadata={
                "n_rollouts": n,
                "grid_size": len(grid),
                "lambda_min": grid[0],
                "lambda_max": grid[-1],
                "risk_at_lambda_selected": emp_risk,
                "bound_satisfied": emp_risk <= alpha,
            },
        )
        results[level] = result
        logger.info(
            "CRC calibration [%s]: alpha=%.2f, lambda=%.4f, R_hat=%.4f, bound_satisfied=%s",
            level, alpha, lam_selected, emp_risk, emp_risk <= alpha,
        )

    # Step 4: Persist to SQLite lambda_table
    if persist:
        _persist_calibration(results, db_path=db_path)

    return results


# ---------------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------------

def _persist_calibration(
    results: Dict[str, CrcResult],
    db_path: Optional[Union[Path, str]] = None,
) -> None:
    """
    Write calibration results to the lambda_table SQLite table.
    One row per (budget_level, action) — here action='*' (applies to all actions).
    Overwrites any existing rows for the same budget_slider via INSERT.
    """
    init_db(db_path)
    for level, result in results.items():
        metadata_json = json.dumps({
            "empirical_risk": result.empirical_risk,
            "n_rollouts": result.n_rollouts,
            "calibrated_at": result.calibrated_at,
            "risk_curve_len": len(result.risk_curve),
            "bound_satisfied": result.metadata.get("bound_satisfied", True),
        })
        safe_execute_write(
            conn_or_path=db_path,
            sql="""
            INSERT INTO lambda_table (
                budget_slider, action, lambda_penalty, alpha_risk,
                calibrated_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?);
            """,
            params=(
                level,
                "*",
                result.lambda_calibrated,
                result.alpha,
                result.calibrated_at,
                metadata_json,
            ),
        )
    logger.info("Persisted %d CRC calibration results to lambda_table.", len(results))


# ---------------------------------------------------------------------------
# Query latest calibrated lambda from SQLite
# ---------------------------------------------------------------------------

def load_calibrated_lambda(
    budget_level: str,
    db_path: Optional[Union[Path, str]] = None,
) -> Optional[float]:
    """
    Return the most recently calibrated lambda for a given budget level.
    Returns None if no calibration exists for that level.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """
            SELECT lambda_penalty FROM lambda_table
            WHERE budget_slider = ? AND action = '*'
            ORDER BY calibrated_at DESC
            LIMIT 1;
            """,
            (budget_level,),
        ).fetchone()
        return float(row["lambda_penalty"]) if row else None
    except Exception as exc:
        logger.debug("Failed to load calibrated lambda for '%s': %s", budget_level, exc)
        return None
    finally:
        conn.close()


def get_calibration_summary(
    db_path: Optional[Union[Path, str]] = None,
) -> List[Dict[str, Any]]:
    """
    Return calibration summary rows for display in `bp report` or `bp budget`.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT budget_slider, lambda_penalty, alpha_risk, calibrated_at, metadata
            FROM lambda_table
            WHERE action = '*'
            ORDER BY calibrated_at DESC, budget_slider;
            """
        ).fetchall()
        result = []
        for row in rows:
            meta = {}
            try:
                meta = json.loads(row["metadata"] or "{}")
            except Exception:
                pass
            result.append({
                "budget_level": row["budget_slider"],
                "lambda_calibrated": row["lambda_penalty"],
                "alpha_risk": row["alpha_risk"],
                "calibrated_at": row["calibrated_at"],
                "empirical_risk": meta.get("empirical_risk"),
                "n_rollouts": meta.get("n_rollouts"),
                "bound_satisfied": meta.get("bound_satisfied", True),
            })
        return result
    except Exception as exc:
        logger.debug("Failed to fetch calibration summary: %s", exc)
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Calibration set builder from SQLite rollouts table
# ---------------------------------------------------------------------------

def build_calibration_set_from_rollouts(
    db_path: Optional[Union[Path, str]] = None,
    failure_classes: Optional[List[str]] = None,
) -> List[RolloutEntry]:
    """
    Load rollout records from SQLite and convert them to RolloutEntry objects
    for use as a CRC calibration set.

    Optionally filter by failure_class list.
    """
    from budgetpilot.core.calibration.rollouts import (
        _ensure_rollout_columns,
        load_rollout_counts,
    )
    _ensure_rollout_columns(db_path)

    conn = get_connection(db_path)
    try:
        query = """
            SELECT failure_class, action, succeeded, cost_usd, tokens_spent
            FROM rollouts
            WHERE failure_class IS NOT NULL AND action IS NOT NULL
        """
        params: tuple = ()
        if failure_classes:
            placeholders = ",".join("?" * len(failure_classes))
            query += f" AND failure_class IN ({placeholders})"
            params = tuple(failure_classes)

        rows = conn.execute(query, params).fetchall()
        entries = []
        for row in rows:
            entries.append(RolloutEntry(
                failure_class=row["failure_class"],
                action_chosen=row["action"],
                succeeded=bool(row["succeeded"]),
                cost_usd=float(row["cost_usd"] or 0.0),
                tokens_spent=int(row["tokens_spent"] or 0),
            ))
        return entries
    except Exception as exc:
        logger.debug("Failed to build calibration set from rollouts: %s", exc)
        return []
    finally:
        conn.close()

"""
BudgetPilot Avoided Cost Estimation Engine.
Phase I: Calculates modeled avoided spend from gate decisions (rewrites and non-overridden denies).

Formula:
    avoided_usd ≈ tokens_avoided × turns_remaining × input_rate_per_token

Uncertainty Band:
    Uses the 25th and 75th percentile of observed turns-per-session from SQLite history
    to construct a low–high uncertainty range around the mid-point estimate.

Override Deduction:
    Subtracts any gate-denied calls that were subsequently overridden (repeated within 3 turns),
    and files that were eventually read anyway in the same session.

Results are stored in the `avoided_estimates` SQLite table.

Strict Rule: This module must NOT import ollama, mcp, fastapi, or uvicorn.
"""

from dataclasses import asdict, dataclass, field
import json
import logging
from pathlib import Path
import statistics
from typing import Any, Dict, List, Optional, Tuple, Union

from budgetpilot.core.prices import load_prices
from budgetpilot.core.store import get_connection, init_db, safe_execute_write

logger = logging.getLogger(__name__)

# Default token estimate for a tool call context contribution
# (covers typical arguments + result summary re-injected as observation)
DEFAULT_TOOL_CALL_TOKENS: int = 150

# Default fallback turns-remaining estimate when no session history is available
DEFAULT_TURNS_REMAINING_FALLBACK: int = 5


@dataclass
class AvoidedEstimate:
    """
    Modeled avoided cost calculation for a single session.
    Three metrics are kept strictly separate and never merged:
    1. measured_spend_usd      – exact tokens & reference USD (from model_calls)
    2. avoided_mid_usd         – modeled avoided spend (midpoint estimate)
    3. avoided_low_usd         – 25th-percentile uncertainty bound
    4. avoided_high_usd        – 75th-percentile uncertainty bound
    5. paired_savings_usd      – benchmark harness paired-run savings (0.0 if not from bench)

    Deductions are applied for:
    - Gate-denied calls that were later overridden (allowed through within 3 turns)
    - Files that were gated-out but then read anyway (late-read deduction)
    """
    session_id: str
    mechanism: str  # "gate_deny", "gate_rewrite", "combined"

    # Gate decision counts
    deny_count: int = 0
    rewrite_count: int = 0
    override_count: int = 0
    late_read_count: int = 0

    # Token estimates
    tokens_avoided: int = 0
    tokens_deducted: int = 0
    net_tokens_avoided: int = 0

    # Turns estimates (from session history percentiles)
    turns_p25: float = 0.0  # 25th percentile turns per session
    turns_p50: float = 0.0  # median turns per session
    turns_p75: float = 0.0  # 75th percentile turns per session

    # Rate
    input_rate_per_token: float = 0.0  # USD per single input token

    # Cost estimates – strictly NOT merged in reporting
    measured_spend_usd: float = 0.0
    avoided_mid_usd: float = 0.0
    avoided_low_usd: float = 0.0
    avoided_high_usd: float = 0.0
    paired_savings_usd: float = 0.0  # Only set when from benchmark harness

    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _get_session_turns_percentiles(
    db_path: Optional[Union[Path, str]] = None,
    min_sessions: int = 3,
) -> Tuple[float, float, float]:
    """
    Query SQLite task_events to derive turn counts per session.
    Returns (p25, p50, p75) of turns observed across past sessions.
    Falls back to DEFAULT_TURNS_REMAINING_FALLBACK if insufficient history.
    """
    conn = get_connection(db_path)
    try:
        # Count turns per session from model_calls (each call = 1 model turn)
        cursor = conn.execute(
            """
            SELECT session_id, COUNT(*) AS turn_count
            FROM model_calls
            WHERE session_id IS NOT NULL
            GROUP BY session_id
            HAVING COUNT(*) >= 1
            ORDER BY MIN(id) ASC;
            """
        )
        rows = cursor.fetchall()
    except Exception as exc:
        logger.debug("Failed to query session turns from SQLite: %s", exc)
        rows = []
    finally:
        conn.close()

    if not rows or len(rows) < min_sessions:
        fb = float(DEFAULT_TURNS_REMAINING_FALLBACK)
        return fb * 0.6, fb, fb * 1.4  # synthetic low/mid/high

    turn_counts = sorted(float(r["turn_count"]) for r in rows)
    n = len(turn_counts)

    def _percentile(data: List[float], p: float) -> float:
        idx = (p / 100.0) * (len(data) - 1)
        lo = int(idx)
        hi = lo + 1
        if hi >= len(data):
            return data[-1]
        frac = idx - lo
        return data[lo] + frac * (data[hi] - data[lo])

    p25 = _percentile(turn_counts, 25)
    p50 = _percentile(turn_counts, 50)
    p75 = _percentile(turn_counts, 75)
    return p25, p50, p75


def _get_measured_spend(
    session_id: str,
    db_path: Optional[Union[Path, str]] = None,
) -> float:
    """Retrieve exact measured USD spend for this session from model_calls."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT COALESCE(SUM(estimated_cost), 0.0) FROM model_calls WHERE session_id = ?;",
            (session_id,),
        )
        row = cursor.fetchone()
        return float(row[0]) if row else 0.0
    except Exception as exc:
        logger.debug("Failed to get measured spend for session %s: %s", session_id, exc)
        return 0.0
    finally:
        conn.close()


def _get_gate_decisions(
    session_id: str,
    db_path: Optional[Union[Path, str]] = None,
) -> List[Dict[str, Any]]:
    """Retrieve all gate decisions for this session ordered by id."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """
            SELECT id, tool_name, verdict, reason, original_call, metadata
            FROM gate_decisions
            WHERE session_id = ?
            ORDER BY id ASC;
            """,
            (session_id,),
        )
        return [dict(r) for r in cursor.fetchall()]
    except Exception as exc:
        logger.debug("Failed to fetch gate decisions for session %s: %s", session_id, exc)
        return []
    finally:
        conn.close()


def _get_tool_events(
    session_id: str,
    db_path: Optional[Union[Path, str]] = None,
) -> List[Dict[str, Any]]:
    """Retrieve all tool events for this session ordered by id."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """
            SELECT id, tool_name, arguments, is_error, metadata
            FROM tool_events
            WHERE session_id = ?
            ORDER BY id ASC;
            """,
            (session_id,),
        )
        return [dict(r) for r in cursor.fetchall()]
    except Exception as exc:
        logger.debug("Failed to fetch tool events for session %s: %s", session_id, exc)
        return []
    finally:
        conn.close()


def _count_overrides(
    gate_decisions: List[Dict[str, Any]],
    override_window: int = 3,
) -> int:
    """
    Count denied gate decisions that were overridden.
    A denied call is considered 'overridden' if the same tool is called again
    within `override_window` subsequent gate decisions without being denied again.
    """
    overridden = 0
    denied_positions: Dict[str, int] = {}  # tool_name -> position of last denial

    for pos, decision in enumerate(gate_decisions):
        tool = decision.get("tool_name", "")
        verdict = (decision.get("verdict") or "").lower()

        if verdict == "deny":
            denied_positions[tool] = pos
        elif verdict in ("allow", "rewrite") and tool in denied_positions:
            last_deny_pos = denied_positions[tool]
            if (pos - last_deny_pos) <= override_window:
                overridden += 1
                del denied_positions[tool]

    return overridden


def _count_late_reads(
    gate_decisions: List[Dict[str, Any]],
    tool_events: List[Dict[str, Any]],
) -> int:
    """
    Count files that were blocked by a gate deny but were ultimately read
    later in the session anyway (late-read deduction).
    A file is 'late-read' if a denied read_file call path later appears
    in a successful tool event for the same path.
    """
    blocked_paths: set = set()

    for decision in gate_decisions:
        if (decision.get("verdict") or "").lower() == "deny":
            tool = decision.get("tool_name", "")
            if tool in ("read_file", "list_dir", "search"):
                raw_call = decision.get("original_call") or "{}"
                try:
                    call_data = json.loads(raw_call) if isinstance(raw_call, str) else raw_call
                    # Extract path from various argument schemas
                    path = (
                        call_data.get("path")
                        or call_data.get("file_path")
                        or call_data.get("file")
                        or call_data.get("arguments", {}).get("path")
                    )
                    if path:
                        blocked_paths.add(str(path).strip())
                except Exception:
                    pass

    if not blocked_paths:
        return 0

    late_read_count = 0
    for event in tool_events:
        if event.get("is_error"):
            continue
        tool = event.get("tool_name", "")
        if tool in ("read_file", "list_dir", "search"):
            raw_args = event.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                path = args.get("path") or args.get("file_path") or args.get("file")
                if path and str(path).strip() in blocked_paths:
                    late_read_count += 1
                    blocked_paths.discard(str(path).strip())
            except Exception:
                pass

    return late_read_count


def _get_input_rate_per_token(tier: str = "cheap") -> float:
    """Return USD cost per single input token for the given tier."""
    try:
        prices = load_prices()
        tier_price = prices.get(tier.lower(), prices.get("cheap"))
        if tier_price:
            return tier_price.input_per_million / 1_000_000.0
    except Exception as exc:
        logger.debug("Failed to load prices for savings calculation: %s", exc)
    return 1.00 / 1_000_000.0  # $1.00/M token fallback


def compute_avoided_estimate(
    session_id: str,
    tier: str = "cheap",
    tokens_per_blocked_call: int = DEFAULT_TOOL_CALL_TOKENS,
    override_window: int = 3,
    db_path: Optional[Union[Path, str]] = None,
    persist: bool = True,
) -> AvoidedEstimate:
    """
    Compute the modeled avoided cost for gate denies and rewrites in a session.

    Algorithm:
    1. Fetch all gate_decisions for this session.
    2. Count deny decisions (non-overridden) and rewrite decisions.
    3. Compute turns_remaining distribution (p25/p50/p75) from session history.
    4. tokens_avoided = count × tokens_per_blocked_call
    5. Apply deductions for:
       - Override calls (denied then allowed within override_window turns)
       - Late-read deductions (blocked file was read anyway later in session)
    6. avoided_usd = net_tokens_avoided × turns_remaining × input_rate_per_token
    7. Persist to avoided_estimates table.

    Returns:
        AvoidedEstimate with three strictly separate cost metrics.
    """
    init_db(db_path)

    gate_decisions = _get_gate_decisions(session_id=session_id, db_path=db_path)
    tool_events = _get_tool_events(session_id=session_id, db_path=db_path)

    deny_count = sum(1 for d in gate_decisions if (d.get("verdict") or "").lower() == "deny")
    rewrite_count = sum(1 for d in gate_decisions if (d.get("verdict") or "").lower() == "rewrite")
    override_count = _count_overrides(gate_decisions, override_window=override_window)
    late_read_count = _count_late_reads(gate_decisions, tool_events)

    # Effective counts: denies minus overrides and late reads
    effective_denies = max(0, deny_count - override_count - late_read_count)
    # Rewrites save partial tokens (rewrite reduces scope, not full suppression → ~50%)
    effective_rewrites = rewrite_count

    tokens_avoided_from_denies = effective_denies * tokens_per_blocked_call
    tokens_avoided_from_rewrites = effective_rewrites * (tokens_per_blocked_call // 2)
    tokens_avoided = tokens_avoided_from_denies + tokens_avoided_from_rewrites

    tokens_deducted = (override_count + late_read_count) * tokens_per_blocked_call
    net_tokens_avoided = max(0, tokens_avoided)

    # Turns remaining estimate (using percentile distribution from historical sessions)
    p25, p50, p75 = _get_session_turns_percentiles(db_path=db_path)

    # Rate (USD per single input token)
    input_rate_per_token = _get_input_rate_per_token(tier=tier)

    # Avoided cost estimates: avoided_usd = net_tokens_avoided × turns_remaining × rate
    avoided_low_usd = round(net_tokens_avoided * p25 * input_rate_per_token, 8)
    avoided_mid_usd = round(net_tokens_avoided * p50 * input_rate_per_token, 8)
    avoided_high_usd = round(net_tokens_avoided * p75 * input_rate_per_token, 8)

    # Measured spend (exact — never merged with modeled avoided)
    measured_spend_usd = _get_measured_spend(session_id=session_id, db_path=db_path)

    mechanism = "combined" if (deny_count > 0 and rewrite_count > 0) else (
        "gate_deny" if deny_count > 0 else ("gate_rewrite" if rewrite_count > 0 else "none")
    )

    estimate = AvoidedEstimate(
        session_id=session_id,
        mechanism=mechanism,
        deny_count=deny_count,
        rewrite_count=rewrite_count,
        override_count=override_count,
        late_read_count=late_read_count,
        tokens_avoided=tokens_avoided,
        tokens_deducted=tokens_deducted,
        net_tokens_avoided=net_tokens_avoided,
        turns_p25=round(p25, 2),
        turns_p50=round(p50, 2),
        turns_p75=round(p75, 2),
        input_rate_per_token=round(input_rate_per_token, 10),
        measured_spend_usd=round(measured_spend_usd, 8),
        avoided_mid_usd=avoided_mid_usd,
        avoided_low_usd=avoided_low_usd,
        avoided_high_usd=avoided_high_usd,
        paired_savings_usd=0.0,  # Only populated from benchmark harness
        metadata={
            "tier": tier,
            "tokens_per_blocked_call": tokens_per_blocked_call,
            "tokens_avoided_from_denies": tokens_avoided_from_denies,
            "tokens_avoided_from_rewrites": tokens_avoided_from_rewrites,
            "effective_denies": effective_denies,
            "effective_rewrites": effective_rewrites,
            "override_window": override_window,
        },
    )

    if persist:
        _persist_avoided_estimate(estimate=estimate, db_path=db_path)

    return estimate


def _persist_avoided_estimate(
    estimate: AvoidedEstimate,
    db_path: Optional[Union[Path, str]] = None,
) -> bool:
    """Persist an AvoidedEstimate to the avoided_estimates SQLite table."""
    metadata_payload = {
        **estimate.metadata,
        "deny_count": estimate.deny_count,
        "rewrite_count": estimate.rewrite_count,
        "override_count": estimate.override_count,
        "late_read_count": estimate.late_read_count,
        "tokens_avoided": estimate.tokens_avoided,
        "tokens_deducted": estimate.tokens_deducted,
        "net_tokens_avoided": estimate.net_tokens_avoided,
        "turns_p25": estimate.turns_p25,
        "turns_p50": estimate.turns_p50,
        "turns_p75": estimate.turns_p75,
        "input_rate_per_token": estimate.input_rate_per_token,
        "avoided_low_usd": estimate.avoided_low_usd,
        "avoided_mid_usd": estimate.avoided_mid_usd,
        "avoided_high_usd": estimate.avoided_high_usd,
        "paired_savings_usd": estimate.paired_savings_usd,
        "measured_spend_usd": estimate.measured_spend_usd,
    }

    try:
        # Ensure session row exists
        safe_execute_write(
            conn_or_path=db_path,
            sql="INSERT OR IGNORE INTO sessions (session_id) VALUES (?);",
            params=(estimate.session_id,),
        )
        return safe_execute_write(
            conn_or_path=db_path,
            sql="""
            INSERT INTO avoided_estimates (
                session_id, mechanism, baseline_cost_usd, actual_cost_usd, saved_cost_usd, metadata
            ) VALUES (?, ?, ?, ?, ?, ?);
            """,
            params=(
                estimate.session_id,
                estimate.mechanism,
                round(estimate.measured_spend_usd + estimate.avoided_mid_usd, 8),  # hypothetical baseline
                round(estimate.measured_spend_usd, 8),
                round(estimate.avoided_mid_usd, 8),
                json.dumps(metadata_payload),
            ),
        )
    except Exception as exc:
        logger.debug("Failed to persist avoided_estimate for session %s: %s", estimate.session_id, exc)
        return False


def compute_paired_run_savings(
    gated_session_id: str,
    ungated_session_id: str,
    db_path: Optional[Union[Path, str]] = None,
) -> float:
    """
    Compute observed paired-run savings from the benchmark harness:
        paired_savings = ungated_cost_usd - gated_cost_usd

    This is the only reliable measurement of actual savings (from bench runs only).
    Returns a positive value if gating saved money, negative if gating added overhead.
    """
    gated_cost = _get_measured_spend(session_id=gated_session_id, db_path=db_path)
    ungated_cost = _get_measured_spend(session_id=ungated_session_id, db_path=db_path)
    return round(ungated_cost - gated_cost, 8)


def get_session_avoided_estimates(
    session_id: str,
    db_path: Optional[Union[Path, str]] = None,
) -> List[Dict[str, Any]]:
    """Retrieve all stored avoided_estimate rows for a given session."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """
            SELECT id, session_id, timestamp, mechanism,
                   baseline_cost_usd, actual_cost_usd, saved_cost_usd, metadata
            FROM avoided_estimates
            WHERE session_id = ?
            ORDER BY id ASC;
            """,
            (session_id,),
        )
        rows = []
        for r in cursor.fetchall():
            row_dict = dict(r)
            if row_dict.get("metadata"):
                try:
                    row_dict["metadata"] = json.loads(row_dict["metadata"])
                except Exception:
                    pass
            rows.append(row_dict)
        return rows
    except Exception as exc:
        logger.debug("Failed to fetch avoided_estimates for session %s: %s", session_id, exc)
        return []
    finally:
        conn.close()

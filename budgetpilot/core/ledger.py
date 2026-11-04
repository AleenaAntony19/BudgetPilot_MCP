"""
BudgetPilot Cost Ledger and Anomaly Detection Subsystem.
Phase A: Token pricing, prompt cache-aware telemetry recording, and pure advisory anomaly detection.

Strict Rule: This module must NOT import ollama, mcp, or fastapi.
"""

from dataclasses import asdict, dataclass, field
import json
import logging
from pathlib import Path
import sqlite3
import statistics
from typing import Any, Dict, List, Optional, Tuple, Union

from budgetpilot.core.prices import price_call
from budgetpilot.core.store import get_connection, safe_execute_write

logger = logging.getLogger(__name__)


@dataclass
class AnomalyMetric:
    """
    Statistical distribution details for an observed metric.
    """
    name: str
    current_value: float
    mean: float
    std_dev: float
    threshold: float
    is_anomaly: bool


@dataclass
class AnomalyReport:
    """
    Pure advisory anomaly detection result.
    Does NOT mutate any generation options or predict limits.
    """
    has_anomaly: bool
    anomaly_count: int
    latency_anomaly: bool
    input_token_anomaly: bool
    output_token_anomaly: bool
    metrics: Dict[str, AnomalyMetric] = field(default_factory=dict)
    status: str = "NORMAL"  # NORMAL, WARNING, ALERT
    message: str = "No unusual behavior detected."

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_anomaly": self.has_anomaly,
            "anomaly_count": self.anomaly_count,
            "latency_anomaly": self.latency_anomaly,
            "input_token_anomaly": self.input_token_anomaly,
            "output_token_anomaly": self.output_token_anomaly,
            "status": self.status,
            "message": self.message,
            "metrics": {k: asdict(v) for k, v in self.metrics.items()},
        }


def estimate_prompt_tokens(prompt: str) -> int:
    """
    Estimate prompt tokens using the standard 4 characters per token heuristic.
    Used to account for Ollama prompt cache re-use where prompt_eval_count might be 0 or small.
    """
    if not prompt:
        return 0
    return max(1, len(prompt) // 4)


def record_model_call(
    model: str,
    prompt: str,
    response: str,
    prompt_eval_tokens: int,
    output_tokens: int,
    tier: str = "cheap",
    session_id: Optional[str] = None,
    total_time: float = 0.0,
    load_time: float = 0.0,
    prompt_time: float = 0.0,
    generation_time: float = 0.0,
    query_size: Optional[str] = None,
    provider: str = "ollama",
    db_path: Optional[Union[Path, str]] = None,
    prices_path: Optional[Union[Path, str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Record an LLM call to SQLite model_calls.
    Prices on the maximum of prompt_eval_tokens or estimated prompt length (chars / 4)
    to accurately account for Ollama prompt cache re-use.
    """
    safe_prompt_eval = max(0, prompt_eval_tokens or 0)
    safe_output_tokens = max(0, output_tokens or 0)

    # Heuristic for prompt cache re-use
    char_estimate = estimate_prompt_tokens(prompt or "")
    billable_input_tokens = max(safe_prompt_eval, char_estimate)
    total_tokens = billable_input_tokens + safe_output_tokens

    # Compute reference cost in USD
    estimated_cost = price_call(
        tier=tier,
        in_tokens=billable_input_tokens,
        out_tokens=safe_output_tokens,
        prices_path=prices_path,
    )

    call_metadata = metadata.copy() if metadata else {}
    call_metadata.update({
        "tier": tier,
        "raw_prompt_eval_tokens": safe_prompt_eval,
        "char_token_estimate": char_estimate,
        "billable_input_tokens": billable_input_tokens,
    })

    if session_id:
        safe_execute_write(
            conn_or_path=db_path,
            sql="INSERT OR IGNORE INTO sessions (session_id) VALUES (?);",
            params=(session_id,),
        )

    insert_sql = """
    INSERT INTO model_calls (
        session_id,
        model,
        provider,
        input_tokens,
        output_tokens,
        total_tokens,
        estimated_cost,
        total_time,
        load_time,
        prompt_time,
        generation_time,
        query_size,
        source,
        prompt,
        response,
        metadata
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """

    params = (
        session_id,
        model,
        provider,
        billable_input_tokens,
        safe_output_tokens,
        total_tokens,
        estimated_cost,
        float(total_time or 0.0),
        float(load_time or 0.0),
        float(prompt_time or 0.0),
        float(generation_time or 0.0),
        query_size,
        "live",
        prompt,
        response,
        json.dumps(call_metadata),
    )

    write_success = safe_execute_write(
        conn_or_path=db_path,
        sql=insert_sql,
        params=params,
    )

    return {
        "success": write_success,
        "billable_input_tokens": billable_input_tokens,
        "raw_prompt_eval_tokens": safe_prompt_eval,
        "output_tokens": safe_output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost": estimated_cost,
        "tier": tier,
    }


def record_tool_event(
    session_id: str,
    tool_name: str,
    arguments: Union[Dict[str, Any], str],
    result: str,
    call_id: Optional[str] = None,
    duration: float = 0.0,
    is_error: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
    db_path: Optional[Union[Path, str]] = None,
) -> bool:
    """
    Record a tool execution event to SQLite tool_events.
    """
    raw_args = json.dumps(arguments) if isinstance(arguments, (dict, list)) else str(arguments or "{}")
    raw_meta = json.dumps(metadata) if metadata else "{}"

    if session_id:
        safe_execute_write(
            conn_or_path=db_path,
            sql="INSERT OR IGNORE INTO sessions (session_id) VALUES (?);",
            params=(session_id,),
        )

    insert_sql = """
    INSERT INTO tool_events (
        session_id, tool_name, arguments, call_id, result, duration, is_error, metadata
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
    """
    return safe_execute_write(
        conn_or_path=db_path,
        sql=insert_sql,
        params=(
            session_id,
            tool_name,
            raw_args,
            call_id,
            result[:2000] if result else "",
            float(duration or 0.0),
            1 if is_error else 0,
            raw_meta,
        ),
    )


def _extract_metric(record: Any, field_name: str, fallback_names: Tuple[str, ...]) -> float:
    """Helper to extract a numeric metric from a dict, sqlite3.Row, or object."""
    if isinstance(record, (dict, sqlite3.Row)):
        val = record.get(field_name) if isinstance(record, dict) else (
            record[field_name] if field_name in record.keys() else None
        )
        if val is None:
            for alt in fallback_names:
                if isinstance(record, dict) and alt in record:
                    val = record[alt]
                    break
                elif isinstance(record, sqlite3.Row) and alt in record.keys():
                    val = record[alt]
                    break
        return float(val) if val is not None else 0.0

    # Attribute-based lookup
    if hasattr(record, field_name):
        val = getattr(record, field_name)
        return float(val) if val is not None else 0.0
    for alt in fallback_names:
        if hasattr(record, alt):
            val = getattr(record, alt)
            return float(val) if val is not None else 0.0
    return 0.0


def detect_anomalies(
    history: List[Any],
    current: Optional[Any] = None,
) -> AnomalyReport:
    """
    Pure advisory anomaly detector evaluating mean + 2σ outliers across:
    - Latency (total_time)
    - Input tokens
    - Output tokens

    CRITICAL INVARIANT:
    This function is strictly advisory. It NEVER mutates num_predict, model options,
    or caller parameters.
    """
    if not history:
        return AnomalyReport(
            has_anomaly=False,
            anomaly_count=0,
            latency_anomaly=False,
            input_token_anomaly=False,
            output_token_anomaly=False,
            status="NORMAL",
            message="No history available for anomaly detection.",
        )

    # Determine reference baseline and the observation under test
    if current is not None:
        baseline_records = list(history)
        eval_record = current
    else:
        if len(history) < 2:
            return AnomalyReport(
                has_anomaly=False,
                anomaly_count=0,
                latency_anomaly=False,
                input_token_anomaly=False,
                output_token_anomaly=False,
                status="NORMAL",
                message="Insufficient baseline history (< 2 samples).",
            )
        baseline_records = history[:-1]
        eval_record = history[-1]

    if len(baseline_records) < 2:
        return AnomalyReport(
            has_anomaly=False,
            anomaly_count=0,
            latency_anomaly=False,
            input_token_anomaly=False,
            output_token_anomaly=False,
            status="NORMAL",
            message="Insufficient baseline history (< 2 samples).",
        )

    # Metric extraction specifications: (canonical_name, fallback_keys)
    metric_specs = [
        ("latency", "total_time", ("duration", "time", "latency")),
        ("input_tokens", "input_tokens", ("prompt_tokens", "in_tokens")),
        ("output_tokens", "output_tokens", ("eval_count", "out_tokens")),
    ]

    metrics: Dict[str, AnomalyMetric] = {}
    is_anomaly_map: Dict[str, bool] = {}

    for label, primary_key, fallbacks in metric_specs:
        baseline_vals = [
            _extract_metric(rec, primary_key, fallbacks) for rec in baseline_records
        ]
        curr_val = _extract_metric(eval_record, primary_key, fallbacks)

        mean_val = statistics.mean(baseline_vals)
        std_val = statistics.stdev(baseline_vals)
        threshold = mean_val + (2.0 * std_val)

        is_outlier = curr_val > threshold if std_val > 0.0 else False

        metrics[label] = AnomalyMetric(
            name=label,
            current_value=curr_val,
            mean=round(mean_val, 4),
            std_dev=round(std_val, 4),
            threshold=round(threshold, 4),
            is_anomaly=is_outlier,
        )
        is_anomaly_map[label] = is_outlier

    latency_flag = is_anomaly_map["latency"]
    input_flag = is_anomaly_map["input_tokens"]
    output_flag = is_anomaly_map["output_tokens"]
    anomaly_count = int(latency_flag) + int(input_flag) + int(output_flag)

    if anomaly_count == 0:
        status = "NORMAL"
        msg = "No unusual behavior detected."
    elif anomaly_count == 1:
        status = "WARNING"
        msg = "1 unusual metric detected."
    else:
        status = "ALERT"
        msg = f"{anomaly_count} unusual metrics detected."

    return AnomalyReport(
        has_anomaly=(anomaly_count > 0),
        anomaly_count=anomaly_count,
        latency_anomaly=latency_flag,
        input_token_anomaly=input_flag,
        output_token_anomaly=output_flag,
        metrics=metrics,
        status=status,
        message=msg,
    )


class CostLedger:
    """
    Persistence and audit ledger for BudgetPilot telemetry.
    """

    def __init__(self, db_path: Optional[Union[Path, str]] = None) -> None:
        self.db_path = db_path

    def record_call(
        self,
        model: str,
        prompt: str,
        response: str,
        prompt_eval_tokens: int,
        output_tokens: int,
        tier: str = "cheap",
        session_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        return record_model_call(
            model=model,
            prompt=prompt,
            response=response,
            prompt_eval_tokens=prompt_eval_tokens,
            output_tokens=output_tokens,
            tier=tier,
            session_id=session_id,
            db_path=self.db_path,
            **kwargs,
        )

    def record_tool(
        self,
        session_id: str,
        tool_name: str,
        arguments: Union[Dict[str, Any], str],
        result: str,
        **kwargs: Any,
    ) -> bool:
        """Record a tool execution event to SQLite tool_events."""
        return record_tool_event(
            session_id=session_id,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            db_path=self.db_path,
            **kwargs,
        )

    def record_event(self, event: Any) -> None:
        """
        Record a BpEvent to the persistent audit log.

        Raises:
            NotImplementedError: Scheduled for implementation in Phase C.
        """
        raise NotImplementedError("CostLedger.record_event is scheduled for Phase C implementation.")

    def get_session_cost(self, session_id: str) -> float:
        """Calculate total recorded USD cost for a given session."""
        conn = get_connection(self.db_path)
        try:
            cursor = conn.execute(
                "SELECT COALESCE(SUM(estimated_cost), 0.0) FROM model_calls WHERE session_id = ?;",
                (session_id,),
            )
            row = cursor.fetchone()
            return float(row[0]) if row else 0.0
        finally:
            conn.close()

    def get_session_history(self, session_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieve recent model calls for a session or globally."""
        conn = get_connection(self.db_path)
        try:
            if session_id:
                cursor = conn.execute(
                    """
                    SELECT id, session_id, model, input_tokens, output_tokens, total_tokens,
                           estimated_cost, total_time, load_time, prompt_time, generation_time
                    FROM model_calls WHERE session_id = ? ORDER BY id ASC LIMIT ?;
                    """,
                    (session_id, limit),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT id, session_id, model, input_tokens, output_tokens, total_tokens,
                           estimated_cost, total_time, load_time, prompt_time, generation_time
                    FROM model_calls ORDER BY id ASC LIMIT ?;
                    """,
                    (limit,),
                )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

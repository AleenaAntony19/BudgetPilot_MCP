"""
BudgetPilot Core - Backwards Compatibility Layer.
Routes through the modular budgetpilot.agent.model and budgetpilot.core.ledger engines.
"""

from typing import Any, Dict
from budgetpilot.agent.model import chat
from budgetpilot.core.ledger import CostLedger, detect_anomalies, record_model_call
from budgetpilot.core.store import init_db

DB_NAME = "budgetpilot.db"
MODEL_NAME = "qwen2.5:3b"
INPUT_PRICE_PER_MILLION = 1.00
OUTPUT_PRICE_PER_MILLION = 2.00


def ask_model(prompt: str) -> Dict[str, Any]:
    """
    Backwards-compatible wrapper calling agent/model.py and core/ledger.py.
    """
    init_db()
    ledger = CostLedger()
    history = ledger.get_session_history(limit=50)

    # 1. Invoke model via agent wrapper
    result = chat(
        messages=[{"role": "user", "content": prompt}],
        tier="cheap",
    )

    # 2. Record to ledger (accounts for prompt cache re-use)
    record = record_model_call(
        model=result.model,
        prompt=prompt,
        response=result.content,
        prompt_eval_tokens=result.prompt_tokens,
        output_tokens=result.output_tokens,
        tier=result.tier,
        total_time=result.total_time,
        load_time=result.load_time,
        prompt_time=result.prompt_time,
        generation_time=result.generation_time,
    )

    # 3. Detect anomalies via pure advisory detector (mean + 2σ)
    report = detect_anomalies(
        history=history,
        current={
            "total_time": result.total_time,
            "input_tokens": result.prompt_tokens,
            "output_tokens": result.output_tokens,
        },
    )

    return {
        "model": result.model,
        "response": result.content,
        "input_tokens": record["billable_input_tokens"],
        "output_tokens": result.output_tokens,
        "total_tokens": record["total_tokens"],
        "estimated_cost": record["estimated_cost"],
        "total_time": result.total_time,
        "load_time": result.load_time,
        "prompt_time": result.prompt_time,
        "generation_time": result.generation_time,
        "status": report.status,
        "latency_anomaly": report.latency_anomaly,
        "input_anomaly": report.input_token_anomaly,
        "output_anomaly": report.output_token_anomaly,
        "action": "NO_ACTION",
        "output_limit": None,
    }
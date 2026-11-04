"""
BudgetPilot FastAPI Service (Compatibility Module).
Uses budgetpilot.core.store and shared ledger utilities.
"""

from typing import Any, Dict
from fastapi import FastAPI
from pydantic import BaseModel

from budgetpilot.core.store import get_connection, init_db
from budgetpilot_core import ask_model

app = FastAPI(title="BudgetPilot API")


class PromptRequest(BaseModel):
    prompt: str


@app.get("/")
def home() -> Dict[str, str]:
    return {
        "project": "BudgetPilot",
        "status": "running",
    }


@app.get("/health")
def health() -> Dict[str, str]:
    return {
        "status": "healthy",
    }


@app.get("/budgetpilot/status")
def budgetpilot_status() -> Dict[str, Any]:
    init_db()
    conn = get_connection()
    try:
        cursor = conn.execute("""
            SELECT
                COUNT(*),
                COALESCE(AVG(input_tokens), 0.0),
                COALESCE(AVG(output_tokens), 0.0),
                COALESCE(AVG(total_time), 0.0),
                COALESCE(SUM(estimated_cost), 0.0)
            FROM model_calls;
        """)
        row = cursor.fetchone()
        return {
            "model": "qwen2.5:3b",
            "total_interactions": int(row[0]) if row else 0,
            "average_input_tokens": round(float(row[1]), 2) if row else 0.0,
            "average_output_tokens": round(float(row[2]), 2) if row else 0.0,
            "average_response_time": round(float(row[3]), 2) if row else 0.0,
            "total_estimated_cost": round(float(row[4]), 6) if row else 0.0,
        }
    finally:
        conn.close()


@app.post("/budgetpilot/ask")
def ask_budgetpilot(request: PromptRequest) -> Dict[str, Any]:
    return ask_model(request.prompt)
"""
BudgetPilot MCP Server (Compatibility Module).
Uses budgetpilot.core.store and shared ledger utilities.
"""

from typing import Any, Dict
from mcp.server import MCPServer
from budgetpilot.core.store import get_connection, init_db
from budgetpilot_core import ask_model

mcp = MCPServer("BudgetPilot")


@mcp.tool()
def budgetpilot_ask(prompt: str) -> str:
    """
    Ask BudgetPilot to process a prompt using the AI model with telemetry and pricing.
    """
    result = ask_model(prompt)
    return str(result)


@mcp.tool()
def budgetpilot_status() -> Dict[str, Any]:
    """
    Get BudgetPilot usage and cost statistics from the unified database.
    """
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


if __name__ == "__main__":
    mcp.run()
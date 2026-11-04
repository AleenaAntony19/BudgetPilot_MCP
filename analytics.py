"""
BudgetPilot Analytics Utility (Compatibility Module).
Queries aggregated telemetry from the unified store.
"""

from budgetpilot.core.store import get_connection, init_db

init_db()
connection = get_connection()
cursor = connection.cursor()

# Total interactions
cursor.execute("SELECT COUNT(*) FROM model_calls;")
interaction_count = cursor.fetchone()[0] or 0

# Average input tokens
cursor.execute("SELECT COALESCE(AVG(input_tokens), 0.0) FROM model_calls;")
average_input_tokens = cursor.fetchone()[0] or 0.0

# Average output tokens
cursor.execute("SELECT COALESCE(AVG(output_tokens), 0.0) FROM model_calls;")
average_output_tokens = cursor.fetchone()[0] or 0.0

# Total estimated cost
cursor.execute("SELECT COALESCE(SUM(estimated_cost), 0.0) FROM model_calls;")
total_cost = cursor.fetchone()[0] or 0.0

# Average total time
cursor.execute("SELECT COALESCE(AVG(total_time), 0.0) FROM model_calls;")
average_time = cursor.fetchone()[0] or 0.0

print("===== BudgetPilot Analytics =====")
print()
print("Total interactions:", interaction_count)
print("Average input tokens:", round(average_input_tokens, 2))
print("Average output tokens:", round(average_output_tokens, 2))
print("Total estimated cost: $", round(total_cost, 6))
print("Average response time:", round(average_time, 2), "seconds")

# Most expensive interaction
cursor.execute("""
SELECT id, model, total_tokens, estimated_cost, total_time
FROM model_calls
ORDER BY estimated_cost DESC
LIMIT 1;
""")
most_expensive = cursor.fetchone()

if most_expensive:
    print()
    print("Most expensive interaction:")
    print("ID:", most_expensive[0])
    print("Model:", most_expensive[1])
    print("Total tokens:", most_expensive[2])
    print("Estimated cost: $", round(most_expensive[3], 6))
    print("Response time:", round(most_expensive[4], 2), "seconds")

connection.close()

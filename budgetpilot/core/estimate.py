"""
BudgetPilot Scope Estimator.
Implementation Phase: Phase B (Week 2).

Lightweight lexical and structural probe predicting task scope (1: small, 2: medium, 3: large)
before an agent over-reads repository context.

Strict Rule: This module must NOT import ollama, mcp, or fastapi.
"""

from typing import Any, Optional
from budgetpilot.core.types import ScopePrediction


class ScopeEstimator:
    """
    Predicts task complexity and read/edit scope requirements.
    Scheduled for implementation in Phase B.
    """

    def predict(self, prompt: str, repo_path: Optional[str] = None) -> ScopePrediction:
        """
        Predict task scope and file budget from the user prompt and repository layout.

        Raises:
            NotImplementedError: Scheduled for implementation in Phase B.
        """
        raise NotImplementedError("ScopeEstimator.predict is scheduled for Phase B implementation.")

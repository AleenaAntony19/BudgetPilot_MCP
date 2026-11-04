"""
BudgetPilot Verification Hook.
Implementation Phase: Phase B (Week 2).

Captures pass/fail status, exit codes, and stderr from test executions
to classify failures and construct RecoveryContext.

Strict Rule: This module must NOT import ollama, mcp, or fastapi.
"""

from typing import Any, List, Optional
from budgetpilot.core.types import FailureClass, RecoveryContext


class VerificationHook:
    """
    Executes and inspects verification runs after agent edits.
    Scheduled for implementation in Phase B.
    """

    def verify(
        self,
        test_command: str,
        files_touched: Optional[List[str]] = None,
        timeout: int = 60,
    ) -> Optional[RecoveryContext]:
        """
        Run verification command and return RecoveryContext if verification fails.

        Raises:
            NotImplementedError: Scheduled for implementation in Phase B.
        """
        raise NotImplementedError("VerificationHook.verify is scheduled for Phase B implementation.")

    def classify_failure(self, exit_code: int, stdout: str, stderr: str) -> FailureClass:
        """
        Parse outputs and categorize into a canonical FailureClass.

        Raises:
            NotImplementedError: Scheduled for implementation in Phase B.
        """
        raise NotImplementedError("VerificationHook.classify_failure is scheduled for Phase B implementation.")

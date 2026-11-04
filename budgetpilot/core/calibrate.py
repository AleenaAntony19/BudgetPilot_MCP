"""
BudgetPilot CRC Calibrator — Legacy Compatibility Module.
Implementation Phase: Phase K (completed).

Conformal Risk Control calibration engine mapping discrete budget-slider positions
(tight, balanced, loose) to cost penalty multiplier lambda tables.

Primary implementation is in `budgetpilot.core.calibration.crc`.
This module preserves backward compatibility with test_types.py stub checks.

Strict Rule: This module must NOT import ollama, mcp, fastapi, or uvicorn.
"""

from typing import Any, Dict, List, Optional


class CRCCalibrator:
    """
    Offline calibration pass for budget parameterization.
    Delegates to budgetpilot.core.calibration.crc.calibrate() for implementation.
    """

    def calibrate(self, rollout_dataset_path: str, alpha_risk: float = 0.1) -> Dict[str, float]:
        """
        Compute conformal lambda penalties from failure rollout data.

        Raises:
            NotImplementedError: Use budgetpilot.core.calibration.crc.calibrate() directly.
        """
        raise NotImplementedError(
            "CRCCalibrator.calibrate() is deprecated. "
            "Use budgetpilot.core.calibration.crc.calibrate() directly."
        )

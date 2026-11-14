#!/usr/bin/env python3
"""
Standalone entry point for the hook payload capture.

Usable without installing BudgetPilot: it loads budgetpilot/capture_hook.py from
this checkout by path. `bp capture on` registers the installed module instead.
"""

import importlib.util
from pathlib import Path
import sys

_SOURCE = Path(__file__).resolve().parent.parent / "budgetpilot" / "capture_hook.py"


def main() -> int:
    try:
        spec = importlib.util.spec_from_file_location("bp_capture_hook", _SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.main()
    except BaseException:
        return 0


if __name__ == "__main__":
    sys.exit(main())

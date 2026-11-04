"""
BudgetPilot Main Launcher (Legacy Entry Point).
Delegates to the unified BudgetPilot CLI chat interface.
"""

import sys
from budgetpilot.cli import main

if __name__ == "__main__":
    sys.exit(main(["chat"] + sys.argv[1:]))
"""
Entry point Claude Code runs for every registered hook event:

    python -m budgetpilot.adapters.hooks      (or the `budgetpilot-hook` script)

It reads one JSON payload on stdin and prints either nothing or one JSON
object. It always exits 0: on any error Claude Code proceeds as if BudgetPilot
were not installed (fail open).

This module is deliberately tiny and imports only `os` and `sys` at the top.
When the mode is `off` (the default) it returns before loading anything else,
because its start-up time is paid on every tool call. The real work is in
`budgetpilot.adapters.hook_engine`.
"""

import os
import sys

_MODES = ("enforce", "observe", "off")


def _quick_mode():
    """
    The mode without importing json: `BP_MODE`, else the "mode" value in
    config.json found by plain string search. Returns None when unsure, and the
    caller then takes the full path.
    """
    env = os.environ.get("BP_MODE", "").strip().lower()
    if env in _MODES:
        return env
    home = os.environ.get("BP_HOME") or os.path.join(os.path.expanduser("~"), ".budgetpilot")
    try:
        with open(os.path.join(os.path.expanduser(home), "config.json"), "rb") as f:
            data = f.read(65536)
    except FileNotFoundError:
        return "off"
    except OSError:
        return None
    key = data.find(b'"mode"')
    if key < 0:
        return "off" if data.strip().startswith(b"{") else None
    start = data.find(b'"', data.find(b":", key) + 1)
    end = data.find(b'"', start + 1)
    if start < 0 or end < 0:
        return None
    value = data[start + 1:end].decode("ascii", "replace").strip().lower()
    return value if value in _MODES else None


def main() -> int:
    """Always returns 0. Prints nothing, or exactly one JSON object."""
    try:
        raw = sys.stdin.buffer.read()
        # Off: only a UserPromptSubmit can matter (it may be a bp_* command).
        if _quick_mode() == "off" and b"UserPromptSubmit" not in raw:
            return 0
        import budgetpilot.adapters.hook_engine as hook_engine

        hook_engine.run(raw.decode("utf-8", errors="replace"))
    except BaseException as exc:  # fail open: never disturb the host session
        try:
            from budgetpilot.core import hookdb

            hookdb.log_error("hook", exc)
        except BaseException:
            pass
    return 0


def __getattr__(name: str):
    """`handle`, `trim_text` and the handlers live in hook_engine."""
    if name.startswith("__") or name == "hook_engine":
        raise AttributeError(name)
    import budgetpilot.adapters.hook_engine as hook_engine

    try:
        return getattr(hook_engine, name)
    except AttributeError:
        raise AttributeError(f"module 'budgetpilot.adapters.hooks' has no attribute '{name}'") from None


if __name__ == "__main__":
    sys.exit(main())

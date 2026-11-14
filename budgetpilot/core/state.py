"""
BudgetPilot runtime state: home directory, mode, budget dial, human-in-the-loop.

Single source of truth for the mode. The hook process, the MCP server and the
CLI all read it through `get_mode()`.

Standard library only: this module is imported by the hook process on every
tool call, so it must stay cheap (no pydantic, no SQLite).

Precedence: environment (`BP_MODE`, `BP_BUDGET_SLIDER`, `BP_HITL`) over
`$BP_HOME/config.json`. The mode is global across projects.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

MODES = ("enforce", "observe", "off")
SLIDERS = ("tight", "balanced", "loose")
DEFAULT_MODE = "off"
DEFAULT_SLIDER = "balanced"

# User-facing names for the internal modes.
MODE_ALIASES = {"on": "enforce", "enforce": "enforce", "observe": "observe", "off": "off"}


def get_bp_home(create: bool = True) -> Path:
    """`$BP_HOME`, else `~/.budgetpilot`."""
    env_home = os.environ.get("BP_HOME")
    home = Path(env_home).expanduser() if env_home else Path.home() / ".budgetpilot"
    home = home.resolve()
    if create:
        home.mkdir(parents=True, exist_ok=True)
    return home


def config_path() -> Path:
    return get_bp_home() / "config.json"


def read_config() -> Dict[str, Any]:
    """Raw config.json as a dict. Missing or unreadable file gives {}."""
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_config(data: Dict[str, Any]) -> None:
    """Atomic write (temp file + os.replace) so a concurrent reader never sees half a file."""
    path = config_path()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def update_config(**changes: Any) -> Dict[str, Any]:
    data = read_config()
    data.update(changes)
    write_config(data)
    return data


def get_mode(config: Optional[Dict[str, Any]] = None) -> str:
    """Current mode: `enforce`, `observe` or `off`. Defaults to `off`."""
    env = os.environ.get("BP_MODE", "").strip().lower()
    if env in MODES:
        return env
    data = read_config() if config is None else config
    mode = str(data.get("mode", DEFAULT_MODE)).strip().lower()
    return mode if mode in MODES else DEFAULT_MODE


def set_mode(mode: str) -> str:
    """Persist the mode. Accepts `on` as an alias of `enforce`."""
    resolved = MODE_ALIASES.get(str(mode).strip().lower())
    if resolved is None:
        raise ValueError(f"Unknown mode '{mode}'. Use on, off or observe.")
    update_config(mode=resolved)
    return resolved


def get_budget_slider(config: Optional[Dict[str, Any]] = None) -> str:
    env = os.environ.get("BP_BUDGET_SLIDER", "").strip().lower()
    if env in SLIDERS:
        return env
    data = read_config() if config is None else config
    slider = str(data.get("budget_slider", DEFAULT_SLIDER)).strip().lower()
    return slider if slider in SLIDERS else DEFAULT_SLIDER


def set_budget_slider(slider: str) -> str:
    resolved = str(slider).strip().lower()
    if resolved not in SLIDERS:
        raise ValueError(f"Unknown budget '{slider}'. Use tight, balanced or loose.")
    update_config(budget_slider=resolved)
    return resolved


def hitl_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """Human-in-the-loop recovery. `BP_HITL=0` (headless, CI, benchmarks) turns it off."""
    env = os.environ.get("BP_HITL", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    data = read_config() if config is None else config
    return bool(data.get("hitl", True))


def env_overrides_mode() -> bool:
    """True when `BP_MODE` pins the mode, so `bp_mode_*` cannot change it."""
    return os.environ.get("BP_MODE", "").strip().lower() in MODES

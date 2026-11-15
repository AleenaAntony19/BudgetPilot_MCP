"""
Claude Code settings integration: register and remove BudgetPilot hook commands.

Standard library only. Every change is a merge: entries that do not belong to
BudgetPilot are never modified, and the settings file is backed up before it is
rewritten.
"""

import json
import os
from pathlib import Path
import shlex
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

HOOK_MODULE = "budgetpilot.adapters.hooks"
CAPTURE_MODULE = "budgetpilot.capture_hook"

# Events the BudgetPilot hook engine handles: (event, matcher or None, timeout seconds).
HOOK_EVENTS: List[Tuple[str, Optional[str], int]] = [
    ("SessionStart", None, 10),
    ("UserPromptSubmit", None, 10),
    ("PreToolUse", "Read|Grep|Glob|Bash|Edit|MultiEdit|Write", 10),
    ("PostToolUse", "Bash|Read|Grep|Glob|Edit|MultiEdit|Write", 10),
    ("PostToolUseFailure", "Bash", 10),
    ("Stop", None, 30),
    ("SessionEnd", None, 30),
]

# The capture tool records every tool on every event.
CAPTURE_EVENTS: List[Tuple[str, Optional[str], int]] = [
    ("SessionStart", None, 10),
    ("UserPromptSubmit", None, 10),
    ("PreToolUse", "*", 10),
    ("PostToolUse", "*", 10),
    ("PostToolUseFailure", "*", 10),
    ("Stop", None, 10),
    ("SessionEnd", None, 10),
]


def claude_config_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def settings_path(scope: str = "user", repo: Optional[Path] = None) -> Path:
    """user -> ~/.claude/settings.json; project -> <repo>/.claude/settings.local.json."""
    if scope == "user":
        return claude_config_dir() / "settings.json"
    if scope == "project":
        root = Path(repo).expanduser().resolve() if repo else Path.cwd().resolve()
        return root / ".claude" / "settings.local.json"
    raise ValueError(f"Unknown scope '{scope}' (expected 'user' or 'project')")


def quote_arg(value: str) -> str:
    if os.name == "nt":
        return f'"{value}"'
    return shlex.quote(value)


def hook_command(module: str, python: Optional[str] = None) -> str:
    """`<absolute interpreter> -m <module>`, quoted so a venv path with spaces works."""
    return f"{quote_arg(python or sys.executable)} -m {module}"


def build_hook_groups(
    events: List[Tuple[str, Optional[str], int]], command: str
) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for event, matcher, timeout in events:
        group: Dict[str, Any] = {}
        if matcher is not None:
            group["matcher"] = matcher
        group["hooks"] = [{"type": "command", "command": command, "timeout": timeout}]
        groups[event] = [group]
    return groups


def load_settings(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def write_settings(path: Path, data: Dict[str, Any], backup: bool = True) -> Optional[Path]:
    """Atomic write. Returns the backup path if an existing file was backed up."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if backup and path.exists():
        backup_path = path.with_name(f"{path.name}.bp-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        backup_path.write_bytes(path.read_bytes())
    tmp = path.with_name(path.name + ".bp-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup_path


def _is_ours(hook: Any, marker: str) -> bool:
    return isinstance(hook, dict) and marker in str(hook.get("command", ""))


def remove_hooks(settings: Dict[str, Any], marker: str) -> int:
    """Remove every hook command containing `marker`. Returns how many were removed."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)
                continue
            kept = [h for h in group["hooks"] if not _is_ours(h, marker)]
            removed += len(group["hooks"]) - len(kept)
            if kept:
                kept_groups.append({**group, "hooks": kept})
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return removed


def merge_hooks(
    settings: Dict[str, Any], groups: Dict[str, List[Dict[str, Any]]], marker: str
) -> Dict[str, Any]:
    """Replace our own entries (identified by `marker`) and leave all others alone."""
    remove_hooks(settings, marker)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings 'hooks' is not an object")
    for event, new_groups in groups.items():
        existing = hooks.get(event)
        if not isinstance(existing, list):
            existing = []
        hooks[event] = existing + new_groups
    return settings


def count_hooks(settings: Dict[str, Any], marker: str) -> Dict[str, int]:
    """Registered commands containing `marker`, per event."""
    found: Dict[str, int] = {}
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return found
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        n = sum(
            1
            for g in groups
            if isinstance(g, dict)
            for h in (g.get("hooks") or [])
            if _is_ours(h, marker)
        )
        if n:
            found[event] = n
    return found


def register(
    module: str,
    events: List[Tuple[str, Optional[str], int]],
    scope: str = "user",
    repo: Optional[Path] = None,
    python: Optional[str] = None,
) -> Tuple[Path, Optional[Path]]:
    path = settings_path(scope, repo)
    settings = load_settings(path)
    merge_hooks(settings, build_hook_groups(events, hook_command(module, python)), module)
    backup = write_settings(path, settings)
    return path, backup


def unregister(module: str, scope: str = "user", repo: Optional[Path] = None) -> Tuple[Path, int]:
    path = settings_path(scope, repo)
    if not path.exists():
        return path, 0
    settings = load_settings(path)
    removed = remove_hooks(settings, module)
    if removed:
        write_settings(path, settings)
    return path, removed

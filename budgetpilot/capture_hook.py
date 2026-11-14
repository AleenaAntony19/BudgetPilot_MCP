"""
Hook payload capture (diagnostic).

Registered by `bp capture on`. Claude Code runs this for every hook event; it
saves the raw stdin payload to `$BP_HOME/captures/` so the real hook contract can
be checked against what Claude Code actually sends. It prints nothing and always
exits 0, so it can never block or alter a session.

Standard library only.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

# Values are recorded only for these; other CLAUDE_* variables are listed by name
# because some carry session tokens.
_SAFE_ENV_VALUES = ("CLAUDE_PROJECT_DIR", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_REMOTE")


def _captures_dir() -> Path:
    home = os.environ.get("BP_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".budgetpilot"
    path = base / "captures"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.]+", "_", str(value or "none"))[:60] or "none"


def capture(raw: str) -> Path:
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    is_dict = isinstance(payload, dict)
    event = payload.get("hook_event_name") if is_dict else "unparsed"
    tool = payload.get("tool_name") if is_dict else None

    env = {}
    for name in sorted(os.environ):
        if name.startswith("BP_") or name in _SAFE_ENV_VALUES:
            env[name] = os.environ[name]
        elif name.startswith("CLAUDE"):
            env[name] = "<set>"
    record = {
        "payload": payload if payload is not None else None,
        "raw": None if payload is not None else raw[:20000],
        "argv": sys.argv,
        "env": env,
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target = _captures_dir() / f"{stamp}-{_slug(event)}-{_slug(tool)}-{os.getpid()}.json"
    target.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def main() -> int:
    try:
        capture(sys.stdin.read())
    except BaseException:  # never disturb the host session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

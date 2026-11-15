#!/usr/bin/env python3
"""
Release gate: every hook fixture must come from a real captured payload.

Fails (exit 1) if any fixture in tests/fixtures/hooks is marked `_unverified`,
is not valid JSON, or has no `payload.hook_event_name`.
"""

import json
from pathlib import Path
import sys

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "hooks"
REQUIRED = [
    "SessionStart", "UserPromptSubmit", "PreToolUse__Read__no_limit", "PreToolUse__Grep",
    "PreToolUse__Glob", "PreToolUse__Bash__pytest_target", "PreToolUse__Edit", "PreToolUse__Write",
    "PostToolUse__Bash__huge_output", "PostToolUse__Read__no_limit",
    "PostToolUseFailure__Bash__pytest_fail", "Stop", "SessionEnd",
]


def main() -> int:
    problems = []
    names = set()
    for path in sorted(FIXTURES.glob("*.json")):
        names.add(path.stem)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f"{path.name}: invalid JSON ({exc})")
            continue
        meta = data.get("_meta") or {}
        if data.get("_unverified") or meta.get("_unverified"):
            problems.append(f"{path.name}: still marked _unverified (replace with a real capture)")
        if not isinstance(data.get("payload"), dict) or not data["payload"].get("hook_event_name"):
            problems.append(f"{path.name}: missing payload.hook_event_name")
    for name in REQUIRED:
        if name not in names:
            problems.append(f"missing required fixture: {name}.json")
    if problems:
        print("Fixture check FAILED:")
        for p in problems:
            print("  -", p)
        return 1
    print(f"Fixture check passed: {len(names)} fixtures, all from real captures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Turn raw hook captures (`bp capture on`) into scrubbed golden fixtures.

    python scripts/import_captures.py ~/.budgetpilot/captures tests/fixtures/hooks --source "claude-code 2.1.325 cli"

Each fixture is {"_meta": {...}, "payload": {...}}. Paths are rewritten to
/home/user/project and /home/user so no local path or user name is committed.
Existing fixtures are not overwritten unless --force is given.
"""

import argparse
import json
from pathlib import Path
import re
import sys

PROJECT = "/home/user/project"
HOME = "/home/user"


def variant(payload: dict) -> str:
    tool = payload.get("tool_name")
    ti = payload.get("tool_input") or {}
    if tool == "Read":
        return "with_limit" if "limit" in ti else "no_limit"
    if tool == "Grep":
        return "head_limit" if "head_limit" in ti else ""
    if tool == "Bash":
        cmd = str(ti.get("command", ""))
        if "pytest" in cmd:
            if payload.get("hook_event_name") == "PostToolUseFailure":
                return "pytest_fail"
            target = re.search(r"pytest\s+(\S+)", cmd)
            if not target or target.group(1).startswith("-"):
                return "pytest_full_suite"
            return "pytest_target"
        response = payload.get("tool_response") or {}
        if isinstance(response, dict) and len(str(response.get("stdout", ""))) > 6000:
            return "huge_output"
        return "other"
    return ""


def scrub(value, cwd: str, home: str):
    if isinstance(value, dict):
        return {k: scrub(v, cwd, home) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, cwd, home) for v in value]
    if isinstance(value, str):
        if cwd:
            value = value.replace(cwd, PROJECT)
            value = value.replace(re.sub(r"[^A-Za-z0-9]", "-", cwd), "-home-user-project")
        if home:
            value = value.replace(home, HOME)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("captures")
    parser.add_argument("fixtures")
    parser.add_argument("--source", required=True, help='e.g. "claude-code 2.1.325 cli (headless)"')
    parser.add_argument("--suffix", default="", help="appended to fixture names, e.g. _antigravity")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    out = Path(args.fixtures)
    out.mkdir(parents=True, exist_ok=True)
    home = str(Path.home())
    written = 0
    for path in sorted(Path(args.captures).glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        cwd = str(payload.get("cwd") or "")
        payload = scrub(payload, cwd, home)
        parts = [payload.get("hook_event_name", "Unknown")]
        if payload.get("tool_name"):
            parts.append(payload["tool_name"])
        v = variant(payload)
        name = "__".join(parts) + (f"__{v}" if v else "") + args.suffix + ".json"
        target = out / name
        if target.exists() and not args.force:
            continue
        fixture = {"_meta": {"source": args.source, "captured_from": "real session"}, "payload": payload}
        target.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        written += 1
    print(f"{written} fixture(s) written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

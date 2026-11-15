"""Helpers for hook tests: a scratch repository and a real hook process."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

FIXTURES = Path(__file__).parent / "fixtures" / "hooks"
TRANSCRIPT = Path(__file__).parent / "fixtures" / "transcripts" / "claude_code_2.1.325_haiku.jsonl"
PLACEHOLDER = "/home/user/project"


def make_repo(root: Path) -> Path:
    """The repository the fixtures were captured in (pkg/big.py has 2,700 lines)."""
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "calc.py").write_text("def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n")
    (root / "pkg" / "big.py").write_text("\n".join(f"def f{i}():\n    return {i}\n" for i in range(900)))
    for name in ("util_a", "util_b", "util_c", "util_d"):
        (root / "pkg" / f"{name}.py").write_text(f"def {name}():\n    return 1\n")
    (root / "tests" / "__init__.py").write_text("")
    (root / "tests" / "test_mul.py").write_text("from pkg.calc import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n")
    (root / "tests" / "test_add.py").write_text("from pkg.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    (root / ".env").write_text("\n".join(f"KEY_{i}=value" for i in range(900)))
    return root


def fixture(name: str, repo: Path, **overrides) -> dict:
    """A golden fixture payload with its paths pointed at `repo`."""
    raw = json.loads((FIXTURES / f"{name}.json").read_text())["payload"]
    payload = json.loads(json.dumps(raw).replace(PLACEHOLDER, str(repo)))
    # The fixtures come from several captured sessions; tests treat them as one.
    payload["session_id"] = "session-1"
    if "prompt_id" in payload:
        payload["prompt_id"] = "prompt-1"
    payload.update(overrides)
    return payload


def tool_payload(repo: Path, event: str, tool: str, tool_input: dict, use_id: str = "toolu_test", **extra) -> dict:
    base = fixture("PreToolUse__Read__no_limit", repo)
    base.update(hook_event_name=event, tool_name=tool, tool_input=tool_input, tool_use_id=use_id)
    base.update(extra)
    return base


def run_hook(payload, bp_home: Path, mode: str = "enforce", hitl: str = "0", raw: bytes = None, env_extra=None):
    """Run the real hook process. Returns (returncode, stdout text, parsed JSON or None)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BP_", "CLAUDE"))}
    env.update(BP_HOME=str(bp_home), BP_HITL=hitl)
    if mode is not None:
        env["BP_MODE"] = mode
    env.update(env_extra or {})
    data = raw if raw is not None else json.dumps(payload).encode("utf-8")
    proc = subprocess.run([sys.executable, "-m", "budgetpilot.adapters.hooks"], input=data, capture_output=True, env=env)
    out = proc.stdout.decode("utf-8", errors="replace")
    parsed = json.loads(out) if out.strip() else None
    return proc.returncode, out, parsed


def start_task(repo: Path, bp_home: Path, prompt: str = "Fix the failing addition test.", mode: str = "enforce",
               prompt_id: str = None, **kw):
    payload = fixture("UserPromptSubmit", repo, prompt=prompt)
    if prompt_id:
        payload["prompt_id"] = prompt_id
    return run_hook(payload, bp_home, mode=mode, **kw)


def db(bp_home: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(bp_home / "bp.db")
    conn.row_factory = sqlite3.Row
    return conn


def task_row(bp_home: Path) -> dict:
    conn = db(bp_home)
    try:
        row = dict(conn.execute("SELECT * FROM tasks_bp ORDER BY ts DESC LIMIT 1").fetchone())
        row["state"] = json.loads(row["state"])
        return row
    finally:
        conn.close()


def set_task(bp_home: Path, **columns) -> None:
    conn = db(bp_home)
    try:
        for name, value in columns.items():
            conn.execute(f"UPDATE tasks_bp SET {name}=?", (value,))
        conn.commit()
    finally:
        conn.close()


def pre(output: dict) -> dict:
    return output["hookSpecificOutput"]

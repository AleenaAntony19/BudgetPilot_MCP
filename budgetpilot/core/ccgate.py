"""
Execution gate for Claude Code tool calls (PreToolUse).

`decide()` takes the hook payload, the task row and the scope limits, and
returns one Decision: allow (say nothing), rewrite (allow with a narrower
input), deny, ask, or context (add a note without deciding). It only mutates
`task["state"]`; the caller persists it. No I/O other than reading the target
file's size, so the same payload and state always give the same answer.

Safety rules that hold for every decision:
- `rewrite` auto-approves the call, so it is only ever returned for the
  read-only tools Read and Grep, on a non-secret path inside the working
  directory.
- Anything outside the working directory, or matching a secret pattern, gets
  no opinion at all: Claude Code's own permission flow decides.
- The first read of a file the user named (or a traceback named) is never gated.

Standard library only.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex

from budgetpilot.core.scope import ScopeLimits

EDIT_TOOLS = ("Edit", "MultiEdit", "Write")
READ_ONLY_TOOLS = ("Read", "Grep", "Glob")
MAX_DENIES_PER_TASK = 3          # then the gate only observes for the rest of the task
OVERRIDE_WINDOW = 3              # a denied call repeated within this many tool calls is allowed
DUPLICATE_LIMIT = 2              # identical edit/run already seen this many times -> deny
TOKENS_PER_LINE_EST = 12         # rough tokens per source line, for avoided-token estimates
READ_DEFAULT_MAX_LINES = 2000    # Claude Code's own cap on an unbounded Read
MAX_RECOVERY_ASKS = 2
DEFAULT_CHARS_PER_TOKEN = 3.8

# File names in prompts and tracebacks (same pattern the legacy gate uses).
NAMED_FILE_PATTERN = (
    r"\b((?:[a-zA-Z0-9_\-\.\/]+\/)?[a-zA-Z0-9_\-]+\."
    r"(?:py|js|ts|jsx|tsx|json|yaml|yml|md|txt|html|css|toml|sql|sh|go|rs|c|cpp|h|java|rb|php))\b"
)

SECRET_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "credentials*", ".netrc", ".npmrc", ".pypirc", "*.keystore", "secrets.*", "*.secret",
)
SECRET_DIRS = (".ssh", ".aws", ".gnupg", ".kube")
BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ipynb", ".ico", ".zip", ".gz")

TEST_RUNNER_RE = re.compile(
    r"(?:^|[\s;&|(/])(?:pytest|py\.test|python[0-9.]*\s+-m\s+(?:pytest|unittest)|npm\s+(?:run\s+)?test|"
    r"yarn\s+test|pnpm\s+test|cargo\s+test|go\s+test|jest|vitest)(?:\s|$)"
)
MATCH_ALL_PATTERNS = {".*", ".", ".+", "*", "**", "**/*", "**/*.*", "", "^", "$", "\\w", "\\S"}
_PYTEST_VALUE_OPTS = {
    "-c", "-o", "-p", "-n", "-W", "-r", "--rootdir", "--maxfail", "--tb", "--durations", "--color",
    "--cov", "--cov-report", "--junitxml", "--basetemp", "--confcutdir", "--timeout", "-m",
}


class Decision:
    """One gate decision. verdict: allow | rewrite | deny | ask | context."""

    __slots__ = ("verdict", "rule", "reason", "updated_input", "context", "tokens_avoided_est")

    def __init__(self, verdict="allow", rule="", reason="", updated_input=None, context="", tokens_avoided_est=0):
        self.verdict = verdict
        self.rule = rule
        self.reason = reason
        self.updated_input = updated_input
        self.context = context
        self.tokens_avoided_est = tokens_avoided_est

    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__slots__}

    def __repr__(self) -> str:
        return f"Decision({self.to_dict()!r})"


def est_tokens(text: Any, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
    """Estimated tokens: UTF-8 bytes / chars-per-token. An estimate, never an exact count."""
    if text is None:
        return 0
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(text)
    if not text:
        return 0
    return int(math.ceil(len(text.encode("utf-8", errors="replace")) / max(chars_per_token, 0.5)))


def call_key(tool: str, tool_input: Dict[str, Any], salt: Any = None) -> str:
    """SHA-256 of the tool name and canonical input (and an optional salt)."""
    try:
        body = json.dumps([tool, tool_input, salt], sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        body = repr([tool, sorted(tool_input.items(), key=str), salt])
    return hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()[:20]


def extract_named(text: str) -> List[str]:
    """File names mentioned in a prompt or traceback, as lowercase paths plus basenames."""
    named: List[str] = []
    for match in re.findall(NAMED_FILE_PATTERN, text or ""):
        for item in (match.lower().lstrip("./"), Path(match).name.lower()):
            if item and item not in named:
                named.append(item)
    return named


def is_named(rel_path: str, named: List[str]) -> bool:
    if not rel_path or not named:
        return False
    rel = rel_path.lower()
    base = Path(rel).name
    return any(rel == n or base == n or rel.endswith("/" + n) for n in named)


def locate(cwd: str, raw_path: Any) -> Tuple[Optional[Path], Optional[str]]:
    """(resolved absolute path, path relative to cwd). The relative path is None outside cwd."""
    if not raw_path or not cwd:
        return None, None
    try:
        root = Path(cwd).resolve()
        target = Path(cwd, str(raw_path)).resolve()  # resolve() follows symlinks out of the repo
        return target, target.relative_to(root).as_posix()
    except (ValueError, OSError):
        try:
            return Path(cwd, str(raw_path)).resolve(), None
        except OSError:
            return None, None


def is_secret(path: Optional[Path]) -> bool:
    if path is None:
        return True
    name = path.name.lower()
    if any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_PATTERNS):
        return True
    return any(part.lower() in SECRET_DIRS for part in path.parts)


def count_lines(path: Path, max_bytes: int = 8_000_000) -> Optional[int]:
    """Line count of a text file. None for missing, binary or very large files."""
    try:
        if not path.is_file() or path.suffix.lower() in BINARY_SUFFIXES:
            return None
        size = path.stat().st_size
        if size > max_bytes:
            return None
        with open(path, "rb") as f:
            data = f.read()
        if b"\x00" in data[:4096]:
            return None
        return data.count(b"\n") + 1 if data else 0  # same convention as Claude Code's totalLines
    except OSError:
        return None


# ------------------------------------------------------------------ Bash analysis
def _segments(command: str) -> List[List[str]]:
    segments: List[List[str]] = []
    for part in re.split(r"&&|\|\||;|\|", command or ""):
        try:
            tokens = shlex.split(part)
        except ValueError:
            tokens = part.split()
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]
        if tokens:
            segments.append(tokens)
    return segments


def is_test_command(command: str) -> bool:
    return bool(TEST_RUNNER_RE.search(command or ""))


def _has_positional(args: List[str], value_opts: Any = ()) -> bool:
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in value_opts:
            skip = True
            continue
        if arg == "--":
            continue
        if not arg.startswith("-"):
            return True
    return False


def is_full_suite(command: str) -> bool:
    """True for a test run with no path, node id or name filter."""
    for tokens in _segments(command):
        names = [Path(t).name for t in tokens]
        for i, name in enumerate(names):
            rest = tokens[i + 1:]
            if name in ("pytest", "py.test") and (i == 0 or names[i - 1] in ("-m", "run", "exec") or i <= 2):
                if "-k" in rest or any(a.startswith("-k") and len(a) > 2 for a in rest):
                    return False
                if "--lf" in rest or "--last-failed" in rest:
                    return False
                return not _has_positional(rest, _PYTEST_VALUE_OPTS)
            if name == "unittest" and i > 0 and names[i - 1] == "-m":
                positional = [a for a in rest if not a.startswith("-")]
                return not positional or positional[0] == "discover"
            if name in ("npm", "yarn", "pnpm") and rest[:1] == ["test"] or (
                name in ("npm", "pnpm") and rest[:2] == ["run", "test"]
            ):
                tail = rest[rest.index("test") + 1:]
                return not _has_positional(tail)
            if name == "go" and rest[:1] == ["test"]:
                tail = rest[1:]
                if "-run" in tail:
                    return False
                packages = [a for a in tail if not a.startswith("-")]
                return not packages or any(p.endswith("...") for p in packages)
            if name == "cargo" and rest[:1] == ["test"]:
                return not _has_positional(rest[1:], {"-p", "--package", "--test", "--features", "-j"})
            if name in ("jest", "vitest"):
                tail = [a for a in rest if a not in ("run", "watch")]
                return not _has_positional(tail, {"-t", "--testNamePattern", "-c", "--config"})
    return False


def suggest_test_target(cwd: str, state: Dict[str, Any]) -> str:
    """A targeted pytest command built from files named in the prompt or touched this task."""
    candidates: List[str] = []
    for name in list(state.get("touched") or []) + list(state.get("named") or []):
        base = Path(name).name
        if not base.endswith(".py"):
            continue
        if base.startswith("test_") or base.endswith("_test.py"):
            candidates.append(name)
        else:
            candidates.extend([f"tests/test_{base}", f"test_{base}", f"tests/{Path(base).stem}_test.py"])
    root = Path(cwd)
    for candidate in candidates:
        try:
            if "/" in candidate and (root / candidate).is_file():
                return f"python -m pytest {candidate} -q"
            found = next(iter(root.glob(f"**/{Path(candidate).name}")), None)
            if found is not None and ".venv" not in found.parts and "node_modules" not in found.parts:
                return f"python -m pytest {found.relative_to(root).as_posix()} -q"
        except (OSError, ValueError):
            continue
    return "python -m pytest <path/to/test_file.py>::<test_name> -q"


def noisy_bash(command: str, cwd: str, limits: ScopeLimits) -> Optional[Tuple[str, str]]:
    """(rule, suggestion) for commands whose output is unbounded, else None."""
    for tokens in _segments(command):
        name = Path(tokens[0]).name
        args = tokens[1:]
        positional = [a for a in args if not a.startswith("-")]
        flags = "".join(a for a in args if a.startswith("-") and not a.startswith("--"))
        if name == "find" and positional[:1] and positional[0] in ("/", "~", "$HOME"):
            return "bash_find_root", "Use the Glob tool with a pattern inside the project."
        if name == "ls" and "R" in flags and (not positional or positional[0] in (".", "./")):
            return "bash_ls_recursive", "Use the Glob tool, or `ls` on one directory."
        if name in ("grep", "egrep", "fgrep") and ("r" in flags or "R" in flags) and not limits.allow_repo_wide_search:
            paths = positional[1:]
            if not paths or all(p in (".", "./", "/") for p in paths):
                return "bash_grep_recursive", "Use the Grep tool with a `path` (a file or package directory)."
        if name == "cat" and limits.read_window_lines and len(positional) == 1:
            target, rel = locate(cwd, positional[0])
            total = count_lines(target) if target is not None and rel is not None else None
            if total is not None and total > limits.read_window_lines:
                return (
                    "bash_cat_large",
                    f"{positional[0]} has {total} lines. Use the Read tool with offset and limit "
                    f"(up to {limits.read_window_lines} lines).",
                )
    return None


# ------------------------------------------------------------------ state
def init_state(prompt: str = "") -> Dict[str, Any]:
    return {
        "named": extract_named(prompt), "named_read": [], "reads": [], "touched": [], "created": [],
        "seq": 0, "denies": 0, "denied": {}, "hashes": {}, "edit_epoch": 0, "decisions": {},
        "edits_since_fail": [], "failed_edits": {}, "actions": [],
    }


def _state(task: Dict[str, Any]) -> Dict[str, Any]:
    state = task.setdefault("state", {})
    for key, default in init_state().items():
        state.setdefault(key, default)
    return state


def budget_fraction(task: Dict[str, Any]) -> float:
    ctx = task.get("ctx_used_est", 0) / task["ctx_budget"] if task.get("ctx_budget") else 0.0
    out = task.get("out_used", 0) / task["out_budget"] if task.get("out_budget") else 0.0
    return max(ctx, out)


def budget_summary(task: Dict[str, Any]) -> str:
    return (
        f"context ~{task.get('ctx_used_est', 0)}/{task.get('ctx_budget', 0)} est. tokens, "
        f"output {task.get('out_used', 0)}/{task.get('out_budget', 0)}"
    )


# ------------------------------------------------------------------ rules
def _read_rule(ti: Dict[str, Any], cwd: str, state: Dict[str, Any], limits: ScopeLimits) -> Decision:
    target, rel = locate(cwd, ti.get("file_path"))
    if rel is None or is_secret(target):
        return Decision("allow", "read_unmanaged")  # outside cwd or secret: never auto-approve
    named = is_named(rel, state["named"])
    if named and rel not in state["named_read"]:
        state["named_read"].append(rel)
        return Decision("allow", "named_first_read")
    if not named and rel not in state["reads"]:
        if len(state["reads"]) >= limits.max_files_outside_named:
            listed = ", ".join(state["reads"][-8:])
            return Decision(
                "deny", "read_file_cap",
                f"BudgetPilot: scope {limits.scope} allows {limits.max_files_outside_named} files beyond those "
                f"named in the task; already read: {listed}. Name the file you need, or ask the user to expand scope.",
            )
        state["reads"].append(rel)

    window = limits.read_window_lines
    if not window:
        return Decision("allow", "read_ok")
    try:
        limit = int(ti["limit"]) if ti.get("limit") is not None else None
        offset = max(1, int(ti.get("offset") or 1))
    except (TypeError, ValueError):
        return Decision("allow", "read_ok")
    if limit is not None and limit <= window:
        return Decision("allow", "read_ok")
    total = count_lines(target)
    if total is None:
        return Decision("allow", "read_ok")
    remaining = total - offset + 1
    if remaining <= window:
        return Decision("allow", "read_ok")
    would_read = min(remaining, limit if limit is not None else READ_DEFAULT_MAX_LINES, READ_DEFAULT_MAX_LINES)
    last = offset + window - 1
    return Decision(
        "rewrite", "read_window",
        f"BudgetPilot: windowed to {window} lines of {total}",
        updated_input={**ti, "offset": offset, "limit": window},
        context=(
            f"BudgetPilot: showing lines {offset}-{last} of {total} in {rel}. "
            f"Read again with offset={last + 1} if you need more."
        ),
        tokens_avoided_est=max(0, (would_read - window) * TOKENS_PER_LINE_EST),
    )


def _search_root(ti: Dict[str, Any], cwd: str) -> Tuple[bool, bool]:
    """(inside cwd and not secret, targets the repository root)."""
    raw = ti.get("path")
    if not raw:
        return True, True
    target, rel = locate(cwd, raw)
    if rel is None or is_secret(target):
        return False, False
    return True, rel in ("", ".")


def _grep_rule(ti: Dict[str, Any], cwd: str, limits: ScopeLimits) -> Decision:
    managed, at_root = _search_root(ti, cwd)
    if not managed:
        return Decision("allow", "grep_unmanaged")
    pattern = str(ti.get("pattern", "")).strip()
    if at_root and not ti.get("glob") and pattern in MATCH_ALL_PATTERNS and not limits.allow_repo_wide_search:
        return Decision(
            "deny", "grep_repo_wide",
            f"BudgetPilot: a match-everything search of the whole repository is not allowed at scope "
            f"{limits.scope}. Search for a specific symbol, or set `path` to one file or package directory.",
        )
    cap = limits.grep_head_limit
    try:
        head = int(ti["head_limit"]) if ti.get("head_limit") is not None else None
    except (TypeError, ValueError):
        head = None
    if head is None or head == 0 or head > cap:
        mode = ti.get("output_mode") or "files_with_matches"
        avoided = 0 if mode != "content" else max(0, ((head or 4 * cap) - cap)) * TOKENS_PER_LINE_EST // 2
        return Decision(
            "rewrite", "grep_head_limit", f"BudgetPilot: results capped at {cap} lines",
            updated_input={**ti, "head_limit": cap}, tokens_avoided_est=avoided,
        )
    return Decision("allow", "grep_ok")


def _glob_rule(ti: Dict[str, Any], cwd: str, limits: ScopeLimits) -> Decision:
    managed, at_root = _search_root(ti, cwd)
    pattern = str(ti.get("pattern", "")).strip()
    if managed and at_root and pattern in MATCH_ALL_PATTERNS and not limits.allow_repo_wide_search:
        return Decision(
            "deny", "glob_repo_wide",
            f"BudgetPilot: listing every file in the repository is not allowed at scope {limits.scope}. "
            "Use a narrower pattern such as `pkg/**/*.py`.",
        )
    return Decision("allow", "glob_ok")


def _bash_rule(ti: Dict[str, Any], cwd: str, state: Dict[str, Any], limits: ScopeLimits) -> Decision:
    command = str(ti.get("command", ""))
    if is_test_command(command) and is_full_suite(command) and not limits.allow_full_suite_tests:
        return Decision(
            "deny", "bash_full_suite",
            f"BudgetPilot: full test-suite runs are not allowed at scope {limits.scope}. "
            f"Run the relevant tests only, for example: {suggest_test_target(cwd, state)}",
        )
    noisy = noisy_bash(command, cwd, limits)
    if noisy:
        return Decision("deny", noisy[0], f"BudgetPilot: unbounded output. {noisy[1]}")
    return Decision("allow", "bash_ok")


def new_line_count(tool: str, ti: Dict[str, Any]) -> int:
    if tool == "Write":
        text = str(ti.get("content", ""))
        return text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    if tool == "MultiEdit":
        return sum(new_line_count("Edit", e) for e in (ti.get("edits") or []) if isinstance(e, dict))
    text = str(ti.get("new_string", ""))
    return text.count("\n") + (1 if text else 0)


def _edit_rule(tool: str, ti: Dict[str, Any], cwd: str, limits: ScopeLimits) -> Decision:
    cap = limits.edit_max_new_lines
    if not cap:
        return Decision("allow", "edit_ok")
    target, rel = locate(cwd, ti.get("file_path"))
    if target is None or not target.exists():
        return Decision("allow", "edit_new_file")  # never block creating a new file
    lines = new_line_count(tool, ti)
    if lines > cap:
        return Decision(
            "deny", "edit_too_large",
            f"BudgetPilot: this {tool} writes {lines} lines to an existing file; scope {limits.scope} allows "
            f"{cap} per call. Split it into smaller Edits, each with a short unique old_string.",
        )
    return Decision("allow", "edit_ok")


def recovery_prompt(recovery: Dict[str, Any]) -> str:
    return (
        f"BudgetPilot: the last test run failed [{str(recovery.get('signature', ''))[:8]}, "
        f"{recovery.get('class', 'unknown')}]. Recommended next step: {recovery.get('recommended_label', 'Execute')}. "
        "Allow to continue as is, or deny and tell Claude which option to take."
    )


# ------------------------------------------------------------------ entry point
def decide(payload: Dict[str, Any], task: Dict[str, Any], limits: ScopeLimits, hitl: bool = True) -> Decision:
    """
    Decide one PreToolUse call. Idempotent per `tool_use_id`: a repeated payload
    returns the stored decision without touching counters.
    """
    tool = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    cwd = str(payload.get("cwd") or task.get("cwd") or "")
    use_id = str(payload.get("tool_use_id") or "")
    state = _state(task)

    if use_id and use_id in state["decisions"]:
        return Decision(**state["decisions"][use_id])

    state["seq"] += 1
    decision = _decide(tool, ti, cwd, use_id, task, state, limits, hitl)

    if decision.verdict == "deny":
        state["denied"][call_key(tool, ti)] = state["seq"]
        state["denies"] += 1
    if use_id:
        state["decisions"][use_id] = decision.to_dict()
        if len(state["decisions"]) > 40:
            for old in list(state["decisions"])[:-40]:
                del state["decisions"][old]
    return decision


def _decide(tool: str, ti: Dict[str, Any], cwd: str, use_id: str, task: Dict[str, Any],
            state: Dict[str, Any], limits: ScopeLimits, hitl: bool) -> Decision:
    key = call_key(tool, ti)
    is_edit = tool in EDIT_TOOLS
    is_test = tool == "Bash" and is_test_command(str(ti.get("command", "")))

    # A denied call repeated within a few tool calls is an explicit override.
    denied_at = state["denied"].get(key)
    if denied_at is not None and state["seq"] - denied_at <= OVERRIDE_WINDOW:
        del state["denied"][key]
        return _track(Decision("allow", "override"), tool, ti, cwd, state)

    # Human-in-the-loop recovery: the choice cannot be skipped by editing or re-running.
    recovery = state.get("recovery")
    if recovery and (is_edit or is_test) and hitl:
        if recovery.get("asks", 0) >= MAX_RECOVERY_ASKS:
            state.pop("recovery", None)
            state["recovery_skipped"] = True
            task["pending_recovery"] = None
        else:
            recovery["asks"] = recovery.get("asks", 0) + 1
            recovery["ask_id"] = use_id
            return Decision("ask", "recovery_pending", recovery_prompt(recovery))

    # Budget boundary.
    fraction = budget_fraction(task)
    if fraction >= 1.0 and not state.get("budget_hit"):
        state["budget_hit"] = True
        if hitl:
            state["budget_ask"] = use_id
            return Decision(
                "ask", "budget_reached",
                f"BudgetPilot: task budget reached ({budget_summary(task)}). Allow to expand scope "
                f"{task.get('scope')} -> {min(3, int(task.get('scope', 1)) + 1)} and continue, "
                "or deny to stop and summarize.",
            )
        return Decision(
            "deny", "budget_reached",
            f"BudgetPilot: task budget reached ({budget_summary(task)}). Stop exploring. If this call is "
            "essential to finish the task, repeat it once; otherwise summarize what is done and stop.",
        )

    if tool == "Read":
        decision = _read_rule(ti, cwd, state, limits)
    elif tool == "Grep":
        decision = _grep_rule(ti, cwd, limits)
    elif tool == "Glob":
        decision = _glob_rule(ti, cwd, limits)
    elif tool == "Bash":
        decision = _bash_rule(ti, cwd, state, limits)
    elif is_edit:
        decision = _edit_rule(tool, ti, cwd, limits)
    else:
        decision = Decision("allow", "unmanaged_tool")

    # Duplicate-retry blocker for edits and commands.
    if decision.verdict == "allow" and (is_edit or tool == "Bash"):
        dup_key = call_key(tool, ti, state["edit_epoch"] if tool == "Bash" else None)
        seen = state["hashes"].get(dup_key, 0)
        signature = (state.get("last_failure") or {}).get("signature")
        if is_edit and signature and key in state["failed_edits"].get(signature, []):
            decision = Decision(
                "deny", "edit_repeats_failed_fix",
                f"BudgetPilot: this exact edit was already tried and the same failure followed "
                f"(signature {signature[:8]}). Change the approach.",
            )
        elif seen >= DUPLICATE_LIMIT:
            what = "edit" if is_edit else "command (nothing was edited since it last ran)"
            decision = Decision(
                "deny", "duplicate_retry",
                f"BudgetPilot: identical {what} already attempted {seen} times in this task. Change the approach.",
            )
        else:
            state["hashes"][dup_key] = seen + 1

    if decision.verdict in ("allow", "rewrite"):
        decision = _track(decision, tool, ti, cwd, state)

    # One-time 80 % warning, attached to whatever was decided.
    if decision.verdict in ("allow", "rewrite") and fraction >= 0.8 and not state.get("warned80"):
        state["warned80"] = True
        note = f"BudgetPilot: 80% of the task budget is used ({budget_summary(task)}). Wrap up."
        decision.context = f"{decision.context} {note}".strip()
        if decision.verdict == "allow":
            decision.verdict = "context"
    return decision


def _track(decision: Decision, tool: str, ti: Dict[str, Any], cwd: str, state: Dict[str, Any]) -> Decision:
    """Remember files an allowed edit will touch (for recovery snapshots and test suggestions)."""
    if tool in EDIT_TOOLS:
        target, rel = locate(cwd, ti.get("file_path"))
        if rel is not None and target is not None:
            if rel not in state["touched"]:
                state["touched"].append(rel)
                if not target.exists() and rel not in state["created"]:
                    state["created"].append(rel)
    return decision


def gate_fallback_active(state: Dict[str, Any]) -> bool:
    """After MAX_DENIES_PER_TASK denials the gate only observes for the rest of the task."""
    return int(state.get("denies", 0)) >= MAX_DENIES_PER_TASK

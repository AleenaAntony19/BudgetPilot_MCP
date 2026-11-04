"""
BudgetPilot Sandboxed Repository Tools.
Phase B: Native filesystem tools with directory confinement, truncation protection, and Ollama tool-use schemas.

Tools:
1. list_dir(path=".", depth=1)
2. search(pattern, path=".")
3. read_file(path, start=1, end=None)
4. edit_file(path, old, new)
5. run_tests(command=None, target=None, timeout=60)
"""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from budgetpilot.config import load_config

MAX_OUTPUT_CHARS = 20_000
_ACTIVE_REPO_ROOT: Optional[Path] = None


def set_repo_root(path: Union[str, Path]) -> None:
    """Set global active repository root for sandboxing."""
    global _ACTIVE_REPO_ROOT
    _ACTIVE_REPO_ROOT = Path(path).expanduser().resolve()


def get_repo_root() -> Path:
    """Retrieve active repository root (defaults to CWD)."""
    global _ACTIVE_REPO_ROOT
    if _ACTIVE_REPO_ROOT is not None:
        return _ACTIVE_REPO_ROOT
    env_root = os.environ.get("BP_REPO_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path.cwd().resolve()


def resolve_safe_path(
    target_path: Union[str, Path],
    repo_root: Optional[Union[str, Path]] = None,
) -> Path:
    """
    Resolve a target path securely against the repository root.
    Rejects any path traversal attempts escaping the root.

    Raises:
        PermissionError: If path attempts to escape repository root.
    """
    root = Path(repo_root).expanduser().resolve() if repo_root else get_repo_root()

    raw_path_str = str(target_path).strip()

    # Reject blatant traversal strings
    if raw_path_str.startswith("../") or "/../" in raw_path_str or raw_path_str == "..":
        raise PermissionError(
            f"Path traversal denied: '{target_path}' attempts to escape repository root '{root}'."
        )

    target = Path(target_path)
    if target.is_absolute():
        resolved = target.resolve()
    else:
        resolved = (root / target).resolve()

    try:
        resolved.relative_to(root)
    except ValueError:
        raise PermissionError(
            f"Path traversal denied: '{target_path}' resolves to '{resolved}', which is outside root '{root}'."
        )

    return resolved


def _truncate_output(text: str, meta: Dict[str, Any]) -> str:
    """Enforce MAX_OUTPUT_CHARS cap with clear warning message."""
    if len(text) <= MAX_OUTPUT_CHARS:
        meta["truncated"] = False
        return text

    truncated_count = len(text) - MAX_OUTPUT_CHARS
    meta["truncated"] = True
    meta["omitted_chars"] = truncated_count
    return (
        text[:MAX_OUTPUT_CHARS]
        + f"\n\n[WARNING: Output truncated. {truncated_count} characters omitted because output exceeded {MAX_OUTPUT_CHARS} characters limit.]"
    )


# ---------------------------------------------------------------------------
# 1. list_dir
# ---------------------------------------------------------------------------
def list_dir(
    path: str = ".",
    depth: int = 1,
    repo_root: Optional[Union[str, Path]] = None,
) -> Tuple[str, bool, Dict[str, Any]]:
    """
    List contents of a directory up to the specified depth within repository sandbox.
    """
    meta: Dict[str, Any] = {"tool": "list_dir", "path": path, "depth": depth}
    try:
        resolved = resolve_safe_path(path, repo_root=repo_root)
    except PermissionError as exc:
        meta["error"] = "path_traversal"
        return str(exc), False, meta

    if not resolved.exists():
        meta["error"] = "not_found"
        return f"Directory '{path}' does not exist.", False, meta

    if not resolved.is_dir():
        meta["error"] = "not_a_directory"
        return f"Path '{path}' is a file, not a directory.", False, meta

    root = Path(repo_root).resolve() if repo_root else get_repo_root()
    lines: List[str] = []
    item_count = 0

    def _walk(curr_dir: Path, curr_depth: int) -> None:
        nonlocal item_count
        if curr_depth > depth:
            return
        try:
            entries = sorted(curr_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            lines.append(f"{'  ' * (curr_depth - 1)}[ACCESS DENIED] {curr_dir.name}/")
            return

        for entry in entries:
            # Skip common hidden noise
            if entry.name in (".git", "__pycache__", ".pytest_cache", ".venv"):
                continue

            rel = entry.relative_to(root)
            indent = "  " * (curr_depth - 1)
            item_count += 1

            if entry.is_dir():
                lines.append(f"{indent}[DIR]  {entry.name}/")
                _walk(entry, curr_depth + 1)
            else:
                try:
                    size = entry.stat().st_size
                    size_str = f"{size} B" if size < 1024 else f"{size / 1024:.1f} KB"
                except Exception:
                    size_str = "unknown"
                lines.append(f"{indent}[FILE] {entry.name} ({size_str})")

    _walk(resolved, 1)

    meta["count"] = item_count
    if not lines:
        return f"Directory '{path}' is empty.", True, meta

    header = f"Contents of '{path}' (depth {depth}):\n"
    output = header + "\n".join(lines)
    return _truncate_output(output, meta), True, meta


# ---------------------------------------------------------------------------
# 2. search (ripgrep)
# ---------------------------------------------------------------------------
def search(
    pattern: str,
    path: str = ".",
    repo_root: Optional[Union[str, Path]] = None,
) -> Tuple[str, bool, Dict[str, Any]]:
    """
    Search files for a pattern using ripgrep (rg) within the repository sandbox.
    Falls back to Python-based search if rg is not found.
    """
    meta: Dict[str, Any] = {"tool": "search", "pattern": pattern, "path": path}
    try:
        resolved = resolve_safe_path(path, repo_root=repo_root)
    except PermissionError as exc:
        meta["error"] = "path_traversal"
        return str(exc), False, meta

    if not resolved.exists():
        meta["error"] = "not_found"
        return f"Search path '{path}' does not exist.", False, meta

    root = Path(repo_root).resolve() if repo_root else get_repo_root()
    rg_path = shutil.which("rg")

    if rg_path:
        # Run ripgrep
        cmd = [
            rg_path,
            "--no-heading",
            "--line-number",
            "--color=never",
            "--max-count", "100",
            pattern,
            str(resolved),
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=30,
            )
            raw_out = proc.stdout.strip()
            if proc.returncode == 0 and raw_out:
                lines = raw_out.splitlines()
                meta["matches"] = len(lines)
                meta["engine"] = "ripgrep"
                return _truncate_output(raw_out, meta), True, meta
            elif proc.returncode == 1 or not raw_out:
                meta["matches"] = 0
                meta["engine"] = "ripgrep"
                return f"No matches found for pattern '{pattern}' in '{path}'.", True, meta
            else:
                meta["error"] = proc.stderr.strip()
                return f"ripgrep error: {proc.stderr.strip()}", False, meta
        except subprocess.TimeoutExpired:
            meta["error"] = "timeout"
            return f"Search for pattern '{pattern}' timed out after 30 seconds.", False, meta

    # Fallback pure-Python search
    meta["engine"] = "python_fallback"
    matches: List[str] = []
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        meta["error"] = "invalid_regex"
        return f"Invalid regex pattern: {exc}", False, meta

    search_targets = [resolved] if resolved.is_file() else resolved.rglob("*")
    for file_path in search_targets:
        if file_path.is_file():
            if any(part in (".git", "__pycache__", ".pytest_cache", ".venv") for part in file_path.parts):
                continue
            try:
                rel_path = file_path.relative_to(root)
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    for line_no, line in enumerate(f, start=1):
                        if regex.search(line):
                            matches.append(f"{rel_path}:{line_no}:{line.rstrip()}")
                            if len(matches) >= 500:
                                break
            except Exception:
                continue

    meta["matches"] = len(matches)
    if not matches:
        return f"No matches found for pattern '{pattern}' in '{path}'.", True, meta

    output = "\n".join(matches)
    return _truncate_output(output, meta), True, meta


# ---------------------------------------------------------------------------
# 3. read_file
# ---------------------------------------------------------------------------
def read_file(
    path: str,
    start: int = 1,
    end: Optional[int] = None,
    repo_root: Optional[Union[str, Path]] = None,
) -> Tuple[str, bool, Dict[str, Any]]:
    """
    Read file contents with 1-indexed line numbers within the repository sandbox.
    """
    meta: Dict[str, Any] = {"tool": "read_file", "path": path, "start": start, "end": end}
    try:
        resolved = resolve_safe_path(path, repo_root=repo_root)
    except PermissionError as exc:
        meta["error"] = "path_traversal"
        return str(exc), False, meta

    if not resolved.exists():
        meta["error"] = "not_found"
        return f"File '{path}' does not exist.", False, meta

    if resolved.is_dir():
        meta["error"] = "is_directory"
        return f"Path '{path}' is a directory, not a file.", False, meta

    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except Exception as exc:
        meta["error"] = str(exc)
        return f"Could not read file '{path}': {exc}", False, meta

    total_lines = len(all_lines)
    meta["total_lines"] = total_lines

    start_idx = max(1, start)
    end_idx = min(total_lines, end) if end is not None else total_lines

    if start_idx > total_lines:
        return f"File '{path}' only has {total_lines} lines (requested start at line {start_idx}).", True, meta

    formatted: List[str] = []
    for line_num in range(start_idx, end_idx + 1):
        line_content = all_lines[line_num - 1].rstrip("\r\n")
        formatted.append(f"{line_num:4d} | {line_content}")

    meta["lines_read"] = len(formatted)
    output = f"File: {path} (lines {start_idx}-{end_idx} of {total_lines})\n" + "\n".join(formatted)
    return _truncate_output(output, meta), True, meta


# ---------------------------------------------------------------------------
# 4. edit_file
# ---------------------------------------------------------------------------
def edit_file(
    path: str,
    old: str,
    new: str,
    repo_root: Optional[Union[str, Path]] = None,
) -> Tuple[str, bool, Dict[str, Any]]:
    """
    Perform exact string replacement in a target file.
    Fails if 'old' string is not found or is not uniquely matched.
    """
    meta: Dict[str, Any] = {"tool": "edit_file", "path": path}
    try:
        resolved = resolve_safe_path(path, repo_root=repo_root)
    except PermissionError as exc:
        meta["error"] = "path_traversal"
        return str(exc), False, meta

    if not resolved.exists():
        meta["error"] = "not_found"
        return f"File '{path}' does not exist.", False, meta

    if resolved.is_dir():
        meta["error"] = "is_directory"
        return f"Cannot edit directory '{path}'.", False, meta

    try:
        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as exc:
        meta["error"] = str(exc)
        return f"Could not read file '{path}': {exc}", False, meta

    match_count = content.count(old)
    meta["match_count"] = match_count

    if match_count == 0:
        meta["error"] = "string_not_found"
        return (
            f"Failed to edit '{path}': target string to replace was not found in file.",
            False,
            meta,
        )

    if match_count > 1:
        meta["error"] = "not_unique"
        return (
            f"Failed to edit '{path}': target string occurs {match_count} times (must be unique). "
            "Please include more surrounding context in 'old' to disambiguate.",
            False,
            meta,
        )

    # Unique match confirmed -> execute exact replacement
    new_content = content.replace(old, new, 1)
    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as exc:
        meta["error"] = str(exc)
        return f"Failed to write modifications to '{path}': {exc}", False, meta

    meta["replaced"] = 1
    return f"Successfully modified {path}.", True, meta


# ---------------------------------------------------------------------------
# 5. run_tests
# ---------------------------------------------------------------------------
def run_tests(
    command: Optional[str] = None,
    target: Optional[str] = None,
    timeout: int = 60,
    repo_root: Optional[Union[str, Path]] = None,
) -> Tuple[str, bool, Dict[str, Any]]:
    """
    Execute tests within repository sandbox with configurable timeout.
    """
    root = Path(repo_root).resolve() if repo_root else get_repo_root()

    if not command:
        try:
            cfg = load_config()
            command = cfg.test_commands.default
        except Exception:
            command = "pytest"

    cmd = command if not target else f"{command} {target}"
    meta: Dict[str, Any] = {"tool": "run_tests", "command": cmd, "timeout": timeout}

    exec_cmd = cmd
    if not shutil.which("python") and re.match(r"^python\b", exec_cmd):
        exec_cmd = re.sub(r"^python\b", sys.executable, exec_cmd)

    try:
        proc = subprocess.run(
            exec_cmd,
            shell=True,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        combined = stdout
        if stderr:
            combined = f"{combined}\n--- STDERR ---\n{stderr}".strip()

        ok = (proc.returncode == 0)
        meta["exit_code"] = proc.returncode
        meta["passed"] = ok

        header = f"Command: {cmd} (Exit Code: {proc.returncode})\n\n"
        full_output = header + (combined if combined else "(No output)")
        return _truncate_output(full_output, meta), ok, meta

    except subprocess.TimeoutExpired:
        meta["exit_code"] = -1
        meta["timeout_expired"] = True
        return (
            f"Test execution timed out after {timeout} seconds for command: '{cmd}'.",
            False,
            meta,
        )
    except Exception as exc:
        meta["error"] = str(exc)
        return f"Failed to execute tests: {exc}", False, meta


# ---------------------------------------------------------------------------
# 6. request_scope
# ---------------------------------------------------------------------------
def request_scope(
    reason: str = "Broadened scope required for task",
    repo_root: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> Tuple[str, bool, Dict[str, Any]]:
    """
    Request a task scope expansion (+1 level) with justification.
    Allows expanding from Scope 1 -> 2, or Scope 2 -> 3 when broader exploration
    or multi-file modifications are required.
    """
    clean_reason = str(reason).strip() if reason else "Broadened scope required for task"
    meta: Dict[str, Any] = {
        "tool": "request_scope",
        "reason": clean_reason,
        "scope_increment": 1,
    }
    output = f"Scope expansion requested: '{clean_reason}'. Scope increment of +1 level granted."
    return output, True, meta


# ---------------------------------------------------------------------------
# Tool Dispatcher and Ollama JSON Schemas
# ---------------------------------------------------------------------------

TOOL_MAP: Dict[str, Callable[..., Tuple[str, bool, Dict[str, Any]]]] = {
    "list_dir": list_dir,
    "search": search,
    "read_file": read_file,
    "edit_file": edit_file,
    "run_tests": run_tests,
    "request_scope": request_scope,
}

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List directory contents up to a specified depth within repository sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to directory (default: '.')",
                        "default": ".",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Recursion depth (default: 1)",
                        "default": 1,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search repository files for text or regex pattern using ripgrep.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "String or regex pattern to search for",
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative search path (default: '.')",
                        "default": ".",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents with 1-indexed line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to target file",
                    },
                    "start": {
                        "type": "integer",
                        "description": "Starting line number (1-indexed, default: 1)",
                        "default": 1,
                    },
                    "end": {
                        "type": "integer",
                        "description": "Ending line number (inclusive, optional)",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Perform exact string replacement in a file. Must uniquely match in file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to target file",
                    },
                    "old": {
                        "type": "string",
                        "description": "Exact text string to be replaced (must be unique)",
                    },
                    "new": {
                        "type": "string",
                        "description": "New replacement text",
                    },
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Execute tests within repository sandbox with configurable timeout.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Test command to execute (defaults to configured test runner)",
                    },
                    "target": {
                        "type": "string",
                        "description": "Optional specific test file or target path",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Execution timeout in seconds (default: 60)",
                        "default": 60,
                    },
                },
                "required": [],
            },
        },
    },
]

REQUEST_SCOPE_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "request_scope",
        "description": "Request a scope expansion (+1 level) when task requires broader edits or reading more files.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Justification for why current scope is insufficient",
                },
            },
            "required": ["reason"],
        },
    },
}

AGENT_TOOL_SCHEMAS: List[Dict[str, Any]] = TOOL_SCHEMAS + [REQUEST_SCOPE_SCHEMA]



def execute_tool(
    name: str,
    arguments: Dict[str, Any],
    repo_root: Optional[Union[str, Path]] = None,
) -> Tuple[str, bool, Dict[str, Any]]:
    """
    Dispatch and execute a sandboxed repository tool by name.
    """
    if name not in TOOL_MAP:
        return (
            f"Unknown tool '{name}'. Available tools: {list(TOOL_MAP.keys())}",
            False,
            {"error": "unknown_tool", "tool": name},
        )

    fn = TOOL_MAP[name]
    kwargs = dict(arguments)
    if "repo_root" not in kwargs:
        kwargs["repo_root"] = repo_root

    try:
        return fn(**kwargs)
    except Exception as exc:
        return (
            f"Unexpected error executing tool '{name}': {exc}",
            False,
            {"error": "execution_error", "tool": name, "details": str(exc)},
        )

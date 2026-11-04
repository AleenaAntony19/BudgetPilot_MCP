"""
BudgetPilot Failure State Generators.
Phase J: Constructs reproducible failure states on a target git repository for calibration rollouts.

Three failure kind generators:
    under_scoped    — Apply only primary file hunks from multi-file commits; dependent files stay stale.
    repairable      — Apply mutation operators (off-by-one, swapped args, wrong comparison, missing return).
    capability_gap  — Complex historical commits where the cheap model tier fails from scratch.

Each generator produces a FailureState dataclass containing:
    - failure_kind: FailureKind enum
    - worktree_path: Path to isolated git worktree at the base commit
    - base_commit: Git commit SHA the worktree is pinned to
    - touched_files: Files that were partially or mutantly applied
    - stale_files: Files that depend on changes but were left stale (under_scoped only)
    - mutation_description: Human-readable description of the mutation applied
    - failure_class: Predicted FailureClass for router prior calibration
    - metadata: Free-form dict for debugging and reporting

Strict Rule: This module must NOT import ollama, mcp, fastapi, or uvicorn.
"""

from dataclasses import dataclass, field
from enum import Enum
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from budgetpilot.core.types import FailureClass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mutation operator catalogue
# ---------------------------------------------------------------------------

class MutationKind(str, Enum):
    """Atomic mutation operators applied to source code for repairable failures."""
    OFF_BY_ONE = "off_by_one"
    SWAPPED_ARGS = "swapped_args"
    WRONG_COMPARISON = "wrong_comparison"
    MISSING_RETURN = "missing_return"


class FailureKind(str, Enum):
    """Top-level failure state category produced by the generators."""
    UNDER_SCOPED = "under_scoped"
    REPAIRABLE = "repairable"
    CAPABILITY_GAP = "capability_gap"


# ---------------------------------------------------------------------------
# Core data structure
# ---------------------------------------------------------------------------

@dataclass
class FailureState:
    """
    A reproducible failure state ready for recovery action rollouts.

    worktree_path is the absolute path to a git worktree pinned to base_commit.
    It must be cleaned up by the caller via `git worktree remove --force`.
    """
    failure_kind: FailureKind
    base_commit: str
    worktree_path: Path
    touched_files: List[str] = field(default_factory=list)
    stale_files: List[str] = field(default_factory=list)
    mutation_description: str = ""
    failure_class: FailureClass = FailureClass.ASSERTION_LOCAL
    repo_path: Path = field(default_factory=Path)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "failure_kind": self.failure_kind.value,
            "base_commit": self.base_commit,
            "worktree_path": str(self.worktree_path),
            "touched_files": self.touched_files,
            "stale_files": self.stale_files,
            "mutation_description": self.mutation_description,
            "failure_class": self.failure_class.value,
            "repo_path": str(self.repo_path),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FailureState":
        return cls(
            failure_kind=FailureKind(data["failure_kind"]),
            base_commit=data["base_commit"],
            worktree_path=Path(data["worktree_path"]),
            touched_files=data.get("touched_files", []),
            stale_files=data.get("stale_files", []),
            mutation_description=data.get("mutation_description", ""),
            failure_class=FailureClass(data.get("failure_class", "assertion_local")),
            repo_path=Path(data.get("repo_path", ".")),
            metadata=data.get("metadata", {}),
        )


# ---------------------------------------------------------------------------
# Git utility helpers
# ---------------------------------------------------------------------------

def _run_git(args: List[str], cwd: str, timeout: int = 15, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command, returning CompletedProcess. Never raises on check=False."""
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _create_worktree(repo_path: Path, base_commit: str, worktree_dir: Path) -> bool:
    """
    Create a git worktree at base_commit inside worktree_dir.
    Returns True on success, False on failure.
    """
    result = _run_git(
        ["worktree", "add", "--detach", str(worktree_dir), base_commit],
        cwd=str(repo_path),
        check=False,
    )
    if result.returncode != 0:
        logger.warning(
            "Failed to create worktree at %s for commit %s: %s",
            worktree_dir, base_commit, result.stderr.strip(),
        )
        return False
    return True


def _cleanup_worktree(repo_path: Path, worktree_dir: Path) -> None:
    """Safely remove a git worktree. Used in finally blocks."""
    try:
        _run_git(
            ["worktree", "remove", "--force", str(worktree_dir)],
            cwd=str(repo_path),
            check=False,
            timeout=10,
        )
    except Exception as exc:
        logger.debug("Could not remove worktree %s via git: %s; trying shutil.", worktree_dir, exc)
    # Belt-and-suspenders: remove directory residue
    if worktree_dir.exists():
        try:
            shutil.rmtree(str(worktree_dir), ignore_errors=True)
        except Exception:
            pass


def _get_commit_files(repo_path: Path, commit: str) -> List[str]:
    """Return list of files changed in a given commit."""
    result = _run_git(
        ["show", "--format=", "--name-only", commit],
        cwd=str(repo_path),
        check=False,
    )
    if result.returncode != 0:
        return []
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def _get_recent_multi_file_commits(
    repo_path: Path,
    min_files: int = 2,
    limit: int = 50,
) -> List[Tuple[str, List[str]]]:
    """
    Return recent commits that touch at least `min_files` files.
    Returns list of (commit_sha, [files_changed]).
    """
    result = _run_git(
        ["log", f"-n{limit}", "--format=%H"],
        cwd=str(repo_path),
        check=False,
    )
    if result.returncode != 0:
        return []

    commits = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    multi_file: List[Tuple[str, List[str]]] = []
    for sha in commits:
        files = _get_commit_files(repo_path, sha)
        if len(files) >= min_files:
            multi_file.append((sha, files))

    return multi_file


def _apply_diff_partial(
    worktree_path: Path,
    repo_path: Path,
    commit: str,
    include_files: Sequence[str],
) -> bool:
    """
    Apply only the hunks that touch `include_files` from a given commit's diff.
    Leaves all other files in the commit's diff unchanged (stale).
    """
    # Get the full diff for the commit
    diff_result = _run_git(
        ["show", "--format=", commit, "--"],
        cwd=str(repo_path),
        check=False,
    )
    if diff_result.returncode != 0 or not diff_result.stdout.strip():
        return False

    # Filter diff to only include specified files
    include_set = set(include_files)
    filtered_lines: List[str] = []
    in_target = False

    for line in diff_result.stdout.splitlines(keepends=True):
        if line.startswith("diff --git "):
            # Parse: "diff --git a/foo.py b/foo.py"
            parts = line.split()
            file_b = parts[-1].lstrip("b/") if len(parts) >= 4 else ""
            in_target = file_b in include_set
        if in_target:
            filtered_lines.append(line)

    if not filtered_lines:
        return False

    filtered_diff = "".join(filtered_lines)

    # Apply patch inside worktree
    patch_result = subprocess.run(
        ["git", "apply", "--3way", "--whitespace=fix", "-"],
        input=filtered_diff,
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return patch_result.returncode == 0


# ---------------------------------------------------------------------------
# Mutation operators
# ---------------------------------------------------------------------------

_OFF_BY_ONE_RE = re.compile(r"(\b\w+\b)\s*(\+|-)\s*(\d+)\b")
_COMPARISON_MAP = {"==": "!=", "!=": "==", ">=": ">", "<=": "<", ">": "<=", "<": ">="}
_RETURN_RE = re.compile(r"^(\s+)return\s+(.+)$", re.MULTILINE)


def _mutate_off_by_one(source: str) -> Tuple[str, str]:
    """Replace `x + N` with `x + (N+1)` or `x - N` with `x - (N-1)` for first match."""
    def replacer(m: re.Match) -> str:
        var, op, num_str = m.group(1), m.group(2), m.group(3)
        n = int(num_str)
        new_n = n + 1 if op == "+" else max(0, n - 1)
        return f"{var} {op} {new_n}"

    new_source, count = _OFF_BY_ONE_RE.subn(replacer, source, count=1)
    if count:
        return new_source, f"off-by-one: mutated first arithmetic literal ±1"
    return source, ""


def _mutate_swapped_args(source: str) -> Tuple[str, str]:
    """Swap arguments in the first function call with exactly 2 arguments."""
    call_re = re.compile(r"(\w+)\(([^,()]+),\s*([^,()]+)\)")
    match = call_re.search(source)
    if match:
        fn, a1, a2 = match.group(1), match.group(2), match.group(3)
        new_source = source[:match.start()] + f"{fn}({a2.strip()}, {a1.strip()})" + source[match.end():]
        return new_source, f"swapped_args: swapped arguments in call to '{fn}'"
    return source, ""


def _mutate_wrong_comparison(source: str) -> Tuple[str, str]:
    """Flip the first comparison operator found in the source."""
    for op, replacement in _COMPARISON_MAP.items():
        idx = source.find(op)
        if idx != -1:
            # Avoid replacing inside strings or inside !=
            context = source[max(0, idx - 1):idx + len(op) + 1]
            if '"' not in context and "'" not in context:
                new_source = source[:idx] + replacement + source[idx + len(op):]
                return new_source, f"wrong_comparison: replaced '{op}' with '{replacement}'"
    return source, ""


def _mutate_missing_return(source: str) -> Tuple[str, str]:
    """Comment out the first bare `return <value>` statement found in the source."""
    match = _RETURN_RE.search(source)
    if match:
        indent = match.group(1)
        value = match.group(2)
        original = match.group(0)
        replacement = f"{indent}# MUTATED: return {value}"
        new_source = source.replace(original, replacement, 1)
        return new_source, f"missing_return: commented out 'return {value[:40]}'"
    return source, ""


MUTATION_OPERATORS = {
    MutationKind.OFF_BY_ONE: _mutate_off_by_one,
    MutationKind.SWAPPED_ARGS: _mutate_swapped_args,
    MutationKind.WRONG_COMPARISON: _mutate_wrong_comparison,
    MutationKind.MISSING_RETURN: _mutate_missing_return,
}


def _apply_mutation(
    worktree_path: Path,
    python_files: List[str],
    kind: MutationKind,
) -> Tuple[List[str], str]:
    """
    Apply a mutation operator to the first Python file where it produces a change.
    Returns (mutated_files, mutation_description).
    """
    operator = MUTATION_OPERATORS[kind]
    for rel_path in python_files:
        abs_path = worktree_path / rel_path
        if not abs_path.exists() or not abs_path.is_file():
            continue
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
            new_source, description = operator(source)
            if new_source != source and description:
                abs_path.write_text(new_source, encoding="utf-8")
                return [rel_path], description
        except Exception as exc:
            logger.debug("Mutation %s failed on %s: %s", kind.value, rel_path, exc)
    return [], ""


def _pick_python_files(repo_path: Path, commit: str, worktree_path: Path) -> List[str]:
    """Return .py files changed in the commit that exist in the worktree."""
    all_files = _get_commit_files(repo_path, commit)
    return [f for f in all_files if f.endswith(".py") and (worktree_path / f).exists()]


# ---------------------------------------------------------------------------
# Failure State Generators
# ---------------------------------------------------------------------------

def generate_under_scoped_failure(
    repo_path: Path,
    base_commit: Optional[str] = None,
    worktree_base_dir: Optional[Path] = None,
) -> Optional[FailureState]:
    """
    Generate an under_scoped failure state.

    Strategy:
    1. Find a multi-file commit (≥2 files) in the repo history.
    2. Create a worktree pinned to the commit's parent (pre-change state).
    3. Apply only the diff hunks for the first (primary) file.
    4. Leave remaining dependent files unchanged (stale).

    The worktree is left in a state where the primary file has been modified
    but dependent files still reference the old interface — producing
    SymbolOutsideTouched or ImportError failures when tests run.
    """
    repo_path = Path(repo_path).resolve()
    candidates = _get_recent_multi_file_commits(repo_path, min_files=2, limit=60)
    if not candidates:
        logger.warning("No multi-file commits found in %s for under_scoped failure.", repo_path)
        return None

    # Use provided commit or pick first candidate
    if base_commit:
        all_files = _get_commit_files(repo_path, base_commit)
        if len(all_files) < 2:
            logger.warning("Provided commit %s touches fewer than 2 files.", base_commit)
            return None
        chosen_commit = base_commit
        changed_files = all_files
    else:
        chosen_commit, changed_files = candidates[0]

    # Identify parent commit (before the change)
    parent_result = _run_git(
        ["rev-parse", f"{chosen_commit}^"],
        cwd=str(repo_path),
        check=False,
    )
    parent_commit = parent_result.stdout.strip() if parent_result.returncode == 0 else chosen_commit

    # Create isolated worktree at parent commit
    wt_dir = worktree_base_dir or (repo_path.parent / f"bp_wt_{int(time.time())}")
    wt_dir = Path(wt_dir)
    wt_dir.mkdir(parents=True, exist_ok=True)
    if not _create_worktree(repo_path, parent_commit, wt_dir):
        return None

    # Determine primary file (first touched .py file; fall back to any first file)
    py_files = [f for f in changed_files if f.endswith(".py")]
    primary_file = py_files[0] if py_files else changed_files[0]
    stale_files = [f for f in changed_files if f != primary_file]

    # Apply only the primary file's diff hunks
    success = _apply_diff_partial(wt_dir, repo_path, chosen_commit, [primary_file])

    return FailureState(
        failure_kind=FailureKind.UNDER_SCOPED,
        base_commit=parent_commit,
        worktree_path=wt_dir,
        touched_files=[primary_file],
        stale_files=stale_files,
        mutation_description=(
            f"Applied only '{primary_file}' from {chosen_commit[:8]}; "
            f"{len(stale_files)} dependent file(s) left stale."
        ),
        failure_class=FailureClass.SYMBOL_OUTSIDE_TOUCHED,
        repo_path=repo_path,
        metadata={
            "strategy": "under_scoped",
            "source_commit": chosen_commit,
            "parent_commit": parent_commit,
            "patch_applied": success,
            "all_changed_files": changed_files,
        },
    )


def generate_repairable_failure(
    repo_path: Path,
    base_commit: Optional[str] = None,
    mutation_kind: Optional[MutationKind] = None,
    worktree_base_dir: Optional[Path] = None,
) -> Optional[FailureState]:
    """
    Generate a repairable failure state by applying a mutation operator.

    Strategy:
    1. Create a worktree at HEAD (or specified commit).
    2. Iterate mutation operators until one produces a detectable change.
    3. Write mutated source back to worktree; leave the worktree in broken state.

    Produces assertion_local or syntax_or_lint failures.
    """
    repo_path = Path(repo_path).resolve()

    # Resolve base commit
    if base_commit is None:
        result = _run_git(["rev-parse", "HEAD"], cwd=str(repo_path), check=False)
        base_commit = result.stdout.strip() if result.returncode == 0 else "HEAD"

    # Create worktree
    wt_dir = worktree_base_dir or (repo_path.parent / f"bp_wt_rep_{int(time.time())}")
    wt_dir = Path(wt_dir)
    wt_dir.mkdir(parents=True, exist_ok=True)
    if not _create_worktree(repo_path, base_commit, wt_dir):
        return None

    # Collect Python files in the worktree (limit to top-level package)
    py_files = sorted(
        str(p.relative_to(wt_dir))
        for p in wt_dir.rglob("*.py")
        if ".git" not in str(p) and "test" not in str(p).lower() and p.is_file()
    )[:30]

    # Try each mutation operator until one sticks
    tried_kinds = (
        [mutation_kind] if mutation_kind
        else [MutationKind.OFF_BY_ONE, MutationKind.WRONG_COMPARISON,
              MutationKind.MISSING_RETURN, MutationKind.SWAPPED_ARGS]
    )

    for kind in tried_kinds:
        mutated_files, description = _apply_mutation(wt_dir, py_files, kind)
        if mutated_files and description:
            # Determine resulting failure class
            fc = (
                FailureClass.SYNTAX_OR_LINT
                if kind == MutationKind.MISSING_RETURN
                else FailureClass.ASSERTION_LOCAL
            )
            return FailureState(
                failure_kind=FailureKind.REPAIRABLE,
                base_commit=base_commit,
                worktree_path=wt_dir,
                touched_files=mutated_files,
                stale_files=[],
                mutation_description=description,
                failure_class=fc,
                repo_path=repo_path,
                metadata={
                    "strategy": "repairable",
                    "mutation_kind": kind.value,
                    "files_considered": len(py_files),
                },
            )

    logger.warning("No mutation operator produced a change on %s", repo_path)
    _cleanup_worktree(repo_path, wt_dir)
    return None


def generate_capability_gap_failure(
    repo_path: Path,
    base_commit: Optional[str] = None,
    complexity_threshold: int = 5,
    worktree_base_dir: Optional[Path] = None,
) -> Optional[FailureState]:
    """
    Generate a capability_gap failure state.

    Strategy:
    1. Find a "complex" historical commit — defined as touching >= complexity_threshold files
       with substantial diff size (> 50 added lines).
    2. Create a worktree at the commit's parent (pre-change state).
    3. Leave the worktree entirely unmodified — the agent must solve the full commit from scratch.

    This simulates tasks where the cheap model tier is unlikely to succeed without escalation.
    Produces capability_gap failure class.
    """
    repo_path = Path(repo_path).resolve()

    if base_commit:
        all_files = _get_commit_files(repo_path, base_commit)
        chosen_commit = base_commit
        changed_files = all_files
    else:
        # Find complex commits by diff size
        candidates = _get_recent_multi_file_commits(repo_path, min_files=complexity_threshold, limit=100)
        if not candidates:
            # Fall back to any multi-file commit
            candidates = _get_recent_multi_file_commits(repo_path, min_files=2, limit=100)
        if not candidates:
            logger.warning("No suitable commits found for capability_gap failure in %s", repo_path)
            return None

        # Pick commit with most files changed
        candidates.sort(key=lambda x: len(x[1]), reverse=True)
        chosen_commit, changed_files = candidates[0]

    # Check diff size (added lines)
    stat_result = _run_git(
        ["show", "--stat", "--format=", chosen_commit],
        cwd=str(repo_path),
        check=False,
    )
    stat_text = stat_result.stdout if stat_result.returncode == 0 else ""
    insertions = sum(
        int(m.group(1)) for m in re.finditer(r"(\d+) insertion", stat_text)
    )

    # Identify parent
    parent_result = _run_git(
        ["rev-parse", f"{chosen_commit}^"],
        cwd=str(repo_path),
        check=False,
    )
    parent_commit = parent_result.stdout.strip() if parent_result.returncode == 0 else chosen_commit

    # Create worktree at parent (pre-change — agent must work from scratch)
    wt_dir = worktree_base_dir or (repo_path.parent / f"bp_wt_cap_{int(time.time())}")
    wt_dir = Path(wt_dir)
    wt_dir.mkdir(parents=True, exist_ok=True)
    if not _create_worktree(repo_path, parent_commit, wt_dir):
        return None

    return FailureState(
        failure_kind=FailureKind.CAPABILITY_GAP,
        base_commit=parent_commit,
        worktree_path=wt_dir,
        touched_files=[],
        stale_files=changed_files,
        mutation_description=(
            f"Worktree at {parent_commit[:8]}, before complex commit {chosen_commit[:8]} "
            f"({len(changed_files)} files, ~{insertions} insertions). Agent must solve from scratch."
        ),
        failure_class=FailureClass.CAPABILITY_GAP,
        repo_path=repo_path,
        metadata={
            "strategy": "capability_gap",
            "source_commit": chosen_commit,
            "parent_commit": parent_commit,
            "changed_files_count": len(changed_files),
            "insertions": insertions,
            "complexity_threshold": complexity_threshold,
        },
    )


# ---------------------------------------------------------------------------
# Serialization helpers for --failures DIR storage
# ---------------------------------------------------------------------------

def save_failure_states(states: List[FailureState], directory: Path) -> None:
    """
    Persist a list of FailureState objects to a JSON file in `directory`.
    Creates the directory if it does not exist.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    out_path = directory / "failure_states.json"
    payload = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(states),
        "states": [s.to_dict() for s in states],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    logger.info("Saved %d failure states to %s", len(states), out_path)


def load_failure_states(directory: Path) -> List[FailureState]:
    """
    Load FailureState objects from `directory/failure_states.json`.
    Returns empty list if file not found.
    """
    directory = Path(directory)
    in_path = directory / "failure_states.json"
    if not in_path.exists():
        logger.warning("No failure_states.json found in %s", directory)
        return []
    with open(in_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return [FailureState.from_dict(s) for s in payload.get("states", [])]

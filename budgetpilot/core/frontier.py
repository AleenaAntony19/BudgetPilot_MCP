"""
BudgetPilot Dependency Frontier Builder.
Phase G: Discovers candidate files required for scope expansion:
- Traceback frames outside touched files
- Symbol definition and reference files via ripgrep (rg)
- Associated test suites
- Scope level increment (+1) and resource budget expansion.

Strict Rule: This module must NOT import ollama, mcp, or fastapi.
"""

from dataclasses import asdict, dataclass, field
import logging
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from budgetpilot.core.types import RecoveryContext

logger = logging.getLogger(__name__)

# Scope resource configurations: (budget_tokens, max_read_files)
SCOPE_RESOURCES: Dict[int, Tuple[int, int]] = {
    1: (3000, 3),
    2: (8000, 8),
    3: (20000, 25),
}


@dataclass
class DependencyFrontier:
    """
    Candidate files and expanded resources assembled for expand_scope recovery action.
    """
    traceback_files: List[str]
    symbol_files: List[str]
    related_test_files: List[str]
    frontier_files: List[str]
    previous_scope: int
    new_scope: int
    new_budget_tokens: int
    new_max_read_files: int
    directive: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def __str__(self) -> str:
        return self.directive

    def __contains__(self, item: str) -> bool:
        return item in self.directive or item in self.frontier_files


def apply_frontier_to_state(frontier: DependencyFrontier, state: Any) -> None:
    """
    Apply dependency frontier updates to a TaskState object:
    bumping scope level and expanding allowed read and token budget.
    """
    if state is None:
        return
    if hasattr(state, "metadata") and isinstance(state.metadata, dict):
        state.metadata["current_scope"] = frontier.new_scope
        state.metadata["scope"] = frontier.new_scope
        state.metadata["budget_tokens"] = frontier.new_budget_tokens
        state.metadata["max_read_files"] = frontier.new_max_read_files
    if hasattr(state, "named_files") and isinstance(state.named_files, set):
        state.named_files.update(frontier.frontier_files)


def _normalize_relative_path(path: str, root: Path) -> Optional[str]:
    """Normalize a path string to a relative path from repository root if it exists."""
    if not path:
        return None
    p = Path(path).expanduser()
    if p.is_absolute():
        try:
            rel = p.resolve().relative_to(root.resolve())
            return str(rel)
        except ValueError:
            return None
    candidate = root / p
    if candidate.exists():
        return str(p)
    return str(p)


def build_dependency_frontier(
    ctx: RecoveryContext,
    repo_root: Union[str, Path] = ".",
    current_scope: int = 1,
    max_symbols: int = 5,
    timeout_seconds: float = 1.0,
) -> DependencyFrontier:
    """
    Build the dependency frontier for scope expansion:
    1. Extracts frame paths from outside_frames and error traceback.
    2. Runs ripgrep to find symbol definition/reference files for unread symbols.
    3. Identifies related test files for touched components.
    4. Bumps scope level (+1, max 3) and returns expanded resource bounds.
    """
    root = Path(repo_root).expanduser().resolve()
    touched_norm = {Path(t).name for t in ctx.files_touched}

    traceback_files: List[str] = []
    symbol_files: List[str] = []
    related_test_files: List[str] = []

    # 1. Traceback files outside touched set
    raw_frames = list(ctx.outside_frames)
    # Also extract any file paths directly from stderr
    stderr_matches = re.findall(
        r'\b(?:[a-zA-Z0-9_\-\.\/]+\/)?([a-zA-Z0-9_\-]+\.(?:py|js|ts|json|yaml|yml|md))\b',
        ctx.stderr or "",
    )
    for m in stderr_matches:
        raw_frames.append(m)

    seen_tb: Set[str] = set()
    for item in raw_frames:
        # e.g.: "src/utils.py:25:in helper" -> "src/utils.py"
        file_part = item.split(":")[0].strip()
        if not file_part or file_part in seen_tb:
            continue
        norm = _normalize_relative_path(file_part, root)
        if norm and Path(norm).name not in touched_norm:
            traceback_files.append(norm)
            seen_tb.add(file_part)

    # 2. Symbol reference files via ripgrep (rg)
    candidate_symbols = list(ctx.unread_symbols)
    if not candidate_symbols and ctx.error_message:
        extracted = re.findall(r"['\"]([a-zA-Z_]\w+)['\"]", ctx.error_message)
        candidate_symbols.extend([s for s in extracted if len(s) >= 3])

    rg_bin = shutil.which("rg")
    seen_sym_files: Set[str] = set()

    if rg_bin and root.exists():
        for sym in candidate_symbols[:max_symbols]:
            try:
                cmd = [rg_bin, "-l", "--max-count", "5", sym, str(root)]
                proc = subprocess.run(
                    cmd,
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
                if proc.returncode in (0, 1):
                    for line in proc.stdout.splitlines():
                        match_path = line.strip()
                        if match_path and match_path not in seen_sym_files:
                            norm_sym = _normalize_relative_path(match_path, root)
                            if norm_sym and Path(norm_sym).name not in touched_norm:
                                symbol_files.append(norm_sym)
                                seen_sym_files.add(match_path)
            except Exception as exc:
                logger.debug("Frontier ripgrep failed for symbol '%s': %s", sym, exc)

    # 3. Related test files
    tests_dir = root / "tests"
    for touched in ctx.files_touched:
        base_name = Path(touched).stem
        if tests_dir.exists():
            candidates = [
                f"tests/test_{base_name}.py",
                f"tests/{base_name}_test.py",
                f"tests/test_{base_name}.js",
                f"tests/test_{base_name}.ts",
            ]
            for cand in candidates:
                if (root / cand).exists() and cand not in related_test_files:
                    related_test_files.append(cand)

    # 4. Compile deduplicated frontier
    frontier_files: List[str] = []
    seen_frontier: Set[str] = set()

    for f in (traceback_files + symbol_files + related_test_files):
        if f not in seen_frontier and Path(f).name not in touched_norm:
            frontier_files.append(f)
            seen_frontier.add(f)

    # 5. Expand Scope and Resources
    new_scope = min(3, max(1, current_scope + 1))
    new_tokens, new_reads = SCOPE_RESOURCES[new_scope]

    files_list_str = "\n".join(f"- {f}" for f in frontier_files) if frontier_files else "- (No additional external files discovered)"
    directive = (
        f"[BUDGETPILOT RECOVERY DIRECTIVE: EXPAND SCOPE (Level {current_scope} -> {new_scope})]\n"
        f"Verification failed due to dependencies outside current touched scope ({ctx.failure_class.value}).\n"
        f"Dependency frontier files identified for exploration:\n{files_list_str}\n"
        f"Expanded limits: max_read_files={new_reads}, token_budget={new_tokens}.\n"
        f"Instructions: Read the frontier files to understand external contracts, then update your implementation."
    )

    return DependencyFrontier(
        traceback_files=traceback_files,
        symbol_files=symbol_files,
        related_test_files=related_test_files,
        frontier_files=frontier_files,
        previous_scope=current_scope,
        new_scope=new_scope,
        new_budget_tokens=new_tokens,
        new_max_read_files=new_reads,
        directive=directive,
    )

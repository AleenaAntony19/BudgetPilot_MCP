"""
Scope limits: the one table every component reads.

The estimator, the dependency frontier, the gate and the hook engine all take
their limits from here. Users can override any value under `scope_limits` in
`$BP_HOME/config.json`, for example:

    {"scope_limits": {"1": {"read_window_lines": 300, "context_budget_tokens": 8000}}}

The defaults are starting points. They are meant to be re-tuned from paired
A/B results (see docs/RESULTS.md), not treated as proven optima.

Standard library only.
"""

from dataclasses import dataclass, fields, replace
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class ScopeLimits:
    scope: int
    max_files_outside_named: int        # reads of files NOT named in the prompt or a traceback
    read_window_lines: Optional[int]    # None = unlimited
    list_depth: Optional[int]           # None = unlimited
    allow_repo_wide_search: bool
    allow_full_suite_tests: bool
    grep_head_limit: int                # cap on Grep result lines
    edit_max_new_lines: Optional[int]   # per Edit/Write call on an existing file
    context_budget_tokens: int          # new tokens the task may add through tool results
    output_budget_tokens: int           # tokens the model may emit this task (incl. thinking)


SCOPE_LIMITS: Dict[int, ScopeLimits] = {
    1: ScopeLimits(1, 3, 400, 1, False, False, 50, 60, 6_000, 1_500),
    2: ScopeLimits(2, 8, 800, 2, False, False, 120, 150, 16_000, 4_000),
    3: ScopeLimits(3, 25, None, None, True, True, 250, 400, 40_000, 10_000),
}

MIN_SCOPE, MAX_SCOPE = 1, 3
_FIELD_NAMES = {f.name for f in fields(ScopeLimits)} - {"scope"}


def clamp_scope(scope: Any) -> int:
    try:
        value = int(scope)
    except (TypeError, ValueError):
        value = MIN_SCOPE
    return max(MIN_SCOPE, min(MAX_SCOPE, value))


def get_limits(scope: Any, config: Optional[Dict[str, Any]] = None) -> ScopeLimits:
    """Limits for a scope, with `scope_limits` overrides from config.json applied."""
    level = clamp_scope(scope)
    base = SCOPE_LIMITS[level]
    if config is None:
        from budgetpilot.core.state import read_config

        config = read_config()
    overrides = (config.get("scope_limits") or {}).get(str(level))
    if not isinstance(overrides, dict):
        return base
    clean = {k: v for k, v in overrides.items() if k in _FIELD_NAMES}
    try:
        return replace(base, **clean)
    except TypeError:
        return base


def scope_resources(scope: Any) -> Tuple[int, int]:
    """(context token budget, max unnamed file reads): the pair the estimator reports."""
    limits = get_limits(scope)
    return limits.context_budget_tokens, limits.max_files_outside_named


def limits_table() -> Dict[str, Dict[str, Any]]:
    """Effective limits for all scopes, keyed "1".."3" (for docs, doctor and the dashboard)."""
    out: Dict[str, Dict[str, Any]] = {}
    for level in SCOPE_LIMITS:
        lim = get_limits(level)
        out[str(level)] = {name: getattr(lim, name) for name in sorted(_FIELD_NAMES)}
    return out

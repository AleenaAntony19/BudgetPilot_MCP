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

from __future__ import annotations

from collections import namedtuple

# An immutable record (namedtuple rather than a frozen dataclass: importing
# `dataclasses` costs tens of milliseconds, and the hook process pays its
# start-up time on every tool call).
#
#   scope                    1, 2 or 3
#   max_files_outside_named  reads of files NOT named in the prompt or a traceback
#   read_window_lines        None = unlimited
#   list_depth               None = unlimited
#   allow_repo_wide_search
#   allow_full_suite_tests
#   grep_head_limit          cap on Grep result lines
#   edit_max_new_lines       per Edit/Write call on an existing file
#   context_budget_tokens    new tokens the task may add through tool results
#   output_budget_tokens     tokens the model may emit this task (incl. thinking)
ScopeLimits = namedtuple(
    "ScopeLimits",
    "scope max_files_outside_named read_window_lines list_depth allow_repo_wide_search "
    "allow_full_suite_tests grep_head_limit edit_max_new_lines context_budget_tokens output_budget_tokens",
)


SCOPE_LIMITS: dict[int, ScopeLimits] = {
    1: ScopeLimits(1, 3, 400, 1, False, False, 50, 60, 6_000, 1_500),
    2: ScopeLimits(2, 8, 800, 2, False, False, 120, 150, 16_000, 4_000),
    3: ScopeLimits(3, 25, None, None, True, True, 250, 400, 40_000, 10_000),
}

MIN_SCOPE, MAX_SCOPE = 1, 3
_FIELD_NAMES = set(ScopeLimits._fields) - {"scope"}


def clamp_scope(scope: object) -> int:
    try:
        value = int(scope)
    except (TypeError, ValueError):
        value = MIN_SCOPE
    return max(MIN_SCOPE, min(MAX_SCOPE, value))


def get_limits(scope: object, config: dict | None = None) -> ScopeLimits:
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
        return base._replace(**clean)
    except (TypeError, ValueError):
        return base


def scope_resources(scope: object) -> tuple[int, int]:
    """(context token budget, max unnamed file reads): the pair the estimator reports."""
    limits = get_limits(scope)
    return limits.context_budget_tokens, limits.max_files_outside_named


def limits_table() -> dict:
    """Effective limits for all scopes, keyed "1".."3" (for docs, doctor and the dashboard)."""
    out: dict = {}
    for level in SCOPE_LIMITS:
        lim = get_limits(level)
        out[str(level)] = {name: getattr(lim, name) for name in sorted(_FIELD_NAMES)}
    return out

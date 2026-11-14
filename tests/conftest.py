"""
Shared test configuration.

Isolation guard: every test runs with BP_HOME pointed at a fresh temporary
directory, so no test can read or mutate the developer's real ~/.budgetpilot
(bp.db, config.json, prices.json, fallback.jsonl, worktrees). Tests that need a
specific BP_HOME may still override it with monkeypatch.setenv.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_bp_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("bp_home")
    monkeypatch.setenv("BP_HOME", str(home))
    monkeypatch.delenv("BP_HALT", raising=False)
    yield home

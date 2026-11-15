"""Mode/state single source of truth, the scope table, and schema migration 4."""

import json
import sqlite3
import subprocess
import sys

import pytest

from budgetpilot.config import ExecutionMode, load_config, save_config
from budgetpilot.core import scope, state
from budgetpilot.core.store import HOOK_TABLES, LATEST_SCHEMA_VERSION, get_current_schema_version, init_db


# ---------------------------------------------------------------- state / mode
def test_default_mode_is_off_everywhere(monkeypatch):
    monkeypatch.delenv("BP_MODE", raising=False)
    assert state.get_mode() == "off"
    assert load_config().mode == ExecutionMode.OFF


def test_set_mode_is_shared_by_config_and_state(monkeypatch):
    monkeypatch.delenv("BP_MODE", raising=False)
    assert state.set_mode("on") == "enforce"
    assert state.get_mode() == "enforce"
    assert load_config().mode == ExecutionMode.ENFORCE
    state.set_mode("observe")
    assert load_config().mode == ExecutionMode.OBSERVE
    with pytest.raises(ValueError):
        state.set_mode("turbo")


def test_env_takes_precedence_over_config(monkeypatch):
    state.set_mode("off")
    state.set_budget_slider("loose")
    monkeypatch.setenv("BP_MODE", "enforce")
    monkeypatch.setenv("BP_BUDGET_SLIDER", "tight")
    assert state.get_mode() == "enforce" and state.env_overrides_mode()
    assert state.get_budget_slider() == "tight"
    cfg = load_config()
    assert cfg.mode == ExecutionMode.ENFORCE and cfg.budget_slider == "tight"


def test_hitl_env(monkeypatch):
    monkeypatch.delenv("BP_HITL", raising=False)
    assert state.hitl_enabled() is True
    monkeypatch.setenv("BP_HITL", "0")
    assert state.hitl_enabled() is False


def test_pydantic_save_keeps_unknown_keys(monkeypatch):
    monkeypatch.delenv("BP_MODE", raising=False)
    state.update_config(scope_limits={"1": {"read_window_lines": 300}}, hitl=False)
    cfg = load_config()
    cfg.budget_slider = "tight"
    save_config(cfg)
    raw = state.read_config()
    assert raw["scope_limits"] == {"1": {"read_window_lines": 300}}
    assert raw["hitl"] is False and raw["budget_slider"] == "tight"


def test_corrupt_config_falls_back_to_off(monkeypatch):
    monkeypatch.delenv("BP_MODE", raising=False)
    state.config_path().write_text("{not json")
    assert state.get_mode() == "off"


def test_state_module_is_stdlib_only():
    code = (
        "import sys; import budgetpilot.core.state, budgetpilot.core.scope; "
        "bad=[m for m in ('pydantic','mcp','ollama','sqlite3','yaml') if m in sys.modules]; print(bad)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


# ---------------------------------------------------------------- scope table
def test_scope_table_values():
    s1, s2, s3 = (scope.get_limits(i, config={}) for i in (1, 2, 3))
    assert (s1.max_files_outside_named, s2.max_files_outside_named, s3.max_files_outside_named) == (3, 8, 25)
    assert (s1.read_window_lines, s2.read_window_lines, s3.read_window_lines) == (400, 800, None)
    assert (s1.grep_head_limit, s2.grep_head_limit, s3.grep_head_limit) == (50, 120, 250)
    assert (s1.edit_max_new_lines, s2.edit_max_new_lines, s3.edit_max_new_lines) == (60, 150, 400)
    assert (s1.context_budget_tokens, s2.context_budget_tokens, s3.context_budget_tokens) == (6000, 16000, 40000)
    assert (s1.output_budget_tokens, s2.output_budget_tokens, s3.output_budget_tokens) == (1500, 4000, 10000)
    assert not s1.allow_full_suite_tests and s3.allow_full_suite_tests and s3.allow_repo_wide_search


def test_scope_is_clamped_and_overridable():
    assert scope.get_limits(0, config={}).scope == 1
    assert scope.get_limits(9, config={}).scope == 3
    assert scope.get_limits("junk", config={}).scope == 1
    cfg = {"scope_limits": {"1": {"read_window_lines": 250, "not_a_field": 1}}}
    assert scope.get_limits(1, config=cfg).read_window_lines == 250
    assert scope.get_limits(2, config=cfg).read_window_lines == 800


def test_every_component_reads_the_same_table():
    from budgetpilot.core import estimator, frontier

    assert estimator.SCOPE_RESOURCES == frontier.SCOPE_RESOURCES == {1: (6000, 3), 2: (16000, 8), 3: (40000, 25)}
    assert load_config().limits.max_read_files_per_scope == {"scope_1": 3, "scope_2": 8, "scope_3": 25}


# ---------------------------------------------------------------- migrations
def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_database_gets_hook_tables(tmp_path):
    conn = init_db(tmp_path / "fresh.db")
    try:
        assert get_current_schema_version(conn) == LATEST_SCHEMA_VERSION == 4
        assert set(HOOK_TABLES) <= _tables(conn)
        assert "tasks" not in _tables(conn) and "budget_reservations" not in _tables(conn)
    finally:
        conn.close()


@pytest.mark.parametrize("legacy_versions", [[2], [2, 3]])
def test_database_with_legacy_control_plane_schema_migrates(tmp_path, legacy_versions):
    """A user DB that already has the dropped control-plane migrations applied."""
    path = tmp_path / "legacy.db"
    conn = init_db(path)
    conn.close()
    raw = sqlite3.connect(path)
    raw.execute("DELETE FROM schema_version WHERE version > 1")
    for name in HOOK_TABLES:
        raw.execute(f"DROP TABLE IF EXISTS {name}")
    raw.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY)")
    raw.execute("CREATE TABLE budget_reservations (reservation_id TEXT PRIMARY KEY)")
    raw.execute("ALTER TABLE model_calls ADD COLUMN model_call_id TEXT")
    for v in legacy_versions:
        raw.execute("INSERT INTO schema_version (version, description) VALUES (?, 'legacy control plane')", (v,))
    raw.execute("INSERT INTO tasks VALUES ('keep-me')")
    raw.commit()
    raw.close()

    conn = init_db(path)
    try:
        assert get_current_schema_version(conn) == 4
        assert set(HOOK_TABLES) <= _tables(conn)
        assert conn.execute("SELECT task_id FROM tasks").fetchone()[0] == "keep-me"  # legacy data untouched
    finally:
        conn.close()
    init_db(path).close()  # idempotent

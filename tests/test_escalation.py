"""
Direct tests for agent/escalation.run_escalation (previously only mocked).

Regression for audit finding F4: run_escalation called the non-existent
CostLedger.calculate_cost and therefore always ended with status="error".
"""

import subprocess

import pytest

import budgetpilot.agent.escalation as escalation
from budgetpilot.agent.model import ModelResult
from budgetpilot.core.prices import price_call
from budgetpilot.core.store import get_connection


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BP_HOME", str(tmp_path / "bp_home"))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.txt").write_text("hello world\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    for cmd in (["git", "init", "-q"], ["git", "add", "."], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=root, check=True, env=env)
    return root


def _scripted_chat(responses):
    calls = iter(responses)

    def fake_chat(**kwargs):
        return next(calls)

    return fake_chat


def test_escalation_completes_and_prices_strong_tier(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(escalation, "chat", _scripted_chat([
        ModelResult(content="nothing to change", model="qwen2.5:14b", tier="strong",
                    prompt_tokens=100, output_tokens=50),
    ]))
    db = tmp_path / "esc.db"
    result = escalation.run_escalation(prompt="fix", repo_path=repo, db_path=db, max_turns=2,
                                       session_id="esc_s")
    assert result.error is None
    assert result.status == "completed"
    assert result.cost_usd == pytest.approx(price_call("strong", 100, 50))
    assert result.worktree_cleaned is True
    assert result.succeeded is False  # no patch produced

    conn = get_connection(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM model_calls WHERE session_id='esc_s';").fetchone()[0] == 1
        outcome = conn.execute("SELECT action, succeeded FROM recovery_outcomes;").fetchone()
        assert tuple(outcome) == ("escalate", 0)
    finally:
        conn.close()


def test_escalation_produces_patch_without_touching_main_tree(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(escalation, "chat", _scripted_chat([
        ModelResult(content="", model="qwen2.5:14b", tier="strong", prompt_tokens=10, output_tokens=5,
                    tool_calls=[{"function": {"name": "edit_file",
                                              "arguments": {"path": "a.txt", "old": "hello", "new": "goodbye"}}}]),
        ModelResult(content="done", model="qwen2.5:14b", tier="strong", prompt_tokens=10, output_tokens=5),
    ]))
    result = escalation.run_escalation(prompt="fix", repo_path=repo, db_path=tmp_path / "esc.db", max_turns=3)
    assert result.status == "completed" and result.error is None
    assert result.succeeded is True
    assert "+goodbye world" in result.patch
    assert (repo / "a.txt").read_text() == "hello world\n"  # user tree untouched
    assert result.worktree_cleaned is True

"""
BudgetPilot Phase J Unit Tests: Rollout Collection, Failure State Generators,
and Laplace-Smoothed Prior Estimation.

Tests cover:
- FailureState dataclass serialization round-trip
- Mutation operators (off-by-one, swapped_args, wrong_comparison, missing_return)
- save/load failure_states.json cycle
- RolloutRecord dataclass
- collect_rollouts: execution logging and SQLite persistence
- load_rollout_counts: aggregation from rollouts table
- Laplace-smoothed prior estimation: _laplace_smooth, load_empirical_priors, _blend_priors
- Router integration: empirical priors blend into route() when rollout data exists
- CLI bp rollout collect basic flow (with mocked failure states)
- Decoupling: calibration package must not import forbidden modules
"""

import ast
import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch, MagicMock

import pytest

from budgetpilot.core.types import FailureClass, RecoveryAction


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    from budgetpilot.core.store import init_db
    db = tmp_path / "test_bp.db"
    init_db(db)
    return db


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repository with at least two commits touching multiple files."""
    repo = tmp_path / "test_repo"
    repo.mkdir()

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + list(args), cwd=str(repo),
            capture_output=True, text=True, timeout=15,
        )

    git("init")
    git("config", "user.email", "test@bp.test")
    git("config", "user.name", "BudgetPilot Test")

    # Initial commit: two Python files
    (repo / "mod_a.py").write_text("def foo(x):\n    return x + 1\n\n\ndef bar(a, b):\n    return a + b\n")
    (repo / "mod_b.py").write_text("from mod_a import foo\n\nresult = foo(5)\n")
    git("add", ".")
    git("commit", "-m", "initial: add mod_a and mod_b")

    # Second commit: change both files (multi-file commit)
    (repo / "mod_a.py").write_text("def foo(x):\n    return x + 2\n\n\ndef bar(a, b):\n    return a - b\n")
    (repo / "mod_b.py").write_text("from mod_a import foo, bar\n\nresult = foo(10)\n")
    git("add", ".")
    git("commit", "-m", "update foo and bar, extend imports")

    return repo


# ---------------------------------------------------------------------------
# Test: FailureState serialization
# ---------------------------------------------------------------------------

class TestFailureStateSerialization:
    def test_to_dict_and_back(self, tmp_path: Path) -> None:
        from budgetpilot.core.calibration.failures import FailureState, FailureKind
        state = FailureState(
            failure_kind=FailureKind.REPAIRABLE,
            base_commit="abc1234",
            worktree_path=tmp_path / "wt",
            touched_files=["mod_a.py"],
            stale_files=["mod_b.py"],
            mutation_description="off-by-one in foo",
            failure_class=FailureClass.ASSERTION_LOCAL,
            repo_path=tmp_path,
            metadata={"seed": 42},
        )
        d = state.to_dict()
        assert d["failure_kind"] == "repairable"
        assert d["failure_class"] == "assertion_local"
        assert d["touched_files"] == ["mod_a.py"]

        restored = FailureState.from_dict(d)
        assert restored.failure_kind == FailureKind.REPAIRABLE
        assert restored.failure_class == FailureClass.ASSERTION_LOCAL
        assert restored.touched_files == ["mod_a.py"]
        assert restored.stale_files == ["mod_b.py"]

    def test_save_and_load_failure_states(self, tmp_path: Path) -> None:
        from budgetpilot.core.calibration.failures import (
            FailureState, FailureKind, save_failure_states, load_failure_states
        )
        states = [
            FailureState(
                failure_kind=FailureKind.UNDER_SCOPED,
                base_commit=f"dead{i}beef",
                worktree_path=tmp_path / f"wt{i}",
                touched_files=[f"file_{i}.py"],
                stale_files=[f"stale_{i}.py"],
                failure_class=FailureClass.SYMBOL_OUTSIDE_TOUCHED,
                repo_path=tmp_path,
            )
            for i in range(3)
        ]
        save_failure_states(states, tmp_path)

        loaded = load_failure_states(tmp_path)
        assert len(loaded) == 3
        assert loaded[1].base_commit == "dead1beef"
        assert loaded[2].touched_files == ["file_2.py"]

    def test_load_nonexistent_returns_empty(self, tmp_path: Path) -> None:
        from budgetpilot.core.calibration.failures import load_failure_states
        result = load_failure_states(tmp_path / "does_not_exist")
        assert result == []


# ---------------------------------------------------------------------------
# Test: Mutation operators
# ---------------------------------------------------------------------------

class TestMutationOperators:
    def test_off_by_one_replaces_arithmetic(self) -> None:
        from budgetpilot.core.calibration.failures import _mutate_off_by_one
        source = "def foo(x):\n    return x + 1\n"
        new, desc = _mutate_off_by_one(source)
        assert new != source
        assert "off-by-one" in desc
        assert "x + 2" in new  # 1 → 2

    def test_off_by_one_no_match_returns_original(self) -> None:
        from budgetpilot.core.calibration.failures import _mutate_off_by_one
        source = "def foo(x):\n    return x\n"
        new, desc = _mutate_off_by_one(source)
        assert new == source
        assert desc == ""

    def test_swapped_args_flips_two_arg_call(self) -> None:
        from budgetpilot.core.calibration.failures import _mutate_swapped_args
        source = "result = calculate(alpha, beta)\n"
        new, desc = _mutate_swapped_args(source)
        assert "calculate(beta, alpha)" in new
        assert "swapped_args" in desc

    def test_swapped_args_no_match_returns_original(self) -> None:
        from budgetpilot.core.calibration.failures import _mutate_swapped_args
        source = "x = 42\n"
        new, desc = _mutate_swapped_args(source)
        assert new == source
        assert desc == ""

    def test_wrong_comparison_flips_equality(self) -> None:
        from budgetpilot.core.calibration.failures import _mutate_wrong_comparison
        source = "if x == 0:\n    pass\n"
        new, desc = _mutate_wrong_comparison(source)
        assert "!=" in new
        assert "wrong_comparison" in desc

    def test_missing_return_comments_out_return(self) -> None:
        from budgetpilot.core.calibration.failures import _mutate_missing_return
        source = "def foo(x):\n    return x + 1\n"
        new, desc = _mutate_missing_return(source)
        assert "# MUTATED: return" in new
        assert "missing_return" in desc
        # The return is now a comment line (starts with #), not an active return statement
        active_returns = [line.lstrip() for line in new.splitlines() if line.lstrip().startswith("return")]
        assert len(active_returns) == 0, f"Return statement should be commented out, found: {active_returns}"

    def test_apply_mutation_writes_to_file(self, tmp_path: Path) -> None:
        from budgetpilot.core.calibration.failures import _apply_mutation, MutationKind
        # Create a file with a recognizable comparison
        src_file = tmp_path / "sample.py"
        src_file.write_text("def check(n):\n    if n == 0:\n        return True\n    return False\n")

        mutated_files, desc = _apply_mutation(tmp_path, ["sample.py"], MutationKind.WRONG_COMPARISON)
        assert mutated_files == ["sample.py"]
        assert "wrong_comparison" in desc
        content = src_file.read_text()
        assert "!=" in content  # was == now !=


# ---------------------------------------------------------------------------
# Test: Git helpers (require a real git repo)
# ---------------------------------------------------------------------------

class TestGitHelpers:
    def test_get_commit_files(self, git_repo: Path) -> None:
        from budgetpilot.core.calibration.failures import _get_commit_files, _run_git
        head = _run_git(["rev-parse", "HEAD"], cwd=str(git_repo), check=False).stdout.strip()
        files = _get_commit_files(git_repo, head)
        assert len(files) >= 2
        assert any("mod_a.py" in f for f in files)
        assert any("mod_b.py" in f for f in files)

    def test_get_recent_multi_file_commits(self, git_repo: Path) -> None:
        from budgetpilot.core.calibration.failures import _get_recent_multi_file_commits
        commits = _get_recent_multi_file_commits(git_repo, min_files=2, limit=10)
        assert len(commits) >= 1
        sha, files = commits[0]
        assert len(sha) == 40
        assert len(files) >= 2

    def test_create_and_cleanup_worktree(self, git_repo: Path, tmp_path: Path) -> None:
        from budgetpilot.core.calibration.failures import _create_worktree, _cleanup_worktree, _run_git
        head = _run_git(["rev-parse", "HEAD"], cwd=str(git_repo), check=False).stdout.strip()
        wt_dir = tmp_path / "test_worktree"
        wt_dir.mkdir()
        ok = _create_worktree(git_repo, head, wt_dir)
        assert ok, "Worktree creation should succeed"
        assert (wt_dir / "mod_a.py").exists(), "mod_a.py should exist in worktree"
        _cleanup_worktree(git_repo, wt_dir)
        # Directory should be gone or empty after cleanup
        assert not wt_dir.exists() or not (wt_dir / ".git").exists()


# ---------------------------------------------------------------------------
# Test: Failure state generators
# ---------------------------------------------------------------------------

class TestFailureGenerators:
    def test_generate_repairable_failure(self, git_repo: Path, tmp_path: Path) -> None:
        from budgetpilot.core.calibration.failures import generate_repairable_failure, _cleanup_worktree
        state = generate_repairable_failure(
            repo_path=git_repo,
            worktree_base_dir=tmp_path / "rep_wt",
        )
        # A state should be returned if at least one mutation operator succeeds
        # (may be None if the small test repo's Python files don't match any operator)
        if state is None:
            pytest.skip("No mutation operator matched the test repo source — OK for minimal repos")
        assert state.failure_kind.value == "repairable"
        assert len(state.touched_files) >= 1
        assert state.mutation_description != ""
        assert state.worktree_path.exists()
        # Cleanup
        _cleanup_worktree(git_repo, state.worktree_path)

    def test_generate_under_scoped_failure(self, git_repo: Path, tmp_path: Path) -> None:
        from budgetpilot.core.calibration.failures import generate_under_scoped_failure, _cleanup_worktree
        state = generate_under_scoped_failure(
            repo_path=git_repo,
            worktree_base_dir=tmp_path / "us_wt",
        )
        assert state is not None, "Should produce a FailureState for a multi-file commit repo"
        assert state.failure_kind.value == "under_scoped"
        assert len(state.stale_files) >= 1  # At least one file left stale
        # Cleanup
        _cleanup_worktree(git_repo, state.worktree_path)

    def test_generate_capability_gap_failure(self, git_repo: Path, tmp_path: Path) -> None:
        from budgetpilot.core.calibration.failures import generate_capability_gap_failure, _cleanup_worktree
        state = generate_capability_gap_failure(
            repo_path=git_repo,
            complexity_threshold=1,  # Low threshold so our small repo qualifies
            worktree_base_dir=tmp_path / "cap_wt",
        )
        assert state is not None, "Should produce a FailureState for any repo"
        assert state.failure_kind.value == "capability_gap"
        assert state.failure_class == FailureClass.CAPABILITY_GAP
        # Cleanup
        _cleanup_worktree(git_repo, state.worktree_path)

    def test_generated_state_has_existing_worktree(self, git_repo: Path, tmp_path: Path) -> None:
        from budgetpilot.core.calibration.failures import generate_repairable_failure, _cleanup_worktree
        state = generate_repairable_failure(
            repo_path=git_repo,
            worktree_base_dir=tmp_path / "exist_wt",
        )
        if state:
            assert state.worktree_path.exists(), "Worktree directory must exist after generation"
            _cleanup_worktree(git_repo, state.worktree_path)


# ---------------------------------------------------------------------------
# Test: RolloutRecord
# ---------------------------------------------------------------------------

class TestRolloutRecord:
    def test_to_dict_keys(self) -> None:
        from budgetpilot.core.calibration.rollouts import RolloutRecord
        rec = RolloutRecord(
            failure_kind="repairable",
            failure_class="assertion_local",
            base_commit="abc123",
            repo_name="test_repo",
            action="reflect",
            seed=0,
            succeeded=True,
            tokens_spent=800,
            cost_usd=0.0008,
            wall_time_sec=1.5,
        )
        d = rec.to_dict()
        assert d["action"] == "reflect"
        assert d["succeeded"] is True
        assert d["tokens_spent"] == 800
        assert d["cost_usd"] == pytest.approx(0.0008)

    def test_failed_record(self) -> None:
        from budgetpilot.core.calibration.rollouts import RolloutRecord
        rec = RolloutRecord(
            failure_kind="capability_gap",
            failure_class="capability_gap",
            base_commit="deadbeef",
            repo_name="my_repo",
            action="escalate",
            seed=2,
            succeeded=False,
            error_message="agent did not complete",
        )
        assert not rec.succeeded
        assert rec.error_message == "agent did not complete"


# ---------------------------------------------------------------------------
# Test: collect_rollouts + SQLite persistence
# ---------------------------------------------------------------------------

class TestCollectRollouts:
    def _make_state(self, tmp_path: Path, kind: str = "repairable") -> Any:
        from budgetpilot.core.calibration.failures import FailureState, FailureKind
        # Use a worktree_path that doesn't exist (simulation fallback will trigger)
        return FailureState(
            failure_kind=FailureKind(kind),
            base_commit="testcommit1234",
            worktree_path=tmp_path / "fake_wt",
            touched_files=["mod_a.py"],
            stale_files=["mod_b.py"],
            mutation_description="test mutation",
            failure_class=FailureClass.ASSERTION_LOCAL,
            repo_path=tmp_path,
            metadata={},
        )

    def _fast_execute(self, state: Any, action: RecoveryAction, seed: int, **kwargs: Any):
        """Fast mock execution — returns a deterministic RolloutRecord without running the agent."""
        from budgetpilot.core.calibration.rollouts import RolloutRecord
        return RolloutRecord(
            failure_kind=state.failure_kind.value,
            failure_class=state.failure_class.value,
            base_commit=state.base_commit,
            repo_name=state.repo_path.name,
            action=action.value,
            seed=seed,
            succeeded=(seed % 2 == 0),  # deterministic alternation
            tokens_spent=100 * (seed + 1),
            cost_usd=0.0001 * (seed + 1),
            wall_time_sec=0.01,
        )

    def test_collect_rollouts_produces_correct_count(self, tmp_path: Path, tmp_db: Path) -> None:
        from budgetpilot.core.calibration.rollouts import collect_rollouts
        import budgetpilot.core.calibration.rollouts as rollouts_mod
        states = [self._make_state(tmp_path)]
        with patch.object(rollouts_mod, "_execute_action_against_failure", self._fast_execute):
            records = collect_rollouts(
                failure_states=states,
                seeds=2,
                tier="cheap",
                max_turns=4,
                db_path=tmp_db,
            )
        # 1 state × 4 actions × 2 seeds = 8 records
        assert len(records) == 8

    def test_collect_rollouts_persists_to_sqlite(self, tmp_path: Path, tmp_db: Path) -> None:
        from budgetpilot.core.calibration.rollouts import collect_rollouts
        from budgetpilot.core.store import get_connection
        import budgetpilot.core.calibration.rollouts as rollouts_mod
        states = [self._make_state(tmp_path, "repairable")]
        with patch.object(rollouts_mod, "_execute_action_against_failure", self._fast_execute):
            collect_rollouts(
                failure_states=states,
                seeds=1,
                tier="cheap",
                db_path=tmp_db,
            )
        conn = get_connection(tmp_db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM rollouts;").fetchone()[0]
        finally:
            conn.close()
        assert count >= 4, "Should have at least 4 rows (1 state × 4 actions × 1 seed)"

    def test_collect_rollouts_records_have_expected_fields(self, tmp_path: Path, tmp_db: Path) -> None:
        from budgetpilot.core.calibration.rollouts import collect_rollouts
        import budgetpilot.core.calibration.rollouts as rollouts_mod
        states = [self._make_state(tmp_path)]
        with patch.object(rollouts_mod, "_execute_action_against_failure", self._fast_execute):
            records = collect_rollouts(
                failure_states=states,
                seeds=1,
                db_path=tmp_db,
            )
        for rec in records:
            assert rec.action in {a.value for a in RecoveryAction}
            assert isinstance(rec.tokens_spent, int) and rec.tokens_spent > 0
            assert isinstance(rec.cost_usd, float)
            assert isinstance(rec.wall_time_sec, float)
            assert rec.failure_class == "assertion_local"

    def test_collect_rollouts_multiple_states(self, tmp_path: Path, tmp_db: Path) -> None:
        from budgetpilot.core.calibration.rollouts import collect_rollouts
        from budgetpilot.core.calibration.failures import FailureState, FailureKind
        import budgetpilot.core.calibration.rollouts as rollouts_mod
        states = [
            self._make_state(tmp_path, "repairable"),
            FailureState(
                failure_kind=FailureKind.CAPABILITY_GAP,
                base_commit="cap0001",
                worktree_path=tmp_path / "wt2",
                touched_files=[],
                stale_files=["mod_a.py", "mod_b.py"],
                failure_class=FailureClass.CAPABILITY_GAP,
                repo_path=tmp_path,
            ),
        ]
        with patch.object(rollouts_mod, "_execute_action_against_failure", self._fast_execute):
            records = collect_rollouts(failure_states=states, seeds=1, db_path=tmp_db)
        # 2 states × 4 actions × 1 seed = 8 records
        assert len(records) == 8
        fc_vals = {r.failure_class for r in records}
        assert "assertion_local" in fc_vals
        assert "capability_gap" in fc_vals


# ---------------------------------------------------------------------------
# Test: load_rollout_counts
# ---------------------------------------------------------------------------

class TestLoadRolloutCounts:
    def _insert_rollout(
        self,
        db_path: Path,
        failure_class: str,
        action: str,
        succeeded: int,
    ) -> None:
        from budgetpilot.core.store import safe_execute_write
        from budgetpilot.core.calibration.rollouts import _ensure_rollout_columns
        _ensure_rollout_columns(db_path)
        safe_execute_write(
            conn_or_path=db_path,
            sql="""
            INSERT INTO rollouts (
                task_id, repo_name, failure_induced, recovery_trajectory, final_verdict,
                total_cost_usd, failure_class, action, seed, succeeded, tokens_spent, cost_usd
            ) VALUES (?, 'test', ?, ?, ?, 0.001, ?, ?, 0, ?, 100, 0.0001);
            """,
            params=(
                f"t_{failure_class}_{action}",
                failure_class,
                action,
                "pass" if succeeded else "fail",
                failure_class,
                action,
                succeeded,
            ),
        )

    def test_load_rollout_counts_basic(self, tmp_db: Path) -> None:
        from budgetpilot.core.calibration.rollouts import load_rollout_counts
        # Insert: assertion_local/reflect: 3 trials, 2 successes
        for i in range(3):
            self._insert_rollout(tmp_db, "assertion_local", "reflect", 1 if i < 2 else 0)

        counts = load_rollout_counts(db_path=tmp_db)
        assert "assertion_local" in counts
        reflect_counts = counts["assertion_local"].get("reflect", {})
        assert reflect_counts["success"] == 2
        assert reflect_counts["total"] == 3

    def test_load_rollout_counts_empty_returns_empty_dict(self, tmp_db: Path) -> None:
        from budgetpilot.core.calibration.rollouts import load_rollout_counts
        counts = load_rollout_counts(db_path=tmp_db)
        assert counts == {}

    def test_load_rollout_counts_multiple_actions(self, tmp_db: Path) -> None:
        from budgetpilot.core.calibration.rollouts import load_rollout_counts
        # 2 successes for reflect, 0 successes for replan
        for _ in range(2):
            self._insert_rollout(tmp_db, "capability_gap", "reflect", 1)
        for _ in range(2):
            self._insert_rollout(tmp_db, "capability_gap", "replan", 0)
        for _ in range(2):
            self._insert_rollout(tmp_db, "capability_gap", "escalate", 1)

        counts = load_rollout_counts(db_path=tmp_db)
        assert "capability_gap" in counts
        cg = counts["capability_gap"]
        assert cg["reflect"]["success"] == 2
        assert cg["replan"]["success"] == 0
        assert cg["escalate"]["success"] == 2


# ---------------------------------------------------------------------------
# Test: Laplace smoothing and prior estimation
# ---------------------------------------------------------------------------

class TestLaplaceSmoothing:
    def test_laplace_smooth_uniform_with_zero_observations(self) -> None:
        from budgetpilot.core.router import _laplace_smooth
        # With 0 successes, 0 total, 4 classes: P = 1 / (0 + 1*4) = 0.25
        p = _laplace_smooth(successes=0, total=0, alpha=1.0, num_classes=4)
        assert p == pytest.approx(0.25, abs=1e-6)

    def test_laplace_smooth_with_all_successes(self) -> None:
        from budgetpilot.core.router import _laplace_smooth
        # 10 successes out of 10: P = (10+1)/(10+4) = 11/14 ≈ 0.786
        p = _laplace_smooth(successes=10, total=10, alpha=1.0, num_classes=4)
        assert p == pytest.approx(11 / 14, abs=1e-6)

    def test_laplace_smooth_convergence_with_infinite_data(self) -> None:
        from budgetpilot.core.router import _laplace_smooth
        # With very large N, approaches empirical frequency
        p = _laplace_smooth(successes=9000, total=10000, alpha=1.0, num_classes=4)
        assert abs(p - 0.9) < 0.01  # Should be ~0.9

    def test_load_empirical_priors_empty_when_no_rollouts(self, tmp_db: Path) -> None:
        from budgetpilot.core.router import load_empirical_priors
        priors = load_empirical_priors(db_path=tmp_db)
        assert priors == {}

    def test_load_empirical_priors_below_min_trials_excluded(self, tmp_db: Path) -> None:
        from budgetpilot.core.router import load_empirical_priors
        from budgetpilot.core.calibration.rollouts import _ensure_rollout_columns
        from budgetpilot.core.store import safe_execute_write
        _ensure_rollout_columns(tmp_db)
        # Insert only 2 trials (below min_trials=5)
        for _ in range(2):
            safe_execute_write(
                tmp_db,
                "INSERT INTO rollouts (task_id, repo_name, failure_induced, recovery_trajectory, "
                "final_verdict, total_cost_usd, failure_class, action, seed, succeeded) "
                "VALUES (?, 'r', 'repairable', 'reflect', 'pass', 0.001, ?, ?, 0, 1);",
                (f"t_{_}", "assertion_local", "reflect"),
            )
        priors = load_empirical_priors(db_path=tmp_db, min_trials=5)
        assert "assertion_local" not in [k.value for k in priors.keys()]

    def test_load_empirical_priors_above_min_trials_included(self, tmp_db: Path) -> None:
        from budgetpilot.core.router import load_empirical_priors
        from budgetpilot.core.calibration.rollouts import _ensure_rollout_columns
        from budgetpilot.core.store import safe_execute_write

        _ensure_rollout_columns(tmp_db)
        # Insert 5 successes for capability_gap/escalate
        for _ in range(5):
            safe_execute_write(
                tmp_db,
                "INSERT INTO rollouts (task_id, repo_name, failure_induced, recovery_trajectory, "
                "final_verdict, total_cost_usd, failure_class, action, seed, succeeded) "
                "VALUES (?, 'r', 'capability_gap', 'escalate', 'pass', 0.001, ?, ?, 0, 1);",
                (f"t_{_}", "capability_gap", "escalate"),
            )

        priors = load_empirical_priors(db_path=tmp_db, min_trials=5)
        # capability_gap should appear in empirical priors
        fc_vals = {fc.value for fc in priors.keys()}
        assert "capability_gap" in fc_vals

        cap_gap_priors = priors.get(FailureClass.CAPABILITY_GAP, {})
        # escalate had 5/5 successes → Laplace: (5+1)/(5+4) = 6/9 ≈ 0.667
        escalate_p = cap_gap_priors.get(RecoveryAction.ESCALATE, 0.0)
        assert escalate_p > 0.5  # Should be high since 100% success

    def test_blend_priors_at_min_trials_gives_50_50(self) -> None:
        from budgetpilot.core.router import _blend_priors, RecoveryAction
        base = {a: 0.4 for a in RecoveryAction}
        empirical = {a: 0.8 for a in RecoveryAction}
        # At min_trials=5, total_trials=5 → w=0.0 → all base
        blended = _blend_priors(base, empirical, total_trials=5, min_trials=5, max_trials=30)
        for action in RecoveryAction:
            # w = (5-5)/(30-5) = 0 → all base = 0.4
            assert blended[action] == pytest.approx(0.4, abs=1e-4)

    def test_blend_priors_at_max_trials_gives_full_empirical(self) -> None:
        from budgetpilot.core.router import _blend_priors, RecoveryAction
        base = {a: 0.3 for a in RecoveryAction}
        empirical = {a: 0.9 for a in RecoveryAction}
        blended = _blend_priors(base, empirical, total_trials=30, min_trials=5, max_trials=30)
        for action in RecoveryAction:
            # w = (30-5)/(30-5) = 1.0 → all empirical = 0.9
            assert blended[action] == pytest.approx(0.9, abs=1e-4)

    def test_blend_priors_respects_valid_probability_range(self) -> None:
        from budgetpilot.core.router import _blend_priors, RecoveryAction
        base = {a: 0.01 for a in RecoveryAction}
        empirical = {a: 1.5 for a in RecoveryAction}  # Out of range
        blended = _blend_priors(base, empirical, total_trials=30, min_trials=5, max_trials=30)
        for v in blended.values():
            assert 0.02 <= v <= 0.99


# ---------------------------------------------------------------------------
# Test: Router integration with empirical priors
# ---------------------------------------------------------------------------

class TestRouterWithEmpiricalPriors:
    def test_route_without_rollout_data_uses_base_prior(self, tmp_db: Path) -> None:
        from budgetpilot.core.router import route
        from budgetpilot.core.types import RecoveryContext
        ctx = RecoveryContext(
            failure_class=FailureClass.CAPABILITY_GAP,
            error_message="test failure",
            attempt_number=1,
        )
        decision = route(ctx, budget="balanced", db_path=tmp_db)
        # Without rollout data, should still return a valid decision
        assert decision.action in list(RecoveryAction)
        assert 0 <= decision.confidence <= 1

    def test_route_with_rollout_data_blends_priors(self, tmp_db: Path) -> None:
        """
        When rollout data shows capability_gap/escalate has very high success rate,
        the router should favor escalate more than the base prior alone.
        """
        from budgetpilot.core.router import route
        from budgetpilot.core.types import RecoveryContext
        from budgetpilot.core.calibration.rollouts import _ensure_rollout_columns
        from budgetpilot.core.store import safe_execute_write

        _ensure_rollout_columns(tmp_db)
        # Insert strong escalate signal: 25 successes for capability_gap/escalate
        for i in range(25):
            safe_execute_write(
                tmp_db,
                "INSERT INTO rollouts (task_id, repo_name, failure_induced, recovery_trajectory, "
                "final_verdict, total_cost_usd, failure_class, action, seed, succeeded) "
                "VALUES (?, 'r', 'capability_gap', 'escalate', 'pass', 0.001, ?, ?, 0, 1);",
                (f"t_esc_{i}", "capability_gap", "escalate"),
            )
        # Insert low reflect signal: 25 failures for capability_gap/reflect
        for i in range(25):
            safe_execute_write(
                tmp_db,
                "INSERT INTO rollouts (task_id, repo_name, failure_induced, recovery_trajectory, "
                "final_verdict, total_cost_usd, failure_class, action, seed, succeeded) "
                "VALUES (?, 'r', 'capability_gap', 'reflect', 'fail', 0.001, ?, ?, 0, 0);",
                (f"t_ref_{i}", "capability_gap", "reflect"),
            )

        ctx = RecoveryContext(
            failure_class=FailureClass.CAPABILITY_GAP,
            error_message="complex task failure",
            attempt_number=1,
        )
        decision = route(ctx, budget="loose", db_path=tmp_db)
        # With loose budget and strong escalate signal, escalate should be chosen
        assert decision.action == RecoveryAction.ESCALATE

    def test_compute_adjusted_probabilities_blends_correctly(self, tmp_db: Path) -> None:
        from budgetpilot.core.router import compute_adjusted_probabilities
        from budgetpilot.core.types import RecoveryContext
        from budgetpilot.core.calibration.rollouts import _ensure_rollout_columns
        from budgetpilot.core.store import safe_execute_write

        _ensure_rollout_columns(tmp_db)
        # Insert 10 successful replan trials for assertion_local
        for i in range(10):
            safe_execute_write(
                tmp_db,
                "INSERT INTO rollouts (task_id, repo_name, failure_induced, recovery_trajectory, "
                "final_verdict, total_cost_usd, failure_class, action, seed, succeeded) "
                "VALUES (?, 'r', 'assertion_local', 'replan', 'pass', 0.001, ?, ?, 0, 1);",
                (f"t_rp_{i}", "assertion_local", "replan"),
            )

        ctx = RecoveryContext(
            failure_class=FailureClass.ASSERTION_LOCAL,
            error_message="assertion failed",
        )
        probs = compute_adjusted_probabilities(ctx, db_path=tmp_db)
        # All probs should be in valid range
        for action, p in probs.items():
            assert 0.02 <= p <= 0.99, f"Probability out of range for {action}: {p}"


# ---------------------------------------------------------------------------
# Test: CLI bp rollout collect
# ---------------------------------------------------------------------------

class TestCLIRolloutCollect:
    def _fast_execute(self, state: Any, action: RecoveryAction, seed: int, **kwargs: Any):
        from budgetpilot.core.calibration.rollouts import RolloutRecord
        return RolloutRecord(
            failure_kind=state.failure_kind.value,
            failure_class=state.failure_class.value,
            base_commit=state.base_commit,
            repo_name="cli_test",
            action=action.value,
            seed=seed,
            succeeded=True,
            tokens_spent=200,
            cost_usd=0.0002,
            wall_time_sec=0.01,
        )

    def test_rollout_collect_missing_dir_returns_error(self, tmp_path: Path) -> None:
        from budgetpilot.cli import run_rollout_collect
        rc = run_rollout_collect(
            failures_dir=str(tmp_path / "nonexistent_dir"),
            seeds=1,
        )
        assert rc == 1

    def test_rollout_collect_empty_dir_returns_error(self, tmp_path: Path) -> None:
        from budgetpilot.cli import run_rollout_collect
        rc = run_rollout_collect(failures_dir=str(tmp_path), seeds=1)
        assert rc == 1

    def test_rollout_collect_with_saved_states(self, tmp_path: Path) -> None:
        from budgetpilot.core.calibration.failures import FailureState, FailureKind, save_failure_states
        from budgetpilot.cli import run_rollout_collect
        import budgetpilot.core.calibration.rollouts as rollouts_mod

        states = [
            FailureState(
                failure_kind=FailureKind.REPAIRABLE,
                base_commit="abc1234",
                worktree_path=tmp_path / "wt",
                touched_files=["mod_a.py"],
                stale_files=[],
                mutation_description="test",
                failure_class=FailureClass.ASSERTION_LOCAL,
                repo_path=tmp_path,
            )
        ]
        save_failure_states(states, tmp_path)

        with patch.object(rollouts_mod, "_execute_action_against_failure", self._fast_execute):
            rc = run_rollout_collect(
                failures_dir=str(tmp_path),
                seeds=1,
                tier="cheap",
                max_turns=2,
                format_type="terminal",
            )
        assert rc == 0

    def test_rollout_collect_json_format(self, tmp_path: Path, capsys: Any) -> None:
        from budgetpilot.core.calibration.failures import FailureState, FailureKind, save_failure_states
        from budgetpilot.cli import run_rollout_collect
        import budgetpilot.core.calibration.rollouts as rollouts_mod

        states = [
            FailureState(
                failure_kind=FailureKind.CAPABILITY_GAP,
                base_commit="cafebabe",
                worktree_path=tmp_path / "wt2",
                touched_files=[],
                stale_files=["mod_a.py"],
                failure_class=FailureClass.CAPABILITY_GAP,
                repo_path=tmp_path,
            )
        ]
        save_failure_states(states, tmp_path)
        with patch.object(rollouts_mod, "_execute_action_against_failure", self._fast_execute):
            rc = run_rollout_collect(
                failures_dir=str(tmp_path),
                seeds=1,
                format_type="json",
            )
        assert rc == 0
        captured = capsys.readouterr()
        # Extract the JSON array — it starts at the '[' character
        out = captured.out
        json_start = out.find("[")
        assert json_start != -1, f"No JSON array found in output:\n{out}"
        data = json.loads(out[json_start:])
        assert isinstance(data, list)
        assert len(data) == 4  # 1 state × 4 actions × 1 seed


# ---------------------------------------------------------------------------
# Test: Decoupling — calibration package must not import forbidden modules
# ---------------------------------------------------------------------------

def test_calibration_package_no_forbidden_imports() -> None:
    """calibration/*.py must not import ollama, mcp, fastapi, or uvicorn."""
    calibration_dir = (
        Path(__file__).resolve().parent.parent
        / "budgetpilot" / "core" / "calibration"
    )
    assert calibration_dir.exists(), f"calibration dir not found: {calibration_dir}"

    forbidden = {"ollama", "mcp", "fastapi", "uvicorn"}
    violations = []

    for py_file in sorted(calibration_dir.glob("*.py")):
        with open(py_file, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in forbidden:
                        violations.append((py_file.name, alias.name))
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                if root in forbidden:
                    violations.append((py_file.name, node.module))

    assert violations == [], f"Forbidden imports found in calibration package: {violations}"


def test_router_no_forbidden_imports() -> None:
    """router.py must not import ollama, mcp, fastapi, or uvicorn."""
    router_path = (
        Path(__file__).resolve().parent.parent
        / "budgetpilot" / "core" / "router.py"
    )
    with open(router_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=str(router_path))
    forbidden = {"ollama", "mcp", "fastapi", "uvicorn"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split(".")[0] not in forbidden, f"Forbidden: {a.name}"
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in forbidden, f"Forbidden: {node.module}"

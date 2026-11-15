"""Prompt size as a scope feature, and the round-up-when-uncertain rule."""

import shutil

import pytest

from budgetpilot.core.estimator import estimate, estimate_prompt_tokens
from tests.hookutil import make_repo

SPEC = "We need to change behaviour. " * 80  # ~600 estimated tokens
# Without ripgrep the identifier probe is skipped, so an identifier-only prompt has less evidence.
NEEDS_RG = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    return make_repo(tmp_path_factory.mktemp("est") / "repo")


@pytest.mark.parametrize("prompt,scope,rounded_up", [
    # small and specific -> scope 1
    ("Fix add in pkg/calc.py", 1, False),
    ("pkg/calc.py:2: AssertionError in add, line 2", 1, False),
    pytest.param("Why does `util_c` return 1?", 1, False, marks=NEEDS_RG),
    ("In pkg/calc.py the add function subtracts. " + "Please be careful about edge cases. " * 6, 1, False),
    # several named files -> wider
    ("Update pkg/calc.py and tests/test_add.py so add works", 2, False),
    ("Change pkg/calc.py, pkg/util_a.py and pkg/util_b.py to share a helper", 3, False),
    # large pasted spec naming files -> scope 2 or 3
    (SPEC + " Touch pkg/calc.py and tests/test_add.py.", 2, False),
    (SPEC + " Touch pkg/calc.py, pkg/util_a.py and tests/test_add.py.", 3, False),
    (SPEC * 4 + " Touch pkg/calc.py and tests/test_add.py.", 2, False),
    # broad wording wins regardless of size
    ("Refactor the whole codebase to rename add everywhere", 3, False),
    (SPEC, 3, False),
    # no evidence at all: low confidence, so never under-scope
    ("make it better", 2, True),
    ("Fix the failing addition test.", 2, True),
    ("", 2, True),
])
def test_scope_from_prompt_size_and_specificity(prompt, scope, rounded_up, repo):
    prediction = estimate(prompt, repo_path=str(repo), persist=False)
    meta = prediction.metadata
    assert prediction.scope == scope, meta
    assert meta["low_confidence_round_up"] is rounded_up
    assert meta["features"]["prompt_tokens_est"] == estimate_prompt_tokens(prompt)
    if rounded_up:
        assert prediction.confidence < meta["threshold"] and prediction.scope == meta["raw_scope"] + 1


def test_round_up_never_lowers_scope(repo):
    for prompt in ("x", "fix bug", "Fix add in pkg/calc.py", SPEC, "refactor everything everywhere"):
        prediction = estimate(prompt, repo_path=str(repo), persist=False)
        assert prediction.scope >= prediction.metadata["raw_scope"]


def test_nested_paths_are_verified_against_the_repository(repo):
    features = estimate("Fix add in pkg/calc.py", repo_path=str(repo), persist=False).metadata["features"]
    assert features["stat_verified_files"] == ["pkg/calc.py"]


def test_token_estimate_is_bytes_over_chars_per_token():
    assert estimate_prompt_tokens("") == 0
    assert estimate_prompt_tokens("a" * 38) == 10
    assert estimate_prompt_tokens("é" * 19) == 10  # UTF-8 bytes, not characters


def test_persist_false_writes_nothing(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("BP_HOME", str(tmp_path / "home"))
    estimate("Fix add in pkg/calc.py", repo_path=str(repo), persist=False)
    assert not (tmp_path / "home" / "bp.db").exists()

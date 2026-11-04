"""
Unit and integration tests for BudgetPilot Scope Estimator (Phase D).
Tests cover:
1. Feature extraction:
   - Stat-verified named file paths (only existing files match).
   - Quoted literals, backticks, snake_case, CamelCase identifiers.
   - Stopwords exclusion from identifiers.
   - Traceback patterns and line numbers.
   - Broad-scope keywords ('all', 'everywhere', 'refactor', 'migrate', 'rename', etc.).
   - Length and clause count extraction.
2. Scope prediction logic & resource allocation:
   - Scope 1: Localized / single-file fix (budget 3000 tokens, max 3 read files).
   - Scope 2: Multi-file / local module (budget 8000 tokens, max 8 read files).
   - Scope 3: Cross-repo / architecture / broad refactoring (budget 20000 tokens, max 25 read files).
3. Ripgrep Identifier Probe:
   - Isolated identifier (1 file hit) -> Level 1 cue.
   - Module identifier (2-5 file hits) -> Level 2 cue.
   - Widespread identifier (>5 hits) -> Level 3 cue.
4. Low Confidence Round-Up:
   - When confidence < threshold, rounds up (Scope 1 -> Scope 2, Scope 2 -> Scope 3).
   - Confident predictions maintain their scope level.
5. Performance guarantee:
   - Fast lexical rules execute in < 100 ms (typically < 30 ms).
6. SQLite persistence:
   - ScopePrediction saved to `scope_predictions` table with metadata and cues.
7. CLI command:
   - `bp estimator eval` terminal and JSON output against repository commit history.
"""

import json
from pathlib import Path
import subprocess
import time
import pytest

from budgetpilot.cli import main
from budgetpilot.core.estimator import (
    BROAD_SCOPE_WORDS,
    ExtractedFeatures,
    SCOPE_RESOURCES,
    ScopeEstimator,
    estimate,
    extract_features,
)
from budgetpilot.core.store import get_connection, init_db
from budgetpilot.core.types import ScopePrediction


def test_feature_extraction_stat_verified_files(tmp_path: Path):
    """Verify that only existing files within the target repo are stat-verified."""
    (tmp_path / "calculator.py").write_text("# calc", encoding="utf-8")
    (tmp_path / "helpers.py").write_text("# helpers", encoding="utf-8")

    prompt = "Fix error in calculator.py and non_existent_file.py when calling helpers.py"
    features = extract_features(prompt, repo_path=tmp_path)

    assert "calculator.py" in features.stat_verified_files
    assert "helpers.py" in features.stat_verified_files
    assert "non_existent_file.py" not in features.stat_verified_files
    assert len(features.stat_verified_files) == 2


def test_feature_extraction_identifiers():
    """Verify identifier extraction with backticks, quotes, snake_case, CamelCase and stopwords filtering."""
    prompt = (
        "Refactor `calculate_annual_tax` and 'TaxRuleEngine' in "
        "process_tax_records function to fix issue with return value"
    )
    features = extract_features(prompt)

    # Identifiers should include backticked, quoted, snake_case, and CamelCase
    assert "calculate_annual_tax" in features.candidate_identifiers
    assert "TaxRuleEngine" in features.candidate_identifiers
    assert "process_tax_records" in features.candidate_identifiers

    # Stopwords should be filtered out
    for stop in ["function", "return", "issue", "tax"]:
        # "tax" might appear as substring, but exact match "function" shouldn't be a standalone candidate
        assert stop not in features.candidate_identifiers

    # Top identifier should be the prioritized backtick identifier
    assert features.top_identifier == "calculate_annual_tax"


def test_feature_extraction_traceback_and_line_numbers():
    """Verify detection of line numbers and stack trace error patterns."""
    prompt_with_line = "Fix IndexError in parser.py at line 142"
    f1 = extract_features(prompt_with_line)
    assert f1.has_traceback_or_line is True

    prompt_with_colon_line = "Check syntax error at main.py:88"
    f2 = extract_features(prompt_with_colon_line)
    assert f2.has_traceback_or_line is True

    prompt_with_traceback = (
        "Traceback (most recent call last):\n"
        "  File 'run.py', line 12\n"
        "TypeError: unsupported operand type"
    )
    f3 = extract_features(prompt_with_traceback)
    assert f3.has_traceback_or_line is True

    prompt_clean = "Add new feature for logging"
    f4 = extract_features(prompt_clean)
    assert f4.has_traceback_or_line is False


def test_feature_extraction_broad_words():
    """Verify broad-scope keywords biasing toward Level 3 are extracted."""
    prompt = "Migrate all database schemas everywhere and refactor codebase across services"
    features = extract_features(prompt)

    assert "all" in features.broad_words
    assert "codebase" in features.broad_words
    assert "everywhere" in features.broad_words
    assert "migrate" in features.broad_words
    assert "refactor" in features.broad_words
    assert "across" in features.broad_words


def test_scope_1_localized_prediction(tmp_path: Path):
    """Verify single file with traceback/line number yields Scope 1 and resource limits."""
    (tmp_path / "calc.py").write_text("def add(): pass", encoding="utf-8")

    prompt = "Fix ZeroDivisionError in calc.py line 42"
    pred = estimate(prompt, repo_path=str(tmp_path))

    assert isinstance(pred, ScopePrediction)
    assert pred.scope == 1
    assert pred.budget_tokens == 3000
    assert pred.max_read_files == 3
    assert pred.confidence >= 0.5
    assert "single stat-verified file" in pred.rationale


def test_scope_2_multi_file_prediction(tmp_path: Path):
    """Verify two stat-verified files yield Scope 2 and resource limits."""
    (tmp_path / "models.py").write_text("# models", encoding="utf-8")
    (tmp_path / "views.py").write_text("# views", encoding="utf-8")

    prompt = "Update models.py and views.py to add user age field"
    pred = estimate(prompt, repo_path=str(tmp_path))

    assert pred.scope == 2
    assert pred.budget_tokens == 8000
    assert pred.max_read_files == 8
    assert "2 stat-verified files" in pred.rationale


def test_scope_3_broad_keywords_prediction():
    """Verify broad-scope keywords bias toward Scope 3."""
    prompt = "Refactor all serialization logic across the entire codebase to migrate to pydantic v2 everywhere"
    pred = estimate(prompt)

    assert pred.scope == 3
    assert pred.budget_tokens == 20000
    assert pred.max_read_files == 25
    assert "broad-scope keywords detected" in pred.rationale


def test_identifier_probe_ripgrep(tmp_path: Path):
    """Verify ripgrep probe identifies single-file vs multi-file vs cross-cutting identifiers."""
    # 1 file hit
    (tmp_path / "auth.py").write_text("TOKEN_SECRET_UNIQUE_XYZ = 'abc'", encoding="utf-8")

    # 3 files hit (Level 2)
    (tmp_path / "f1.py").write_text("SHARED_MODULE_VAR = 1", encoding="utf-8")
    (tmp_path / "f2.py").write_text("SHARED_MODULE_VAR = 2", encoding="utf-8")
    (tmp_path / "f3.py").write_text("SHARED_MODULE_VAR = 3", encoding="utf-8")

    # Probe isolated identifier
    f_isolated = extract_features("Check `TOKEN_SECRET_UNIQUE_XYZ` value", repo_path=tmp_path)
    assert f_isolated.probe_hits == 1
    assert f_isolated.probe_latency_ms < 50.0

    # Probe multi-file identifier
    f_multi = extract_features("Update `SHARED_MODULE_VAR` setting", repo_path=tmp_path)
    assert f_multi.probe_hits == 3


def test_low_confidence_round_up():
    """
    Verify low confidence (< threshold) rounds up to prevent costly under-scoping.
    Under-scoping costs task failure; over-scoping costs tokens.
    """
    # 1. Test forced round-up with high threshold
    prompt_isolated = "Fix bug in line 10"
    # Normally Scope 1; with threshold=0.99, it must round up to Scope 2
    pred_rounded = estimate(prompt_isolated, threshold=0.99)
    assert pred_rounded.metadata["low_confidence_round_up"] is True
    assert pred_rounded.scope == 2
    assert "rounded up from Level 1" in pred_rounded.rationale

    # 2. Confident prediction does not round up
    prompt_confident = "Refactor all classes everywhere across the entire codebase"
    pred_confident = estimate(prompt_confident, threshold=0.35)
    assert pred_confident.metadata["low_confidence_round_up"] is False
    assert pred_confident.scope == 3


def test_execution_time_under_100ms():
    """Verify that estimate execution completes strictly under 100 ms across multiple tasks."""
    prompts = [
        "Fix syntax error in `calculate_total` in billing.py:35",
        "Refactor all authentication handlers across codebase",
        "Update user model and session manager to add permissions",
        "Traceback (most recent call last): KeyError: 'token'",
        "Rename `format_currency` function everywhere",
    ]

    latencies = []
    for p in prompts:
        t0 = time.time()
        pred = estimate(p)
        elapsed_ms = (time.time() - t0) * 1000.0
        latencies.append(elapsed_ms)
        # Strict requirement: under 100 ms
        assert elapsed_ms < 100.0, f"Estimate took {elapsed_ms:.2f} ms (exceeded 100 ms limit)"
        assert pred.metadata["latency_ms"] < 100.0

    avg_latency = sum(latencies) / len(latencies)
    assert avg_latency < 50.0, f"Average latency {avg_latency:.2f} ms was higher than expected (<50ms)"


def test_sqlite_persistence(tmp_path: Path):
    """Verify scope predictions and extracted feature cues are persisted in SQLite scope_predictions table."""
    db_file = tmp_path / "test_scope.db"
    init_db(db_file)

    pred = estimate(
        prompt="Fix IndexError in router.py line 50",
        repo_path=str(tmp_path),
        session_id="session_test_scope_1",
        db_path=db_file,
    )

    conn = get_connection(db_file)
    try:
        cursor = conn.execute(
            """
            SELECT session_id, scope, confidence, budget_tokens, max_read_files, rationale, metadata
            FROM scope_predictions
            WHERE session_id = 'session_test_scope_1';
            """
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["session_id"] == "session_test_scope_1"
        assert row["scope"] == pred.scope
        assert abs(row["confidence"] - pred.confidence) < 1e-4
        assert row["budget_tokens"] == pred.budget_tokens
        assert row["max_read_files"] == pred.max_read_files

        meta = json.loads(row["metadata"])
        assert "features" in meta
        assert "probabilities" in meta
        assert "latency_ms" in meta
        assert meta["final_scope"] == pred.scope
    finally:
        conn.close()


def test_scope_estimator_class(tmp_path: Path):
    """Verify ScopeEstimator class wrapper interface."""
    estimator = ScopeEstimator(threshold=0.40, repo_path=str(tmp_path))
    pred = estimator.predict("Refactor all modules everywhere")

    assert isinstance(pred, ScopePrediction)
    assert pred.scope == 3
    assert pred.metadata["threshold"] == 0.40


def test_cli_estimator_eval_terminal_and_json(tmp_path: Path, capsys):
    """Verify bp estimator eval subcommands with terminal and JSON output formats."""
    # Set up mock git repository
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "eval@budgetpilot.io"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Scope Evaluator"], cwd=str(tmp_path), check=True)

    # Commit 1: 1 file changed (Scope 1)
    (tmp_path / "hello.py").write_text("print('hello')", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "Add hello script"], cwd=str(tmp_path), check=True, capture_output=True)

    # Commit 2: 3 files changed (Scope 2)
    (tmp_path / "mod1.py").write_text("a = 1", encoding="utf-8")
    (tmp_path / "mod2.py").write_text("b = 2", encoding="utf-8")
    (tmp_path / "mod3.py").write_text("c = 3", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "Add modules 1 to 3"], cwd=str(tmp_path), check=True, capture_output=True)

    # 1. Terminal evaluation
    code_term = main(["estimator", "eval", "--repo", str(tmp_path), "--limit", "5", "--format", "terminal"])
    captured_term = capsys.readouterr().out

    assert code_term == 0
    assert "BudgetPilot Scope Estimator Evaluation (Phase D)" in captured_term
    assert "Summary Metrics:" in captured_term
    assert "Commits Evaluated:  2" in captured_term
    assert "Exact Accuracy:" in captured_term
    assert "Mean Latency:" in captured_term

    # 2. JSON evaluation
    code_json = main(["estimator", "eval", "--repo", str(tmp_path), "--limit", "5", "--format", "json"])
    captured_json = capsys.readouterr().out

    assert code_json == 0
    data = json.loads(captured_json)
    assert data["commits_evaluated"] == 2
    assert "accuracy_pct" in data
    assert "under_scoped_count" in data
    assert "avg_latency_ms" in data
    assert len(data["results"]) == 2
    assert data["results"][0]["files_count"] in (1, 3)


def test_cli_estimator_eval_invalid_repo(tmp_path: Path, capsys):
    """Verify bp estimator eval rejects non-git directories gracefully."""
    non_git_dir = tmp_path / "not_git"
    non_git_dir.mkdir()

    code = main(["estimator", "eval", "--repo", str(non_git_dir)])
    captured = capsys.readouterr().out

    assert code == 1
    assert "is not a git repository" in captured

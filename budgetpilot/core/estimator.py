"""
BudgetPilot Scope Estimator Subsystem.
Phase D: Fast lexical and structural probe predicting task scope (1, 2, 3)
without LLM invocations in under 100 milliseconds.

Strict Rule: This module must NOT import ollama, mcp, or fastapi.
"""

from dataclasses import asdict, dataclass
import json
import logging
import math
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from budgetpilot.core.store import get_connection, init_db, safe_execute_write
from budgetpilot.core.types import ScopePrediction

logger = logging.getLogger(__name__)

# Broad-scope indicators biasing toward Scope 3
BROAD_SCOPE_WORDS = {
    "all",
    "everywhere",
    "refactor",
    "refactoring",
    "migrate",
    "migration",
    "rename",
    "renaming",
    "across",
    "system-wide",
    "codebase",
    "architecture",
    "overhaul",
    "global",
    "reorganize",
    "reorganization",
    "deprecate",
    "deprecation",
}

# Stopwords to ignore during identifier extraction
IDENTIFIER_STOPWORDS = {
    "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "with", "from",
    "by", "of", "is", "are", "was", "were", "be", "been", "have", "has", "had",
    "do", "does", "did", "can", "could", "should", "would", "may", "might", "must",
    "will", "shall", "this", "that", "these", "those", "it", "its", "if", "then",
    "else", "when", "where", "why", "how", "what", "which", "who", "whom", "whose",
    "not", "no", "yes", "true", "false", "none", "null", "file", "files", "path",
    "paths", "code", "repo", "test", "tests", "function", "class", "method",
    "return", "import", "def", "error", "issue", "bug", "fix", "update", "change",
}

# Scope resource configurations: (budget_tokens, max_read_files)
SCOPE_RESOURCES: Dict[int, Tuple[int, int]] = {
    1: (3000, 3),    # Scope 1: localized / single-file
    2: (8000, 8),    # Scope 2: multi-file / module
    3: (20000, 25),  # Scope 3: cross-repo / architecture
}


@dataclass
class ExtractedFeatures:
    """
    Lexical and structural features extracted from user task prompt.
    """
    stat_verified_files: List[str]
    candidate_identifiers: List[str]
    top_identifier: Optional[str]
    broad_words: List[str]
    has_traceback_or_line: bool
    word_count: int
    clause_count: int
    probe_hits: Optional[int] = None
    probe_latency_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def extract_features(prompt: str, repo_path: Union[Path, str] = ".") -> ExtractedFeatures:
    """
    Extract lexical, structural, and probe features from the user prompt.
    Executes in under 20 ms.
    """
    root = Path(repo_path).expanduser().resolve()
    prompt_clean = prompt or ""
    prompt_lower = prompt_clean.lower()

    # 1. Stat-verified named file paths
    # Matches patterns like foo/bar.py, greet.py, tests/test_calc.py
    file_pattern = r'\b(?:[a-zA-Z0-9_\-\.\/]+\/)?([a-zA-Z0-9_\-]+\.(?:py|js|ts|jsx|tsx|json|yaml|yml|md|txt|html|css|toml|sql|sh|go|rs|c|cpp|h))\b'
    matches = re.findall(file_pattern, prompt_clean)

    verified_files: List[str] = []
    seen_files: Set[str] = set()

    for match in matches:
        if match in seen_files:
            continue
        # Direct check relative to repo
        direct_path = root / match
        if direct_path.exists() and direct_path.is_file():
            verified_files.append(match)
            seen_files.add(match)
            continue

        # Check basename across immediate files or shallow tree if relative path wasn't full
        base_match = Path(match).name
        if (root / base_match).is_file():
            verified_files.append(base_match)
            seen_files.add(match)

    # 2. Extract Quoted Literals and Code Identifiers
    backtick_ids = re.findall(r'`([^`]+)`', prompt_clean)
    quoted_ids = re.findall(r'["\']([a-zA-Z0-9_\.]+)["\']', prompt_clean)
    snake_ids = re.findall(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b', prompt_clean)
    camel_ids = re.findall(r'\b[A-Z][a-zA-Z0-9]*[a-z][a-zA-Z0-9]*\b', prompt_clean)

    all_raw_candidates: List[str] = []
    # Rank backticked and quoted first
    all_raw_candidates.extend(backtick_ids)
    all_raw_candidates.extend(quoted_ids)
    all_raw_candidates.extend(snake_ids)
    all_raw_candidates.extend(camel_ids)

    candidate_identifiers: List[str] = []
    seen_ids: Set[str] = set()
    for cand in all_raw_candidates:
        clean_cand = cand.strip("`'\" \t\n.,;:()")
        if clean_cand and clean_cand.lower() not in IDENTIFIER_STOPWORDS and len(clean_cand) >= 3:
            if clean_cand not in seen_ids:
                candidate_identifiers.append(clean_cand)
                seen_ids.add(clean_cand)

    top_identifier = candidate_identifiers[0] if candidate_identifiers else None

    # 3. Line numbers or traceback patterns
    has_line_num = bool(re.search(r'\b(?:line\s+\d+|:\d+\b|L\d+\b)', prompt_clean))
    has_traceback = bool(re.search(r'\b(?:Traceback|traceback|SyntaxError|TypeError|ValueError|KeyError|AttributeError|AssertionError|Exception|Error:)\b', prompt_clean))
    has_traceback_or_line = has_line_num or has_traceback

    # 4. Broad-scope words
    words_in_prompt = set(re.findall(r'\b[a-z_\-]+\b', prompt_lower))
    found_broad_words = sorted(list(words_in_prompt.intersection(BROAD_SCOPE_WORDS)))
    if "replace all" in prompt_lower and "replace all" not in found_broad_words:
        found_broad_words.append("replace all")

    # 5. Length and clause counts
    tokens = prompt_clean.split()
    word_count = len(tokens)
    clauses = re.split(r'[,;.]|\b(?:and|then|also|after|furthermore)\b', prompt_clean)
    clause_count = len([c for c in clauses if c.strip()])

    # 6. Identifier Probe using ripgrep (rg) with 50 ms timeout
    probe_hits = None
    probe_latency_ms = 0.0

    if top_identifier and root.exists() and shutil.which("rg"):
        t_probe_start = time.time()
        try:
            cmd = ["rg", "-l", "--max-count", "1", top_identifier, str(root)]
            res = subprocess.run(
                cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=0.05,  # 50 ms strict timeout
                check=False,
            )
            probe_latency_ms = (time.time() - t_probe_start) * 1000.0
            if res.returncode in (0, 1):
                matching_files = [line for line in res.stdout.strip().splitlines() if line.strip()]
                probe_hits = len(matching_files)
        except (subprocess.TimeoutExpired, Exception) as exc:
            logger.debug("Identifier probe skipped or timed out: %s", exc)
            probe_latency_ms = (time.time() - t_probe_start) * 1000.0
            probe_hits = None

    return ExtractedFeatures(
        stat_verified_files=verified_files,
        candidate_identifiers=candidate_identifiers,
        top_identifier=top_identifier,
        broad_words=found_broad_words,
        has_traceback_or_line=has_traceback_or_line,
        word_count=word_count,
        clause_count=clause_count,
        probe_hits=probe_hits,
        probe_latency_ms=round(probe_latency_ms, 2),
    )


def estimate(
    prompt: str,
    repo_path: str = ".",
    threshold: float = 0.35,
    session_id: Optional[str] = None,
    db_path: Optional[Union[Path, str]] = None,
) -> ScopePrediction:
    """
    Fast lexical probe predicting task scope (1, 2, or 3) and budget constraints.
    Guaranteed execution time under 100 milliseconds.

    Scope Levels:
    - Scope 1: Localized / single-file fix
    - Scope 2: Multi-file / local module modification
    - Scope 3: Large / cross-cutting repository refactoring

    Low Confidence Round-Up:
    If prediction confidence is below threshold (default 0.35), selects the higher
    scope level to prevent costly under-scoping failures.
    """
    t_start = time.time()

    features = extract_features(prompt, repo_path=repo_path)

    scores = [0.0, 0.0, 0.0]  # [score_scope_1, score_scope_2, score_scope_3]
    rationale_parts: List[str] = []

    # 1. Broad words evidence (heavily favors Scope 3)
    if features.broad_words:
        weight = 3.5 * len(features.broad_words)
        scores[2] += weight
        rationale_parts.append(f"broad-scope keywords detected ({', '.join(features.broad_words)})")

    # 2. Stat-verified file paths evidence
    num_files = len(features.stat_verified_files)
    if num_files == 1:
        scores[0] += 3.0
        rationale_parts.append(f"single stat-verified file '{features.stat_verified_files[0]}'")
        if features.has_traceback_or_line:
            scores[0] += 2.0
            rationale_parts.append("line number/traceback pattern")
    elif num_files == 2:
        scores[1] += 3.5
        rationale_parts.append(f"2 stat-verified files ({', '.join(features.stat_verified_files)})")
    elif num_files >= 3:
        scores[2] += 4.0
        rationale_parts.append(f"{num_files} stat-verified files ({', '.join(features.stat_verified_files[:3])})")

    # 3. Isolated traceback or line numbers without multi-file context
    if features.has_traceback_or_line and num_files == 0:
        scores[0] += 2.0
        rationale_parts.append("traceback/line reference without multi-file cues")

    # 4. Identifier Probe Evidence
    if features.probe_hits is not None:
        hits = features.probe_hits
        if hits == 1:
            scores[0] += 2.5
            rationale_parts.append(f"probe '{features.top_identifier}' isolated to 1 file")
        elif 2 <= hits <= 5:
            scores[1] += 3.0
            rationale_parts.append(f"probe '{features.top_identifier}' matches {hits} files (Level 2)")
        elif hits > 5:
            scores[2] += 3.0
            rationale_parts.append(f"probe '{features.top_identifier}' matches {hits} files across repo (Level 3)")

    # 5. Complexity / Length Cues
    if features.clause_count >= 4 or features.word_count > 60:
        scores[1] += 1.0
        scores[2] += 1.5
        rationale_parts.append("multi-clause/complex prompt structure")
    elif features.word_count < 15 and num_files <= 1 and not features.broad_words:
        scores[0] += 1.2

    # Compute softmax probabilities
    max_s = max(scores)
    exp_scores = [math.exp(s - max_s) for s in scores]
    sum_exp = sum(exp_scores)
    probs = [round(s / sum_exp, 4) for s in exp_scores]

    raw_scope = probs.index(max(probs)) + 1
    confidence = max(probs)

    # Low-Confidence Round-Up:
    # If confidence is below threshold, round up to higher scope level
    # (Under-scoping costs task failure; over-scoping costs tokens)
    low_confidence_round_up = False
    final_scope = raw_scope

    if confidence < threshold:
        low_confidence_round_up = True
        if raw_scope == 1:
            final_scope = 2
        elif raw_scope == 2:
            final_scope = 3
        # raw_scope == 3 remains 3

    budget_tokens, max_read_files = SCOPE_RESOURCES[final_scope]

    elapsed_ms = (time.time() - t_start) * 1000.0

    if low_confidence_round_up:
        rationale = (
            f"Scope {final_scope} (rounded up from Level {raw_scope} due to low confidence "
            f"{confidence:.2f} < {threshold:.2f}). Evidence: {'; '.join(rationale_parts) if rationale_parts else 'low-evidence prompt'}."
        )
    else:
        rationale = (
            f"Scope {final_scope} (confidence: {confidence:.2f}). "
            f"Evidence: {'; '.join(rationale_parts) if rationale_parts else 'standard prompt'}."
        )

    prediction_metadata: Dict[str, Any] = {
        "raw_scope": raw_scope,
        "final_scope": final_scope,
        "confidence": confidence,
        "threshold": threshold,
        "low_confidence_round_up": low_confidence_round_up,
        "probabilities": {
            "scope_1": probs[0],
            "scope_2": probs[1],
            "scope_3": probs[2],
        },
        "features": features.to_dict(),
        "latency_ms": round(elapsed_ms, 2),
    }

    prediction = ScopePrediction(
        scope=final_scope,
        confidence=confidence,
        budget_tokens=budget_tokens,
        max_read_files=max_read_files,
        rationale=rationale,
        metadata=prediction_metadata,
    )

    # Save prediction to SQLite scope_predictions table
    try:
        init_db(db_path)
        if session_id:
            safe_execute_write(
                conn_or_path=db_path,
                sql="INSERT OR IGNORE INTO sessions (session_id) VALUES (?);",
                params=(session_id,),
            )

        safe_execute_write(
            conn_or_path=db_path,
            sql="""
            INSERT INTO scope_predictions (
                session_id, scope, confidence, budget_tokens, max_read_files, rationale, prompt_snippet, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            params=(
                session_id,
                prediction.scope,
                prediction.confidence,
                prediction.budget_tokens,
                prediction.max_read_files,
                prediction.rationale,
                prompt[:200],
                json.dumps(prediction_metadata),
            ),
        )
    except Exception as db_exc:
        logger.debug("Failed to record scope prediction in SQLite: %s", db_exc)

    return prediction


class ScopeEstimator:
    """
    Estimator class wrapping the lexical estimation probe.
    """

    def __init__(self, threshold: float = 0.35, repo_path: str = ".") -> None:
        self.threshold = threshold
        self.repo_path = repo_path

    def predict(
        self,
        prompt: str,
        repo_path: Optional[str] = None,
        session_id: Optional[str] = None,
        db_path: Optional[Union[Path, str]] = None,
    ) -> ScopePrediction:
        """
        Evaluate task scope using fast lexical probe and identifier ripgrep.
        """
        target_repo = repo_path or self.repo_path
        return estimate(
            prompt=prompt,
            repo_path=target_repo,
            threshold=self.threshold,
            session_id=session_id,
            db_path=db_path,
        )

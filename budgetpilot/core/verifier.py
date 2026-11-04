"""
BudgetPilot Verification and Test Diagnostic Subsystem.
Phase F: Test output parsing (pytest and generic runners), environment noise filtering,
7-class failure taxonomy, failure signature hashing, RecoveryContext assembly,
and SQLite verifications persistence.

Strict Rule: This module must NOT import ollama, mcp, or fastapi.
"""

from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from budgetpilot.core.store import get_connection, init_db, safe_execute_write
from budgetpilot.core.tracker import TaskState
from budgetpilot.core.types import (
    FailureClass,
    RecoveryAction,
    RecoveryContext,
)

logger = logging.getLogger(__name__)

# Max stderr length for RecoveryContext (4 KB)
MAX_STDERR_BYTES = 4096

# Patterns indicating environment noise / transient failures
ENV_NOISE_PATTERNS = [
    r"TimeoutExpired",
    r"timed out after \d+ seconds",
    r"ConnectionError",
    r"ConnectionRefusedError",
    r"NewConnectionError",
    r"MaxRetryError",
    r"socket\.timeout",
    r"ConnectionResetError",
    r"Failed to establish a new connection",
    r"502 Bad Gateway",
    r"503 Service Unavailable",
    r"PermissionError",
    r"Permission denied",
    r"OSError: \[Errno 13\]",
    r"Read-only file system",
    r"No space left on device",
    r"INTERNALERROR",
    r"pytest-xdist worker crashed",
    r"Segmentation fault",
    r"ResourceTemporarilyUnavailable",
]

# Patterns for sensitive credentials to redact in stderr
SECRET_REDACT_PATTERNS = [
    r'(?i)(api[_-]?key|token|password|secret|bearer|authorization)\s*[:=]\s*["\']?[a-zA-Z0-9_\-\.]{8,}["\']?',
]


@dataclass
class StackFrame:
    """Represents a parsed stack trace frame."""
    file_path: str
    line_number: int
    function_name: str = ""
    source_code: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationResult:
    """
    Structured outcome of test execution and diagnostic failure classification.
    """
    passed: bool
    exit_code: int
    test_command: str
    failure_class: Optional[FailureClass] = None
    failure_message: str = ""
    error_type: str = ""
    failing_test_ids: List[str] = field(default_factory=list)
    frames: List[StackFrame] = field(default_factory=list)
    top_in_repo_frame: str = ""
    outside_frames: List[str] = field(default_factory=list)
    unread_symbols: List[str] = field(default_factory=list)
    regressions: List[str] = field(default_factory=list)
    signature_hash: Optional[str] = None
    rerun_directive: bool = False
    recovery_context: Optional[RecoveryContext] = None
    duration: float = 0.0
    stdout: str = ""
    stderr: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.failure_class:
            d["failure_class"] = self.failure_class.value
        return d


def redact_and_truncate_stderr(stderr_text: str, max_chars: int = MAX_STDERR_BYTES) -> str:
    """
    Redact potential secrets and truncate stderr to max_chars limit (4 KB).
    """
    if not stderr_text:
        return ""

    text = stderr_text
    for pat in SECRET_REDACT_PATTERNS:
        text = re.sub(pat, r"\1=***REDACTED***", text)

    if len(text) > max_chars:
        omitted = len(text) - max_chars
        return text[:max_chars] + f"\n\n[... truncated {omitted} characters to {max_chars} bytes ...]"

    return text


def compute_signature_hash(error_type: str, top_in_repo_frame: str) -> str:
    """
    Deterministic failure signature hash: hash(error_type + top_in_repo_frame).
    """
    norm_err = (error_type or "UnknownError").strip()
    norm_frame = (top_in_repo_frame or "unknown:0").strip()
    raw = f"{norm_err}::{norm_frame}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def is_env_noise(text: str, exit_code: Optional[int] = None) -> bool:
    """
    Detect if failure is environmental or transient noise (timeouts, network, permission).
    """
    if exit_code in (-1, 124, 137):
        return True

    for pattern in ENV_NOISE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True

    return False


def parse_pytest_output(
    output_text: str,
    repo_root: Optional[Union[str, Path]] = None,
    touched_set: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    Parse pytest standard output and extract failing test IDs, stack traces,
    error messages, and frame paths.
    """
    root_str = str(Path(repo_root).resolve()) if repo_root else ""
    touched_norm = {Path(t).name for t in (touched_set or set())}

    failing_test_ids: List[str] = []
    frames: List[StackFrame] = []
    error_type = ""
    failure_message = ""
    unread_symbols: List[str] = []
    outside_frames: List[str] = []

    # 1. Extract failing test IDs
    # e.g.: FAILED tests/test_calc.py::test_add - AssertionError
    failed_lines = re.findall(r"FAILED\s+([^\s]+)", output_text)
    for fl in failed_lines:
        clean_id = fl.split(" - ")[0].strip()
        if clean_id not in failing_test_ids:
            failing_test_ids.append(clean_id)

    # 2. Extract section failures
    # e.g.: _______________ test_func _______________
    section_matches = re.findall(r"_{5,}\s+([^\n_]+)\s+_{5,}", output_text)
    for sm in section_matches:
        sec_id = sm.strip()
        if sec_id and sec_id not in failing_test_ids and not sec_id.startswith("FAILURES"):
            failing_test_ids.append(sec_id)

    # 3. Extract pytest failure error lines
    # e.g.: E   AssertionError: assert 2 == 3
    # e.g.: E   NameError: name 'MissingHelper' is not defined
    err_lines = re.findall(r"^E\s+([A-Z]\w+Error|[A-Z]\w+Exception|[A-Z]\w+Failure|[A-Z]\w+):\s*(.*)$", output_text, re.MULTILINE)
    if err_lines:
        error_type = err_lines[-1][0].strip()
        failure_message = f"{error_type}: {err_lines[-1][1].strip()}"
    else:
        # Fallback error detection
        match_err = re.search(r"\b([A-Z]\w+Error|[A-Z]\w+Exception|[A-Z]\w+):\s*([^\n]+)", output_text)
        if match_err:
            error_type = match_err.group(1).strip()
            failure_message = match_err.group(0).strip()

    # 4. Extract stack trace frames
    # Pattern A: File ".../foo.py", line 42, in func
    frame_matches_a = re.findall(r'File "([^"]+)", line (\d+)(?:, in (\w+))?', output_text)
    for f_path, l_num, f_name in frame_matches_a:
        frames.append(StackFrame(file_path=f_path, line_number=int(l_num), function_name=f_name))

    # Pattern B: foo.py:42: in func or foo.py:42: AssertionError
    frame_matches_b = re.findall(r'(\b[\w\/\.\-]+\.py):(\d+):(?:\s+in\s+(\w+)|.*)', output_text)
    for f_path, l_num, f_name in frame_matches_b:
        frames.append(StackFrame(file_path=f_path, line_number=int(l_num), function_name=f_name))

    # 5. Extract top in-repo frame & outside frames
    top_in_repo_frame = ""
    for frame in reversed(frames):
        p = frame.file_path
        # Skip stdlib / virtualenv frames
        if "/lib/python" in p or "site-packages" in p or "<" in p:
            continue

        frame_str = f"{p}:{frame.line_number}"
        if frame.function_name:
            frame_str += f":in {frame.function_name}"

        if not top_in_repo_frame:
            top_in_repo_frame = frame_str

        # Check if frame is outside touched set
        f_name = Path(p).name
        if f_name not in touched_norm:
            outside_frames.append(frame_str)

    if not top_in_repo_frame and failing_test_ids:
        top_in_repo_frame = failing_test_ids[0]

    # 6. Extract unread symbols from error message
    # e.g.: name 'foo' is not defined, cannot import name 'bar', has no attribute 'baz'
    name_symbols = re.findall(r"(?:name|attribute|module)\s+['\"]([^'\"]+)['\"]", failure_message)
    for sym in name_symbols:
        if sym not in unread_symbols:
            unread_symbols.append(sym)

    return {
        "failing_test_ids": failing_test_ids,
        "frames": frames,
        "error_type": error_type or "TestFailure",
        "failure_message": failure_message or "Test suite execution failed.",
        "top_in_repo_frame": top_in_repo_frame,
        "outside_frames": outside_frames,
        "unread_symbols": unread_symbols,
    }


def parse_generic_output(
    output_text: str,
    exit_code: int,
    repo_root: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """
    Fallback parser for generic test runners: extracts last 60 lines,
    detects errors, and identifies top frame.
    """
    lines = [line.strip() for line in output_text.splitlines() if line.strip()]
    tail_lines = lines[-60:] if len(lines) > 60 else lines
    summary = "\n".join(tail_lines)

    # Detect error type from summary
    match_err = re.search(r"\b([A-Z]\w+Error|[A-Z]\w+Exception|[A-Z]\w+):\s*([^\n]+)", summary)
    error_type = match_err.group(1).strip() if match_err else "NonZeroExitCode"
    failure_message = match_err.group(0).strip() if match_err else (tail_lines[-1] if tail_lines else f"Process exited with code {exit_code}")

    # Extract frame paths if present (supports file.py:45, file.py line 45, File "file.py", line 45)
    frame_matches = re.findall(r'(?:File "([^"]+)", line (\d+)|(\b[\w\/\.\-]+\.py)(?::|\s*,?\s*line\s+)(\d+))', summary)
    frames: List[StackFrame] = []
    top_in_repo_frame = ""
    for m in frame_matches:
        f_path = m[0] or m[2]
        l_num = m[1] or m[3]
        if f_path and l_num:
            frames.append(StackFrame(file_path=f_path, line_number=int(l_num)))
            if not top_in_repo_frame and "/lib/python" not in f_path:
                top_in_repo_frame = f"{f_path}:{l_num}"

    return {
        "failing_test_ids": [f"exit_code_{exit_code}"],
        "frames": frames,
        "error_type": error_type,
        "failure_message": failure_message,
        "top_in_repo_frame": top_in_repo_frame or f"command_exit_{exit_code}",
        "outside_frames": [],
        "unread_symbols": [],
    }


def classify_failure(
    error_type: str,
    failure_message: str,
    failing_test_ids: List[str],
    top_in_repo_frame: str,
    outside_frames: List[str],
    unread_symbols: List[str],
    touched_set: Set[str],
    signature_hash: str,
    previous_signatures: Optional[Set[str]] = None,
    attempt_number: int = 1,
    full_output: str = "",
) -> Tuple[FailureClass, bool]:
    """
    Classify failure into one of 7 diagnostic classes:
    1. syntax_or_lint
    2. assertion_local
    3. symbol_outside_touched
    4. regression_elsewhere
    5. repeat_signature
    6. capability_gap
    7. env_or_flaky

    Returns:
        (FailureClass, rerun_directive)
    """
    # 1. Check for Environment Noise / Flaky
    if is_env_noise(full_output) or is_env_noise(failure_message):
        return FailureClass.ENV_OR_FLAKY, True

    # 2. Check for Syntax or Lint Errors
    syntax_types = {"SyntaxError", "IndentationError", "TabError"}
    if error_type in syntax_types or "syntaxerror" in failure_message.lower() or "indentationerror" in failure_message.lower():
        return FailureClass.SYNTAX_OR_LINT, False

    # 3. Check for Repeat Signature
    if previous_signatures and signature_hash in previous_signatures:
        return FailureClass.REPEAT_SIGNATURE, False

    # 4. Check for Capability Gap
    # Missing system binaries, missing external tools, uninstalled packages
    if (
        "No such file or directory:" in failure_message
        or "command not found" in failure_message.lower()
        or error_type == "RecursionError"
        or "maximum recursion depth exceeded" in failure_message.lower()
        or attempt_number >= 4
    ):
        return FailureClass.CAPABILITY_GAP, False

    # 5. Check for Symbol Outside Touched
    symbol_error_types = {"NameError", "ImportError", "ModuleNotFoundError", "AttributeError", "UnboundLocalError"}
    if error_type in symbol_error_types:
        # If unread symbols or frames are outside touched files
        return FailureClass.SYMBOL_OUTSIDE_TOUCHED, False

    # 6. Check for Regression Elsewhere
    # Failure occurring in a test file not associated with touched files
    touched_basenames = {Path(t).name.lower() for t in touched_set}
    has_local_touched = False
    for tid in failing_test_ids:
        tid_lower = tid.lower()
        if any(tb in tid_lower for tb in touched_basenames):
            has_local_touched = True
            break

    # If failure frames are strictly outside touched files and no failing test matches touched set
    if touched_set and outside_frames and not has_local_touched:
        return FailureClass.REGRESSION_ELSEWHERE, False

    # 7. Check for Assertion Local
    if error_type in ("AssertionError", "TestFailure") or "assert" in failure_message.lower():
        return FailureClass.ASSERTION_LOCAL, False

    # Default to Assertion Local if local failure, or Capability Gap if unresolvable
    return FailureClass.ASSERTION_LOCAL, False


def get_git_diff_size(repo_root: Optional[Union[str, Path]]) -> int:
    """Calculate the number of modified lines in the working directory via git diff."""
    if not repo_root:
        return 0
    try:
        res = subprocess.run(
            ["git", "diff", "--shortstat"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            # e.g.: 2 files changed, 10 insertions(+), 3 deletions(-)
            nums = [int(n) for n in re.findall(r"(\d+)", res.stdout)]
            return sum(nums[1:]) if len(nums) > 1 else (nums[0] if nums else 0)
    except Exception:
        pass
    return 0


def verify(
    test_command: str = "pytest",
    output: Optional[str] = None,
    exit_code: Optional[int] = None,
    stderr: Optional[str] = None,
    session_id: Optional[str] = None,
    state: Optional[TaskState] = None,
    repo_root: Optional[Union[str, Path]] = None,
    attempt_number: int = 1,
    prompt: str = "",
    touched_set: Optional[Set[str]] = None,
    db_path: Optional[Union[str, Path]] = None,
    previous_signatures: Optional[Set[str]] = None,
) -> VerificationResult:
    """
    Execute or parse test outputs, diagnose failures, assemble RecoveryContext,
    and persist results to SQLite verifications table.
    """
    t_start = time.time()
    active_root = Path(repo_root).resolve() if repo_root else (state.repo_root if state else Path.cwd().resolve())
    active_session = session_id or (state.session_id if state else f"verify_{int(time.time())}")
    active_touched = set(touched_set) if touched_set is not None else (set(state.touched_set) if state else set())
    active_prompt = prompt or (state.metadata.get("prompt", "") if state else "")

    raw_stdout = ""
    raw_stderr = stderr or ""
    cmd_exit = exit_code

    # 1. Execute test command if output not provided
    if output is None:
        try:
            proc = subprocess.run(
                test_command,
                shell=True,
                cwd=str(active_root),
                capture_output=True,
                text=True,
                timeout=120,
            )
            raw_stdout = proc.stdout
            raw_stderr = proc.stderr
            cmd_exit = proc.returncode
        except subprocess.TimeoutExpired as texc:
            raw_stdout = str(texc.stdout or "")
            raw_stderr = f"TimeoutExpired: Test command '{test_command}' timed out after 120 seconds."
            cmd_exit = -1
        except Exception as exc:
            raw_stderr = f"Execution error: {exc}"
            cmd_exit = 1
    else:
        raw_stdout = output
        if cmd_exit is None:
            cmd_exit = 0 if ("passed" in output.lower() and "failed" not in output.lower() and "error" not in output.lower()) else 1

    duration = round(time.time() - t_start, 4)
    combined_output = f"{raw_stdout}\n{raw_stderr}".strip()
    passed = (cmd_exit == 0)

    # If tests passed, construct successful result
    if passed:
        result = VerificationResult(
            passed=True,
            exit_code=0,
            test_command=test_command,
            failure_class=None,
            failure_message="All tests passed successfully.",
            duration=duration,
            stdout=raw_stdout,
            stderr=raw_stderr,
        )
        _persist_verification(result, session_id=active_session, db_path=db_path)
        return result

    # 2. Parse failing output
    is_pytest = bool(re.search(r"(?:pytest|FAILED|=== FAILURES ===|=+ short test summary info =+)", combined_output))
    if is_pytest:
        parsed = parse_pytest_output(combined_output, repo_root=active_root, touched_set=active_touched)
    else:
        parsed = parse_generic_output(combined_output, exit_code=cmd_exit, repo_root=active_root)

    # 3. Compute failure signature hash
    sig_hash = compute_signature_hash(parsed["error_type"], parsed["top_in_repo_frame"])

    # 4. Classify failure into one of 7 classes
    failure_class, rerun_directive = classify_failure(
        error_type=parsed["error_type"],
        failure_message=parsed["failure_message"],
        failing_test_ids=parsed["failing_test_ids"],
        top_in_repo_frame=parsed["top_in_repo_frame"],
        outside_frames=parsed["outside_frames"],
        unread_symbols=parsed["unread_symbols"],
        touched_set=active_touched,
        signature_hash=sig_hash,
        previous_signatures=previous_signatures,
        attempt_number=attempt_number,
        full_output=combined_output,
    )

    # 5. Construct RecoveryContext
    redacted_stderr = redact_and_truncate_stderr(raw_stderr or combined_output, max_chars=MAX_STDERR_BYTES)
    diff_size = get_git_diff_size(active_root)

    regressions = [tid for tid in parsed["failing_test_ids"] if failure_class == FailureClass.REGRESSION_ELSEWHERE]

    recovery_context = RecoveryContext(
        failure_class=failure_class,
        error_message=parsed["failure_message"],
        test_command=test_command,
        stderr=redacted_stderr,
        attempt_number=attempt_number,
        files_touched=sorted(list(active_touched)),
        session_id=active_session,
        prompt=active_prompt,
        diff_size=diff_size,
        outside_frames=parsed["outside_frames"],
        unread_symbols=parsed["unread_symbols"],
        regressions=regressions,
        signature_hash=sig_hash,
        metadata={
            "failing_test_ids": parsed["failing_test_ids"],
            "top_in_repo_frame": parsed["top_in_repo_frame"],
            "rerun_directive": rerun_directive,
        },
    )

    result = VerificationResult(
        passed=False,
        exit_code=cmd_exit,
        test_command=test_command,
        failure_class=failure_class,
        failure_message=parsed["failure_message"],
        error_type=parsed["error_type"],
        failing_test_ids=parsed["failing_test_ids"],
        frames=parsed["frames"],
        top_in_repo_frame=parsed["top_in_repo_frame"],
        outside_frames=parsed["outside_frames"],
        unread_symbols=parsed["unread_symbols"],
        regressions=regressions,
        signature_hash=sig_hash,
        rerun_directive=rerun_directive,
        recovery_context=recovery_context,
        duration=duration,
        stdout=raw_stdout,
        stderr=raw_stderr,
        metadata={
            "failing_test_ids": parsed["failing_test_ids"],
            "signature_hash": sig_hash,
            "rerun_directive": rerun_directive,
        },
    )

    # 6. Persist to SQLite verifications table
    _persist_verification(result, session_id=active_session, db_path=db_path)

    return result


def intercept_test_output(
    output: str,
    ok: bool,
    meta: Dict[str, Any],
    session_id: Optional[str] = None,
    state: Optional[TaskState] = None,
    touched_set: Optional[Set[str]] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> VerificationResult:
    """
    Convenience interceptor for output produced by run_tests tool calls.
    """
    exit_code = 0 if ok else meta.get("exit_code", 1)
    test_cmd = meta.get("command", "run_tests")
    return verify(
        test_command=test_cmd,
        output=output,
        exit_code=exit_code,
        session_id=session_id,
        state=state,
        touched_set=touched_set,
        db_path=db_path,
    )


def _persist_verification(
    result: VerificationResult,
    session_id: str,
    db_path: Optional[Union[str, Path]] = None,
) -> None:
    """Persist verification record to SQLite verifications table."""
    try:
        init_db(db_path)
        safe_execute_write(
            conn_or_path=db_path,
            sql="INSERT OR IGNORE INTO sessions (session_id) VALUES (?);",
            params=(session_id,),
        )
        safe_execute_write(
            conn_or_path=db_path,
            sql="""
            INSERT INTO verifications (
                session_id, test_command, passed, exit_code, stdout, stderr, duration, failure_class, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            params=(
                session_id,
                result.test_command,
                1 if result.passed else 0,
                result.exit_code,
                result.stdout[:2000] if result.stdout else "",
                result.stderr[:2000] if result.stderr else "",
                result.duration,
                result.failure_class.value if result.failure_class else None,
                json.dumps({
                    "failing_test_ids": result.failing_test_ids,
                    "signature_hash": result.signature_hash,
                    "rerun_directive": result.rerun_directive,
                    "failure_message": result.failure_message,
                    "top_in_repo_frame": result.top_in_repo_frame,
                    "outside_frames": result.outside_frames,
                    "unread_symbols": result.unread_symbols,
                }),
            ),
        )
    except Exception as db_exc:
        logger.debug("Failed to record verification verdict in SQLite: %s", db_exc)

"""
Unit tests for BudgetPilot sandboxed tools, path confinement, exact edit, truncation, and schemas (Phase B).
"""

from pathlib import Path
import tempfile
import pytest

from budgetpilot.agent.tools import (
    MAX_OUTPUT_CHARS,
    TOOL_MAP,
    TOOL_SCHEMAS,
    edit_file,
    execute_tool,
    list_dir,
    read_file,
    resolve_safe_path,
    run_tests,
    search,
)


def test_path_traversal_rejection():
    """Verify sandboxing strictly prevents accessing or escaping files outside the repository root."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir) / "repo"
        repo_root.mkdir()
        outside_file = Path(tmpdir) / "secret.txt"
        outside_file.write_text("super secret", encoding="utf-8")

        # 1. list_dir traversal
        out, ok, meta = list_dir(path="../", repo_root=repo_root)
        assert not ok
        assert meta["error"] == "path_traversal"

        out, ok, meta = list_dir(path="../../", repo_root=repo_root)
        assert not ok

        # 2. read_file traversal
        out, ok, meta = read_file(path="../secret.txt", repo_root=repo_root)
        assert not ok
        assert meta["error"] == "path_traversal"

        # 3. edit_file traversal
        out, ok, meta = edit_file(path="../secret.txt", old="super", new="hacked", repo_root=repo_root)
        assert not ok
        assert meta["error"] == "path_traversal"
        assert outside_file.read_text(encoding="utf-8") == "super secret"

        # 4. search traversal
        out, ok, meta = search(pattern="secret", path="../", repo_root=repo_root)
        assert not ok
        assert meta["error"] == "path_traversal"

        # 5. resolve_safe_path raises PermissionError directly
        with pytest.raises(PermissionError):
            resolve_safe_path("../etc/passwd", repo_root=repo_root)


def test_list_dir_functionality():
    """Verify directory listing with depth control and formatting."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "src").mkdir()
        (root / "src" / "core").mkdir()
        (root / "src" / "core" / "engine.py").write_text("print('engine')", encoding="utf-8")
        (root / "src" / "app.py").write_text("print('app')", encoding="utf-8")
        (root / "README.md").write_text("# Project", encoding="utf-8")

        # Depth 1: lists README.md and src/
        out1, ok1, meta1 = list_dir(path=".", depth=1, repo_root=root)
        assert ok1
        assert "[DIR]  src/" in out1
        assert "[FILE] README.md" in out1
        assert "engine.py" not in out1  # engine is depth 3

        # Depth 2: lists src/app.py and src/core/
        out2, ok2, meta2 = list_dir(path=".", depth=2, repo_root=root)
        assert ok2
        assert "[FILE] app.py" in out2
        assert "[DIR]  core/" in out2

        # Non-existent directory
        out3, ok3, meta3 = list_dir(path="nonexistent_folder", repo_root=root)
        assert not ok3
        assert meta3["error"] == "not_found"


def test_read_file_functionality():
    """Verify reading full files and line slices with 1-indexed line numbers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        file_path = root / "sample.py"
        lines = [f"line {i}" for i in range(1, 11)]
        file_path.write_text("\n".join(lines), encoding="utf-8")

        # Full file read
        out1, ok1, meta1 = read_file(path="sample.py", repo_root=root)
        assert ok1
        assert meta1["total_lines"] == 10
        assert "   1 | line 1" in out1
        assert "  10 | line 10" in out1

        # Sliced read (lines 3 to 6)
        out2, ok2, meta2 = read_file(path="sample.py", start=3, end=6, repo_root=root)
        assert ok2
        assert meta2["lines_read"] == 4
        assert "   3 | line 3" in out2
        assert "   6 | line 6" in out2
        assert "line 2" not in out2
        assert "line 7" not in out2

        # Non-existent file
        out3, ok3, meta3 = read_file(path="does_not_exist.py", repo_root=root)
        assert not ok3
        assert meta3["error"] == "not_found"

        # Reading directory as file
        out4, ok4, meta4 = read_file(path=".", repo_root=root)
        assert not ok4
        assert meta4["error"] == "is_directory"


def test_edit_file_exact_unique_replace():
    """Verify edit_file requires unique matches and correctly performs replacement."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        target_file = root / "calc.py"
        target_file.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

        # 1. Successful unique replacement
        out1, ok1, meta1 = edit_file(
            path="calc.py",
            old="return a - b",
            new="return a + b",
            repo_root=root,
        )
        assert ok1
        assert meta1["replaced"] == 1
        assert target_file.read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"

        # 2. String not found failure
        out2, ok2, meta2 = edit_file(
            path="calc.py",
            old="nonexistent_function()",
            new="something_else()",
            repo_root=root,
        )
        assert not ok2
        assert meta2["error"] == "string_not_found"

        # 3. Non-unique string match failure
        dup_file = root / "dup.txt"
        dup_file.write_text("var = 1\nvar = 1\n", encoding="utf-8")

        out3, ok3, meta3 = edit_file(
            path="dup.txt",
            old="var = 1",
            new="var = 2",
            repo_root=root,
        )
        assert not ok3
        assert meta3["error"] == "not_unique"
        assert meta3["match_count"] == 2
        # File must remain untouched
        assert dup_file.read_text(encoding="utf-8") == "var = 1\nvar = 1\n"


def test_search_functionality():
    """Verify search finds pattern matches across files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "module_a.py").write_text("def compute():\n    magic_token = 'TARGET_XYZ'\n", encoding="utf-8")
        (root / "module_b.py").write_text("def run():\n    pass\n", encoding="utf-8")

        # Search matching pattern
        out1, ok1, meta1 = search(pattern="TARGET_XYZ", path=".", repo_root=root)
        assert ok1
        assert meta1["matches"] >= 1
        assert "module_a.py" in out1
        assert "TARGET_XYZ" in out1

        # Search non-matching pattern
        out2, ok2, meta2 = search(pattern="MISSING_12345", path=".", repo_root=root)
        assert ok2
        assert meta2["matches"] == 0
        assert "No matches found" in out2


def test_run_tests_functionality():
    """Verify test execution with exit code capturing and timeout handling."""
    with tempfile.TemporaryDirectory() as root:
        # 1. Successful command
        out1, ok1, meta1 = run_tests(
            command="python -c \"print('All 5 tests passed')\"",
            repo_root=root,
        )
        assert ok1
        assert meta1["exit_code"] == 0
        assert "All 5 tests passed" in out1

        # 2. Failing command
        out2, ok2, meta2 = run_tests(
            command="python -c \"import sys; sys.stderr.write('AssertionError'); sys.exit(2)\"",
            repo_root=root,
        )
        assert not ok2
        assert meta2["exit_code"] == 2
        assert "AssertionError" in out2

        # 3. Timeout command
        out3, ok3, meta3 = run_tests(
            command="python -c \"import time; time.sleep(5)\"",
            timeout=1,
            repo_root=root,
        )
        assert not ok3
        assert meta3.get("timeout_expired") is True
        assert "timed out after 1 seconds" in out3


def test_output_truncation_enforcement():
    """Verify outputs exceeding 20,000 characters are capped with warning message."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        large_file = root / "large.txt"
        large_content = "0123456789\n" * 3000  # 33,000 characters
        large_file.write_text(large_content, encoding="utf-8")

        out, ok, meta = read_file(path="large.txt", repo_root=root)
        assert ok
        assert meta["truncated"] is True
        assert "[WARNING: Output truncated" in out
        assert len(out) >= MAX_OUTPUT_CHARS
        assert "characters omitted because output exceeded 20000" in out


def test_tool_schemas_and_dispatcher():
    """Verify Ollama-compatible JSON schemas and the execute_tool dispatcher."""
    with tempfile.TemporaryDirectory() as root:
        test_file = Path(root) / "hello.txt"
        test_file.write_text("hello world", encoding="utf-8")

        # Verify 5 schemas
        assert len(TOOL_SCHEMAS) == 5
        schema_names = {s["function"]["name"] for s in TOOL_SCHEMAS}
        assert schema_names == {"list_dir", "search", "read_file", "edit_file", "run_tests"}

        for s in TOOL_SCHEMAS:
            assert s["type"] == "function"
            assert "name" in s["function"]
            assert "description" in s["function"]
            assert "parameters" in s["function"]

        # Test execute_tool dispatcher
        out, ok, meta = execute_tool(
            name="read_file",
            arguments={"path": "hello.txt"},
            repo_root=root,
        )
        assert ok
        assert "hello world" in out

        # Unknown tool
        out_err, ok_err, meta_err = execute_tool(
            name="invalid_tool",
            arguments={},
            repo_root=root,
        )
        assert not ok_err
        assert meta_err["error"] == "unknown_tool"

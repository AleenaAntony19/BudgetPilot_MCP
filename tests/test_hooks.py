"""
Hook engine tests. Every case runs the real hook process (`python -m
budgetpilot.adapters.hooks`) with a payload on stdin, as Claude Code does.
Payloads come from the golden fixtures: real captures from Claude Code 2.1.325.
"""

import json
import os
import random
import sqlite3
import subprocess
import sys

import pytest

from tests.hookutil import (
    FIXTURES, db, fixture, make_repo, pre, run_hook, set_task, start_task, task_row, tool_payload,
)

ALL_FIXTURES = sorted(p.stem for p in FIXTURES.glob("*.json"))
NO_FILE_PROMPT = "Fix the failing addition test."


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "bp_home"
    path.mkdir()
    return path


def read_big(repo, use_id="toolu_r1", **ti):
    return tool_payload(repo, "PreToolUse", "Read", {"file_path": str(repo / "pkg" / "big.py"), **ti}, use_id)


# ============================================================ modes
@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_mode_off_is_silent_and_writes_nothing(name, repo, home):
    rc, out, _ = run_hook(fixture(name, repo), home, mode="off")
    assert (rc, out) == (0, "")
    assert not (home / "bp.db").exists()
    assert not (home / "outbox.jsonl").exists()


def test_default_mode_is_off(repo, home):
    rc, out, _ = run_hook(fixture("PreToolUse__Bash__pytest_full_suite", repo), home, mode=None)
    assert (rc, out) == (0, "") and not (home / "bp.db").exists()


def test_observe_mode_never_prints_but_records(repo, home):
    assert start_task(repo, home, NO_FILE_PROMPT, mode="observe")[1] == ""
    for payload in (
        fixture("SessionStart", repo),
        read_big(repo),
        fixture("PreToolUse__Bash__pytest_full_suite", repo),
        fixture("PostToolUse__Bash__huge_output", repo),
        fixture("PostToolUseFailure__Bash__pytest_fail", repo),
        fixture("Stop", repo),
    ):
        rc, out, _ = run_hook(payload, home, mode="observe")
        assert (rc, out) == (0, ""), payload["hook_event_name"]
    conn = db(home)
    verdicts = {r["rule"]: r["verdict"] for r in conn.execute("SELECT rule, verdict FROM gate_events")}
    assert verdicts["read_window"] == "would_rewrite"
    assert verdicts["bash_full_suite"] == "would_deny"
    obs = conn.execute("SELECT tool, trimmed_tokens_est, truncated FROM tool_obs WHERE trimmed_tokens_est > 0").fetchone()
    assert obs["tool"] == "Bash" and obs["truncated"] == 0  # would have trimmed, did not
    assert conn.execute("SELECT COUNT(*) FROM failure_fingerprints").fetchone()[0] == 1
    assert conn.execute("SELECT outcome FROM recovery_events").fetchone()[0] == "observed"
    task = task_row(home)
    assert task["mode"] == "observe" and task["overhead_tokens_est"] == 0 and task["pending_recovery"] is None


# ============================================================ commands (UserPromptSubmit)
@pytest.mark.parametrize("prompt,mode_after", [
    ("bp_mode_on", "enforce"), ("BP MODE ON", "enforce"), ("/bp_on", "enforce"), ("  bp_mode_observe \n", "observe"),
])
def test_mode_command_is_consumed_without_reaching_the_model(prompt, mode_after, repo, home):
    rc, _, out = run_hook(fixture("UserPromptSubmit", repo, prompt=prompt), home, mode=None)
    assert rc == 0
    assert out["decision"] == "block"
    assert out["hookSpecificOutput"] == {"hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": True}
    assert out["reason"].startswith("BudgetPilot " + {"enforce": "ON", "observe": "OBSERVE"}[mode_after])
    assert json.loads((home / "config.json").read_text())["mode"] == mode_after


def test_mode_off_command_then_hooks_are_silent(repo, home):
    run_hook(fixture("UserPromptSubmit", repo, prompt="bp_mode_on"), home, mode=None)
    assert start_task(repo, home, NO_FILE_PROMPT, mode=None)[2] is not None
    _, _, out = run_hook(fixture("UserPromptSubmit", repo, prompt="bp_mode_off"), home, mode=None)
    assert out["reason"] == "BudgetPilot OFF · hooks are silent."
    assert run_hook(fixture("PreToolUse__Bash__pytest_full_suite", repo), home, mode=None)[1] == ""


def test_budget_and_status_commands(repo, home):
    _, _, out = run_hook(fixture("UserPromptSubmit", repo, prompt="bp_budget tight"), home, mode=None)
    assert out["reason"] == "BudgetPilot budget: tight (mode OFF)."
    assert json.loads((home / "config.json").read_text())["budget_slider"] == "tight"
    _, _, out = run_hook(fixture("UserPromptSubmit", repo, prompt="bp_status"), home, mode=None)
    assert out["decision"] == "block" and out["reason"].startswith("BudgetPilot OFF · budget tight")


def test_env_pinned_mode_is_reported_not_overridden(repo, home):
    _, _, out = run_hook(fixture("UserPromptSubmit", repo, prompt="bp_mode_on"), home, mode="observe")
    assert "BP_MODE environment variable pins the mode" in out["reason"]


@pytest.mark.parametrize("prompt", ["bp_mode_on please", "turn bp_mode_on", "explain bp_status output", "bp_budget huge"])
def test_non_command_prompts_are_not_consumed(prompt, repo, home):
    assert run_hook(fixture("UserPromptSubmit", repo, prompt=prompt), home, mode="off")[1] == ""


# ============================================================ UserPromptSubmit / SessionStart
def test_prompt_creates_task_and_injects_short_scope_note(repo, home):
    rc, _, out = start_task(repo, home, NO_FILE_PROMPT)
    note = out["hookSpecificOutput"]["additionalContext"]
    assert out == {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": note}}
    assert "BudgetPilot output contract" in note  # session had no SessionStart: contract rides along once
    assert "BudgetPilot scope=" in note
    task = task_row(home)
    assert task["mode"] == "enforce" and task["status"] == "active"
    assert task["task_id"] == fixture("UserPromptSubmit", repo)["prompt_id"]
    assert task["prompt_tokens_est"] > 0 and task["overhead_tokens_est"] > 0
    assert task["ctx_budget"] in (6000, 16000, 40000)

    # second prompt in the same session: contract is not repeated, and the note stays under ~60 tokens
    _, _, out2 = start_task(repo, home, "Now fix mul in pkg/calc.py", prompt_id="p2")
    note2 = out2["hookSpecificOutput"]["additionalContext"]
    assert "output contract" not in note2
    assert len(note2.encode()) / 3.8 <= 62


def test_session_start_injects_contract_once(repo, home):
    _, _, out = run_hook(fixture("SessionStart", repo), home)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert out == {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    assert len(text.encode()) / 3.8 <= 90
    assert run_hook(fixture("SessionStart", repo), home)[1] == ""
    assert "output contract" not in start_task(repo, home, NO_FILE_PROMPT)[2]["hookSpecificOutput"]["additionalContext"]
    assert run_hook(fixture("SessionStart", repo, source="resume", session_id="other"), home)[1] == ""


def test_budget_dial_scales_task_budgets(repo, home):
    start_task(repo, home, "Fix add in pkg/calc.py", env_extra={"BP_BUDGET_SLIDER": "tight"})
    tight = task_row(home)
    assert (tight["scope"], tight["ctx_budget"], tight["out_budget"]) == (1, 4500, 1125)


# ============================================================ PreToolUse: Read
def test_read_without_limit_is_windowed_with_updated_input(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    set_task(home, scope=1)
    payload = read_big(repo)
    rc, _, out = run_hook(payload, home)
    assert rc == 0
    assert out == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "BudgetPilot: windowed to 400 lines of 2700",
        "updatedInput": {"file_path": str(repo / "pkg" / "big.py"), "offset": 1, "limit": 400},
        "additionalContext": "BudgetPilot: showing lines 1-400 of 2700 in pkg/big.py. "
                             "Read again with offset=401 if you need more.",
    }}
    event = db(home).execute("SELECT verdict, rule, tokens_avoided_est FROM gate_events").fetchone()
    assert tuple(event) == ("rewrite", "read_window", (2000 - 400) * 12)
    # idempotent: the same payload gives the same output and is not counted twice
    assert run_hook(payload, home)[2] == out
    assert task_row(home)["state"]["reads"] == ["pkg/big.py"]


def test_read_window_keeps_offset_and_respects_small_limits(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    set_task(home, scope=1)
    _, _, out = run_hook(read_big(repo, "a", offset=1000, limit=900), home)
    assert pre(out)["updatedInput"] == {"file_path": str(repo / "pkg" / "big.py"), "offset": 1000, "limit": 400}
    assert run_hook(read_big(repo, "b", offset=100, limit=20), home)[1] == ""      # already narrow
    assert run_hook(read_big(repo, "c", offset=2500), home)[1] == ""                # fewer than 400 lines left
    small = tool_payload(repo, "PreToolUse", "Read", {"file_path": str(repo / "pkg" / "calc.py")}, "d")
    assert run_hook(small, home)[1] == ""                                           # short file


def test_first_read_of_a_named_file_is_never_gated(repo, home):
    start_task(repo, home, "There is a bug somewhere in pkg/big.py, find it.")
    set_task(home, scope=1)
    assert run_hook(read_big(repo, "first"), home)[1] == ""
    assert pre(run_hook(read_big(repo, "second"), home)[2])["updatedInput"]["limit"] == 400


def test_read_cap_on_unnamed_files(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    set_task(home, scope=1)
    for i, name in enumerate(("util_a", "util_b", "util_c")):
        payload = tool_payload(repo, "PreToolUse", "Read", {"file_path": str(repo / "pkg" / f"{name}.py")}, f"u{i}")
        assert run_hook(payload, home)[1] == ""
    fourth = tool_payload(repo, "PreToolUse", "Read", {"file_path": str(repo / "pkg" / "util_d.py")}, "u4")
    out = pre(run_hook(fourth, home)[2])
    assert out["permissionDecision"] == "deny"
    assert "pkg/util_a.py, pkg/util_b.py, pkg/util_c.py" in out["permissionDecisionReason"]
    assert "updatedInput" not in out
    again = tool_payload(repo, "PreToolUse", "Read", {"file_path": str(repo / "pkg" / "util_a.py")}, "u5")
    assert run_hook(again, home)[1] == ""  # re-reading an already counted file is fine


def test_scope_3_does_not_window(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    set_task(home, scope=3)
    assert run_hook(read_big(repo), home)[1] == ""


@pytest.mark.parametrize("target", ["outside", "secret", "symlink"])
def test_paths_outside_repo_and_secrets_are_never_auto_approved(target, repo, home, tmp_path):
    start_task(repo, home, NO_FILE_PROMPT)
    set_task(home, scope=1)
    big = tmp_path / "elsewhere.txt"
    big.write_text("line\n" * 3000)
    if target == "outside":
        path = big
    elif target == "secret":
        path = repo / ".env"
    else:
        path = repo / "link.txt"
        os.symlink(big, path)
    payload = tool_payload(repo, "PreToolUse", "Read", {"file_path": str(path)}, "x1")
    assert run_hook(payload, home)[1] == ""  # no opinion: Claude Code's own permission flow decides
    grep = tool_payload(repo, "PreToolUse", "Grep", {"pattern": "KEY", "path": str(path), "output_mode": "content"}, "x2")
    assert run_hook(grep, home)[1] == ""


# ============================================================ PreToolUse: Grep / Glob
def test_grep_gets_head_limit_via_updated_input(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    set_task(home, scope=1)
    payload = fixture("PreToolUse__Grep", repo)
    _, _, out = run_hook(payload, home)
    assert out == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow",
        "permissionDecisionReason": "BudgetPilot: results capped at 50 lines",
        "updatedInput": {**payload["tool_input"], "head_limit": 50},
    }}
    assert run_hook(fixture("PreToolUse__Grep__head_limit", repo), home)[1] == ""  # head_limit 5 already
    over = fixture("PreToolUse__Grep__head_limit", repo, tool_use_id="g3")
    over["tool_input"]["head_limit"] = 0  # 0 means unlimited
    assert pre(run_hook(over, home)[2])["updatedInput"]["head_limit"] == 50


def test_repo_wide_match_all_search_is_denied(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    set_task(home, scope=1)
    grep = tool_payload(repo, "PreToolUse", "Grep", {"pattern": ".*", "output_mode": "content"}, "g1")
    assert pre(run_hook(grep, home)[2])["permissionDecision"] == "deny"
    glob = fixture("PreToolUse__Glob", repo, tool_use_id="g2")
    glob["tool_input"]["pattern"] = "**/*"
    out = pre(run_hook(glob, home)[2])
    assert out["permissionDecision"] == "deny" and "narrower pattern" in out["permissionDecisionReason"]
    assert run_hook(fixture("PreToolUse__Glob", repo, tool_use_id="g3"), home)[1] == ""  # **/*.py is fine


# ============================================================ PreToolUse: Bash
def test_full_suite_test_run_is_denied_with_a_targeted_suggestion(repo, home):
    start_task(repo, home, "Fix add in pkg/calc.py so tests/test_add.py passes")
    set_task(home, scope=1)
    out = pre(run_hook(fixture("PreToolUse__Bash__pytest_full_suite", repo), home)[2])
    assert out == {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "BudgetPilot: full test-suite runs are not allowed at scope 1. "
                                    "Run the relevant tests only, for example: python -m pytest tests/test_add.py -q",
    }
    assert run_hook(fixture("PreToolUse__Bash__pytest_target", repo), home)[1] == ""


@pytest.mark.parametrize("command,full", [
    ("pytest", True), ("pytest -q", True), ("python -m pytest -x --tb=short", True), ("npm test", True),
    ("go test ./...", True), ("cargo test", True), ("cd pkg && pytest -q", True),
    ("pytest tests/test_add.py", False), ("pytest -k add", False), ("pytest tests/test_add.py::test_add -q", False),
    ("python -m pytest tests -q", False), ("go test ./pkg/calc", False), ("cargo test parser", False),
    ("npm test -- calc.test.js", False), ("pytest --lf", False),
])
def test_full_suite_detection(command, full):
    from budgetpilot.core.ccgate import is_full_suite, is_test_command

    assert is_test_command(command)
    assert is_full_suite(command) is full


@pytest.mark.parametrize("command,rule", [
    ("find / -name '*.py'", "bash_find_root"), ("ls -R", "bash_ls_recursive"),
    ("grep -rn add .", "bash_grep_recursive"), ("cat pkg/big.py", "bash_cat_large"),
])
def test_unbounded_bash_commands_are_denied(command, rule, repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    set_task(home, scope=1)
    payload = tool_payload(repo, "PreToolUse", "Bash", {"command": command}, "b1")
    assert pre(run_hook(payload, home)[2])["permissionDecision"] == "deny"
    assert db(home).execute("SELECT rule FROM gate_events").fetchone()[0] == rule


@pytest.mark.parametrize("command", ["cat pkg/calc.py", "ls pkg", "grep -rn add pkg", "git status", "echo hi"])
def test_ordinary_bash_commands_pass(command, repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    set_task(home, scope=1)
    assert run_hook(tool_payload(repo, "PreToolUse", "Bash", {"command": command}, "b1"), home)[1] == ""


# ============================================================ PreToolUse: edits
def test_large_edit_of_existing_file_is_denied_but_new_files_are_not(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    set_task(home, scope=1)
    body = "\n".join(f"x{i} = {i}" for i in range(61))
    edit = tool_payload(repo, "PreToolUse", "Edit", {
        "file_path": str(repo / "pkg" / "calc.py"), "old_string": "    return a - b", "new_string": body,
        "replace_all": False}, "e1")
    out = pre(run_hook(edit, home)[2])
    assert out["permissionDecision"] == "deny" and "61 lines" in out["permissionDecisionReason"]
    overwrite = tool_payload(repo, "PreToolUse", "Write", {"file_path": str(repo / "pkg" / "calc.py"), "content": body}, "e2")
    assert pre(run_hook(overwrite, home)[2])["permissionDecision"] == "deny"
    new_file = tool_payload(repo, "PreToolUse", "Write", {"file_path": str(repo / "pkg" / "new.py"), "content": body}, "e3")
    assert run_hook(new_file, home)[1] == ""
    assert run_hook(fixture("PreToolUse__Edit", repo), home)[1] == ""  # the real small edit
    assert run_hook(fixture("PreToolUse__Write", repo), home)[1] == ""


def test_edits_are_never_auto_approved(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    for name in ("PreToolUse__Edit", "PreToolUse__Write", "PreToolUse__Bash__pytest_target"):
        _, out, parsed = run_hook(fixture(name, repo), home)
        assert parsed is None or pre(parsed).get("permissionDecision") != "allow"


def test_identical_edit_is_denied_on_the_third_attempt(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    edit = fixture("PreToolUse__Edit", repo)
    assert run_hook({**edit, "tool_use_id": "d1"}, home)[1] == ""
    assert run_hook({**edit, "tool_use_id": "d2"}, home)[1] == ""
    out = pre(run_hook({**edit, "tool_use_id": "d3"}, home)[2])
    assert out["permissionDecision"] == "deny" and "identical edit already attempted 2 times" in out["permissionDecisionReason"]


def test_same_command_is_fine_again_after_an_edit(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    cmd = fixture("PreToolUse__Bash__pytest_target", repo)
    for i in range(2):
        assert run_hook({**cmd, "tool_use_id": f"c{i}"}, home)[1] == ""
    assert pre(run_hook({**cmd, "tool_use_id": "c2"}, home)[2])["permissionDecision"] == "deny"
    run_hook(fixture("PostToolUse__Edit", repo), home)  # an edit landed
    start_denies = task_row(home)["state"]["denies"]
    assert run_hook({**cmd, "tool_use_id": "c4"}, home)[1] == ""
    assert task_row(home)["state"]["denies"] == start_denies


def test_edit_that_already_failed_for_this_signature_is_denied(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    failure = fixture("PostToolUseFailure__Bash__pytest_fail", repo)
    run_hook({**failure, "tool_use_id": "f1"}, home)
    run_hook(fixture("PostToolUse__Edit", repo), home)          # the model tried this edit
    run_hook({**failure, "tool_use_id": "f2"}, home)            # same failure again
    out = pre(run_hook(fixture("PreToolUse__Edit", repo), home)[2])
    assert out["permissionDecision"] == "deny"
    assert "already tried and the same failure followed" in out["permissionDecisionReason"]


# ============================================================ overrides, fallback, budget
def test_repeating_a_denied_call_overrides_it(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    set_task(home, scope=1)
    payload = fixture("PreToolUse__Bash__pytest_full_suite", repo)
    assert pre(run_hook({**payload, "tool_use_id": "o1"}, home)[2])["permissionDecision"] == "deny"
    assert run_hook({**payload, "tool_use_id": "o2"}, home)[1] == ""
    assert db(home).execute("SELECT rule FROM gate_events ORDER BY id DESC").fetchone()[0] == "override"


def test_gate_only_observes_after_three_denials(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    set_task(home, scope=1)
    for i, command in enumerate(("ls -R", "find / -name x", "pytest")):
        payload = tool_payload(repo, "PreToolUse", "Bash", {"command": command}, f"m{i}")
        assert pre(run_hook(payload, home)[2])["permissionDecision"] == "deny"
    fourth = tool_payload(repo, "PreToolUse", "Bash", {"command": "grep -rn x ."}, "m4")
    assert run_hook(fourth, home)[1] == ""
    assert db(home).execute("SELECT verdict FROM gate_events ORDER BY id DESC").fetchone()[0] == "would_deny"


def test_budget_warning_at_80_percent_then_boundary(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    set_task(home, scope=1, ctx_budget=6000, ctx_used_est=5000, out_budget=1500)
    payload = tool_payload(repo, "PreToolUse", "Bash", {"command": "git status"}, "w1")
    out = run_hook(payload, home)[2]
    assert out == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": "BudgetPilot: 80% of the task budget is used (context ~5000/6000 est. tokens, "
                             "output 0/1500). Wrap up.",
    }}
    assert run_hook({**payload, "tool_use_id": "w2"}, home)[1] == ""  # warned once

    set_task(home, ctx_used_est=6100)
    headless = pre(run_hook({**payload, "tool_use_id": "w3"}, home, hitl="0")[2])
    assert headless["permissionDecision"] == "deny" and "task budget reached" in headless["permissionDecisionReason"]
    assert run_hook({**payload, "tool_use_id": "w4"}, home, hitl="0")[1] == ""  # said once, then out of the way


def test_budget_boundary_asks_the_human_and_approval_expands_scope(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    set_task(home, scope=1, ctx_budget=6000, ctx_used_est=6100, out_budget=1500)
    payload = tool_payload(repo, "PreToolUse", "Bash", {"command": "git status"}, "q1")
    out = pre(run_hook(payload, home, hitl="1")[2])
    assert out["permissionDecision"] == "ask" and "expand scope 1 -> 2" in out["permissionDecisionReason"]
    done = tool_payload(repo, "PostToolUse", "Bash", {"command": "git status"}, "q1",
                        tool_response={"stdout": "clean", "stderr": "", "interrupted": False, "isImage": False})
    run_hook(done, home, hitl="1")  # the tool ran, so the user allowed it
    task = task_row(home)
    assert (task["scope"], task["ctx_budget"], task["out_budget"]) == (2, 16000, 4000)


def test_hook_without_a_task_row_has_no_opinion(repo, home):
    assert run_hook(fixture("PreToolUse__Bash__pytest_full_suite", repo), home)[1] == ""


# ============================================================ PostToolUse
def test_noisy_bash_output_is_trimmed_in_the_same_shape(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    payload = fixture("PostToolUse__Bash__huge_output", repo)
    original = payload["tool_response"]
    _, _, out = run_hook(payload, home)
    updated = out["hookSpecificOutput"]["updatedToolOutput"]
    assert out == {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": updated}}
    assert set(updated) == set(original)
    assert {k: v for k, v in updated.items() if k != "stdout"} == {k: v for k, v in original.items() if k != "stdout"}
    assert len(updated["stdout"]) < 6000 < len(original["stdout"])
    marker = updated["stdout"].split("[BudgetPilot trimmed ")[1].split("]")[0]
    raw_path = marker.split("full output: ")[1]
    assert open(raw_path).read().startswith(original["stdout"])  # lossless by reference
    assert oct(os.stat(raw_path).st_mode & 0o777) == "0o600"
    obs = db(home).execute("SELECT result_tokens_est, trimmed_tokens_est, truncated FROM tool_obs").fetchone()
    assert obs["truncated"] == 1 and obs["trimmed_tokens_est"] > 5000
    assert task_row(home)["ctx_used_est"] == obs["result_tokens_est"] - obs["trimmed_tokens_est"]


def test_many_lines_keep_head_and_tail(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    stdout = "\n".join(f"line {i} " + "y" * 60 for i in range(500))
    payload = tool_payload(repo, "PostToolUse", "Bash", {"command": "make build"}, "t1", tool_response={
        "stdout": stdout, "stderr": "", "interrupted": False, "isImage": False, "noOutputExpected": False})
    lines = run_hook(payload, home)[2]["hookSpecificOutput"]["updatedToolOutput"]["stdout"].splitlines()
    assert lines[0].startswith("line 0 ") and lines[-1].startswith("line 499 ")
    assert sum("BudgetPilot trimmed 420 lines" in line for line in lines) == 1


@pytest.mark.parametrize("name", [
    "PostToolUse__Bash__pytest_target", "PostToolUse__Read__no_limit", "PostToolUse__Grep", "PostToolUse__Glob",
    "PostToolUse__Edit", "PostToolUse__Write", "PostToolUse__Bash__other",
])
def test_post_tool_use_is_silent_for_normal_results_and_measures_them(name, repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    assert run_hook(fixture(name, repo), home)[:2] == (0, "")
    obs = db(home).execute("SELECT tool, result_tokens_est FROM tool_obs").fetchone()
    assert obs["tool"] == fixture(name, repo)["tool_name"]
    if "Read" in name:
        assert obs["result_tokens_est"] > 5000 and task_row(home)["ctx_used_est"] == obs["result_tokens_est"]


# ============================================================ failures and fingerprints
def test_failing_test_run_is_fingerprinted_and_auto_directive_given_when_headless(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    _, _, out = run_hook(fixture("PostToolUseFailure__Bash__pytest_fail", repo), home, hitl="0")
    text = out["hookSpecificOutput"]["additionalContext"]
    assert out == {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure", "additionalContext": text}}
    assert text.startswith("BudgetPilot [")
    conn = db(home)
    fp = conn.execute("SELECT * FROM failure_fingerprints").fetchone()
    assert fp["count"] == 1 and fp["failure_class"] == "assertion_local" and len(fp["signature"]) == 16
    event = conn.execute("SELECT * FROM recovery_events").fetchone()
    assert event["decided_by"] == "auto" and event["chosen"] == event["recommended"]
    assert json.loads(event["router_scores"])
    assert task_row(home)["pending_recovery"] is None


def test_fingerprints_persist_across_sessions(repo, home):
    failure = fixture("PostToolUseFailure__Bash__pytest_fail", repo)
    for n, session in enumerate(("session-a", "session-b"), start=1):
        start_task(repo, home, NO_FILE_PROMPT, prompt_id=f"task-{n}")
        run_hook({**failure, "session_id": session, "prompt_id": f"task-{n}", "tool_use_id": f"x{n}"}, home)
        rows = db(home).execute("SELECT count, last_task_id FROM failure_fingerprints").fetchall()
        assert len(rows) == 1 and tuple(rows[0]) == (n, f"task-{n}")


def test_human_in_the_loop_failure_sets_pending_and_edits_ask(repo, home):
    start_task(repo, home, NO_FILE_PROMPT, hitl="1")
    _, _, out = run_hook(fixture("PostToolUseFailure__Bash__pytest_fail", repo), home, hitl="1")
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "AskUserQuestion" in text and "bp_recovery_choose" in text and "5) mode_off" in text
    assert len(text.encode()) / 3.8 <= 125
    task = task_row(home)
    assert task["pending_recovery"] == task["state"]["recovery"]["signature"]

    edit = fixture("PreToolUse__Edit", repo)
    first = pre(run_hook(edit, home, hitl="1")[2])
    assert first["permissionDecision"] == "ask" and "Recommended next step" in first["permissionDecisionReason"]
    assert pre(run_hook({**edit, "tool_use_id": "again"}, home, hitl="1")[2])["permissionDecision"] == "ask"
    assert run_hook({**edit, "tool_use_id": "third"}, home, hitl="1")[1] == ""  # two asks, then stop interfering
    task = task_row(home)
    assert task["pending_recovery"] is None and task["state"]["recovery_skipped"] is True
    read = tool_payload(repo, "PreToolUse", "Read", {"file_path": str(repo / "pkg" / "calc.py")}, "r")
    assert run_hook(read, home, hitl="1")[1] == ""


def test_allowing_the_dialog_records_a_human_retry(repo, home):
    start_task(repo, home, NO_FILE_PROMPT, hitl="1")
    run_hook(fixture("PostToolUseFailure__Bash__pytest_fail", repo), home, hitl="1")
    edit = fixture("PreToolUse__Edit", repo)
    assert pre(run_hook(edit, home, hitl="1")[2])["permissionDecision"] == "ask"
    run_hook(fixture("PostToolUse__Edit", repo), home, hitl="1")  # same tool_use_id: the user allowed it
    event = db(home).execute("SELECT chosen, decided_by FROM recovery_events").fetchone()
    assert tuple(event) == ("retry", "human")
    assert task_row(home)["pending_recovery"] is None


def test_passing_run_after_failure_resolves_and_credits_the_action(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    run_hook(fixture("PostToolUseFailure__Bash__pytest_fail", repo), home)
    run_hook(fixture("PostToolUse__Bash__pytest_target", repo), home)
    conn = db(home)
    event = conn.execute("SELECT chosen, outcome FROM recovery_events").fetchone()
    assert event["outcome"] == "resolved"
    assert json.loads(conn.execute("SELECT resolved_by FROM failure_fingerprints").fetchone()[0]) == {event["chosen"]: 1}
    assert "last_failure" not in task_row(home)["state"]


def test_recovery_attempt_cap(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    failure = fixture("PostToolUseFailure__Bash__pytest_fail", repo)
    texts = [run_hook({**failure, "tool_use_id": f"f{i}"}, home)[2]["hookSpecificOutput"]["additionalContext"]
             for i in range(4)]
    assert all(t.startswith("BudgetPilot [") for t in texts[:3])
    assert "Stop and report to the user" in texts[3]


def test_non_test_failures_are_measured_only(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    payload = fixture("PostToolUseFailure__Bash__pytest_fail", repo)
    payload["tool_input"]["command"] = "python missing.py"
    assert run_hook(payload, home)[1] == ""
    conn = db(home)
    assert conn.execute("SELECT COUNT(*) FROM failure_fingerprints").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tool_obs").fetchone()[0] == 1


# ============================================================ Stop / SessionEnd
def test_stop_closes_the_task_and_never_prints(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    for name in ("Stop", "SessionEnd"):
        assert run_hook(fixture(name, repo), home)[:2] == (0, "")
    assert task_row(home)["status"] == "done"


# ============================================================ robustness
@pytest.mark.parametrize("raw", [
    b"", b"   \n", b"not json{", b"[1, 2, 3]", b'"just a string"', b"null",
    b'{"hook_event_name": "TaskStart"}', b'{"hook_event_name": 7}', b'{"no_event": true}',
    b'{"hook_event_name": "PreToolUse"}', b'{"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": "x"}',
    b'{"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_response": null}',
    b'{"hook_event_name": "UserPromptSubmit", "prompt": 42}',
    b'{"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_input": {"command": "pytest"}, "error": 5}',
    b'\xff\xfe\x00 broken bytes {"hook_event_name": "Stop"}',
    b'{"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "x"}, '
    b'"tool_response": {"stdout": "caf\xc3\xa9 \xff\xfe", "stderr": ""}}',
])
def test_malformed_input_fails_open(raw, home):
    for mode in ("enforce", "observe", "off"):
        rc, out, _ = run_hook(None, home, mode=mode, raw=raw)
        assert (rc, out) == (0, "")


def test_huge_payload(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    payload = fixture("PostToolUse__Bash__huge_output", repo)
    payload["tool_response"]["stdout"] = "z" * 5_000_000
    rc, _, out = run_hook(payload, home)
    assert rc == 0 and len(out["hookSpecificOutput"]["updatedToolOutput"]["stdout"]) < 6000


def test_locked_database_fails_open_and_queues_writes(repo, home):
    start_task(repo, home, NO_FILE_PROMPT)
    set_task(home, scope=1)
    locker = sqlite3.connect(home / "bp.db")
    locker.execute("BEGIN EXCLUSIVE")
    try:
        assert run_hook(fixture("PreToolUse__Bash__pytest_full_suite", repo), home)[:2] == (0, "")
        assert run_hook(fixture("PostToolUse__Bash__pytest_target", repo), home)[:2] == (0, "")
        assert run_hook(fixture("Stop", repo), home)[:2] == (0, "")
    finally:
        locker.rollback()
        locker.close()
    queued = (home / "outbox.jsonl").read_text()
    assert "INSERT INTO tool_obs" in queued
    run_hook(fixture("Stop", repo), home)  # the next Stop flushes the queue
    assert db(home).execute("SELECT COUNT(*) FROM tool_obs").fetchone()[0] == 1
    assert not (home / "outbox.jsonl").exists()


def test_unwritable_home_fails_open(repo, tmp_path):
    blocked = tmp_path / "file_not_dir"
    blocked.write_text("x")
    assert run_hook(fixture("PreToolUse__Read__no_limit", repo), blocked / "home")[:2] == (0, "")


def test_errors_are_logged_not_printed(repo, home):
    (home / "bp.db").write_text("this is not a sqlite database")
    assert run_hook(fixture("PreToolUse__Read__no_limit", repo), home)[:2] == (0, "")


def test_property_any_payload_exits_zero_with_empty_or_valid_output(repo, home):
    """In-process over many random payloads, plus the same sample through the real process."""
    from budgetpilot.adapters import hooks

    start_task(repo, home, NO_FILE_PROMPT)
    rng = random.Random(7)
    events = ["PreToolUse", "PostToolUse", "PostToolUseFailure", "UserPromptSubmit", "Stop", "SessionStart",
              "SessionEnd", "Nope", None, 3]
    tools = ["Read", "Grep", "Glob", "Bash", "Edit", "Write", "MultiEdit", "mcp__x__y", "", None]
    junk = [None, 0, -1, "", "x" * 50, [], {}, {"a": [1, {"b": None}]}, "pytest", "../../etc/passwd", "\x00\ud800",
            str(repo / "pkg" / "big.py"), 10 ** 12, 3.5, True]

    def make():
        payload = {"hook_event_name": rng.choice(events), "session_id": rng.choice(["s", None, 5]),
                   "cwd": rng.choice([str(repo), "", None, "/nonexistent", 9]),
                   "prompt_id": rng.choice([fixture("UserPromptSubmit", repo)["prompt_id"], None, "zz"]),
                   "tool_name": rng.choice(tools), "tool_use_id": rng.choice(["t1", "t2", None, 4]),
                   "prompt": rng.choice(junk), "error": rng.choice(junk), "tool_response": rng.choice(junk),
                   "transcript_path": rng.choice([None, "/nonexistent.jsonl", 3])}
        keys = ["file_path", "command", "pattern", "path", "limit", "offset", "head_limit", "old_string",
                "new_string", "content", "output_mode", "edits"]
        payload["tool_input"] = rng.choice([None, "str", {k: rng.choice(junk) for k in rng.sample(keys, 4)}])
        return payload

    allowed = {"hookSpecificOutput", "decision", "reason"}
    decisions = {"allow", "deny", "ask"}
    os.environ.update(BP_HOME=str(home), BP_MODE="enforce", BP_HITL="1")
    try:
        sample = []
        for i in range(400):
            payload = make()
            raw = json.dumps(payload)
            try:
                out = hooks.handle(raw)
            except Exception:
                out = None  # main() swallows these; covered by the real-process loop below
            if out is not None:
                json.dumps(out)
                assert set(out) <= allowed
                specific = out.get("hookSpecificOutput", {})
                if "permissionDecision" in specific:
                    assert specific["permissionDecision"] in decisions
                if "updatedInput" in specific:
                    assert specific["permissionDecision"] == "allow" and payload["tool_name"] in ("Read", "Grep")
            if i % 40 == 0:
                sample.append(raw)
    finally:
        for key in ("BP_HOME", "BP_MODE", "BP_HITL"):
            os.environ.pop(key, None)
    for raw in sample:
        rc, out, _ = run_hook(None, home, hitl="1", raw=raw.encode("utf-8", errors="replace"))
        assert rc == 0 and (out == "" or isinstance(json.loads(out), dict))


def test_hook_process_imports_no_heavy_modules():
    code = (
        "import sys, io, json; sys.stdin = io.TextIOWrapper(io.BytesIO(b'{}'));"
        "from budgetpilot.adapters import hooks; hooks.main();"
        "print(sorted(m for m in ('pydantic', 'mcp', 'ollama', 'yaml', 'typing', 'dataclasses', 'subprocess',"
        " 'budgetpilot.config', 'budgetpilot.core.store') if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ, "BP_MODE": "off"})
    assert out.stdout.strip() == "[]", out.stdout + out.stderr

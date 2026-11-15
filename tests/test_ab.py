"""Paired A/B harness: tasks and fixture, pairing and statistics, report, and one run through a fake `claude`."""

import json
import shutil
import subprocess
import sys

import pytest

from budgetpilot.bench import ab
from budgetpilot.core.store import init_db
from tests.hookutil import TRANSCRIPT


def test_shipped_tasks_and_fixture():
    tasks = ab.load_tasks()
    assert len(tasks) >= 6 and {t["expected_scope"] for t in tasks} == {1, 2, 3}
    assert len(ab.select_tasks(tasks, smoke=True)) == 3
    assert len(ab.select_tasks(tasks, held_out=True)) >= 2
    for task in tasks:
        assert (ab.DEFAULT_TASKS.parent / task["hidden_test"]).is_file()
        assert task["verify_command"].startswith("python -m pytest")
    with pytest.raises(ValueError):
        ab.select_tasks(tasks, ids=["nope"])
    assert not list(ab.DEFAULT_FIXTURE.rglob("_hidden"))   # hidden tests are not in the fixture


def test_fixture_passes_its_visible_tests_and_fails_every_hidden_test(tmp_path):
    """A no-op agent must not pass: every hidden test fails on the untouched fixture."""
    work = ab.prepare_workdir(ab.DEFAULT_FIXTURE, tmp_path / "repo")
    visible = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=work, capture_output=True, text=True)
    assert visible.returncode == 0, visible.stdout
    settings = json.loads((work / ".claude" / "settings.local.json").read_text())
    assert len(settings["hooks"]) == 7
    for task in ab.load_tasks():
        assert ab.verify(task, work) is False, task["id"]


def test_verify_passes_after_a_correct_fix(tmp_path):
    work = ab.prepare_workdir(ab.DEFAULT_FIXTURE, tmp_path / "repo")
    pricing = work / "shop" / "pricing.py"
    pricing.write_text(pricing.read_text().replace("round(total - percent, 2)", "round(total * (1 - percent / 100), 2)"))
    task = ab.select_tasks(ab.load_tasks(), ids=["t1_discount"])[0]
    assert ab.verify(task, work) is True


def _run(task, arm, repeat, units, passed=True, pair="x"):
    return {"task_id": task, "arm": arm, "repeat": repeat, "passed": passed, "billing_units": units, "turns": 5,
            "out_tok": 100, "cache_hit_ratio": 0.9, "wall_s": 10.0, "pair_id": f"{pair}:{task}:{repeat}", "error": None,
            "hooks": {"gate": {"rewrite:read_window": 2} if arm == "enforce" else {}, "overhead_tokens_est": 40,
                      "hook_errors": False},
            "usage_discrepancy": {"in_tok": 0, "out_tok": 0}}


def test_analysis_pairs_runs_and_reports_ci_with_pass_rates():
    runs = []
    for i, (observe, enforce) in enumerate([(1000, 800), (2000, 1500), (1500, 1200), (1200, 1100)]):
        runs += [_run(f"t{i}", "observe", 0, observe), _run(f"t{i}", "enforce", 0, enforce)]
    a = ab.analyse(runs)
    assert a["pairs"] == 4 and a["mean_change_pct"] == -18.3
    assert a["ci_low_pct"] <= -18.3 <= a["ci_high_pct"] < 0
    assert a["verdict"].startswith("saving")
    assert a["arms"]["observe"]["passed"] == a["arms"]["enforce"]["passed"] == 4
    assert a["gate_rules"] == {"rewrite:read_window": 8} and a["max_usage_discrepancy_tokens"] == 0
    assert a["per_task"]["t0"]["mean_change_pct"] == -20.0 and a["equal_status_pairs"] == 4


def test_a_cheaper_run_that_fails_more_is_not_a_saving():
    runs = []
    for i in range(3):
        runs += [_run(f"t{i}", "observe", 0, 1000, passed=True), _run(f"t{i}", "enforce", 0, 500, passed=i > 0)]
    assert ab.analyse(runs)["verdict"].startswith("not a saving")


def test_inconclusive_and_increase_verdicts():
    mixed = []
    for i, (observe, enforce) in enumerate([(1000, 900), (1000, 1150), (1000, 950)]):
        mixed += [_run(f"t{i}", "observe", 0, observe), _run(f"t{i}", "enforce", 0, enforce)]
    assert ab.analyse(mixed)["verdict"].startswith("inconclusive")
    worse = []
    for i in range(3):
        worse += [_run(f"t{i}", "observe", 0, 1000), _run(f"t{i}", "enforce", 0, 1300 + 10 * i)]
    assert ab.analyse(worse)["verdict"].startswith("no saving")
    assert ab.analyse([_run("t0", "observe", 0, 1000)])["verdict"].startswith("inconclusive")


def test_experiment_order_is_random_but_reproducible_and_report_is_written(tmp_path, monkeypatch):
    tasks = ab.select_tasks(ab.load_tasks(), smoke=True)
    order = []

    def fake_runner(claude, task, arm, repeat, fixture, model, timeout):
        order.append((task["id"], repeat, arm))
        return _run(task["id"], arm, repeat, 800 if arm == "enforce" else 1000)

    result = ab.run_experiment(tasks, "claude", repeats=2, model="haiku", seed=3, out=lambda _: None, runner=fake_runner)
    first = list(order)
    order.clear()
    ab.run_experiment(tasks, "claude", repeats=2, model="haiku", seed=3, out=lambda _: None, runner=fake_runner)
    assert order == first and len(first) == 12
    assert first != sorted(first)                                       # not grouped by arm or task
    assert result["analysis"]["pairs"] == 6

    ab.store_runs(result)
    conn = init_db()
    assert conn.execute("SELECT COUNT(*), COUNT(DISTINCT pair_id) FROM ab_runs").fetchone()[:] == (12, 6)
    conn.close()

    written = ab.write_outputs(result, tmp_path / "out", tmp_path / "RESULTS.md")
    text = (tmp_path / "RESULTS.md").read_text()
    assert json.loads(open(written["json"]).read())["experiment"] == result["experiment"]
    for needle in ("-20.0%", "95% bootstrap CI", "observe 6/6", "enforce 6/6", "## Caveats", "rewrite:read_window",
                   "t6_rename_format_money", "A saving only counts if the pass rate"):
        assert needle in text, needle
    assert ab.main_ab("report", report_json=written["json"], results_md=str(tmp_path / "again.md"), out=lambda _: None) == 0
    assert (tmp_path / "again.md").read_text() == text


def test_run_one_with_a_fake_claude(tmp_path, monkeypatch):
    """The whole per-run path except the real model: workdir, env, JSON result, transcript, hidden test."""
    config = tmp_path / "claude_config"
    (config / "projects" / "p").mkdir(parents=True)
    shutil.copy(TRANSCRIPT, config / "projects" / "p" / "fake-session.jsonl")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    fake = tmp_path / "claude"
    fake.write_text(
        "#!/bin/sh\n"
        'env > "$BP_HOME/env.txt"\n'
        "sed -i 's/round(total - percent, 2)/round(total * (1 - percent \\/ 100), 2)/' shop/pricing.py\n"
        'echo \'{"session_id": "fake-session", "total_cost_usd": 0.0128, "num_turns": 12, "usage": '
        '{"input_tokens": 24, "cache_creation_input_tokens": 35929, "cache_read_input_tokens": 465598, '
        '"output_tokens": 2003}}\'\n'
    )
    fake.chmod(0o755)
    keep = tmp_path / "keep"
    task = ab.select_tasks(ab.load_tasks(), ids=["t1_discount"])[0]
    run = ab.run_one(str(fake), task, "enforce", 0, ab.DEFAULT_FIXTURE, "haiku", 60, keep_dir=keep)
    assert run["passed"] is True and run["error"] is None
    assert (run["in_tok"], run["cache_w"], run["cache_r"], run["out_tok"], run["turns"]) == (24, 35929, 465598, 2003, 12)
    assert run["usage_discrepancy"] == {"in_tok": 0, "cache_w": 0, "cache_r": 0, "out_tok": 0}
    assert run["billing_units"] == 128456.8 and run["total_cost_usd_reported"] == 0.0128
    env = (keep / "t1_discount_enforce_0" / "env.txt").read_text()
    assert "BP_MODE=enforce" in env and "BP_HITL=0" in env and "ANTHROPIC_API_KEY" not in env
    assert "CLAUDECODE=" not in env

    failing = tmp_path / "claude_noop"
    failing.write_text('#!/bin/sh\necho \'{"session_id": "fake-session", "usage": {}}\'\n')
    failing.chmod(0o755)
    assert ab.run_one(str(failing), task, "observe", 0, ab.DEFAULT_FIXTURE, "haiku", 60)["passed"] is False
    broken = tmp_path / "claude_broken"
    broken.write_text("#!/bin/sh\necho not-json\nexit 3\n")
    broken.chmod(0o755)
    bad = ab.run_one(str(broken), task, "observe", 0, ab.DEFAULT_FIXTURE, "haiku", 60)
    assert bad["passed"] is False and "no JSON result" in bad["error"] and bad.get("billing_units") is None


def test_cli_needs_confirmation(monkeypatch, capsys):
    from budgetpilot import cc_cli
    from budgetpilot.cli import main

    monkeypatch.setattr(cc_cli, "find_claude", lambda: "/bin/true")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["ab", "smoke"]) == 1
    out = capsys.readouterr().out
    assert "starts 12 real Claude Code sessions" in out and "Re-run with --yes" in out

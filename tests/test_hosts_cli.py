"""Final step: bp hosts, per-host dashboard panels, doctor host summary."""

import json
import shutil
from pathlib import Path

from budgetpilot import cc_cli
from budgetpilot.core import hookdb
from budgetpilot.hosts.codex_cli import import_all

CODEX = next((Path(__file__).parent / "fixtures" / "codex").glob("rollout-2026-11-19*.jsonl"))


def test_bp_hosts_lists_every_adapter_with_tier_and_open_items():
    lines = []
    assert cc_cli.run_hosts(out=lines.append) == 0
    text = "\n".join(lines)
    for host, tier in (("claude_code", "enforce"), ("gateway", "gateway"), ("generic_mcp", "tools"),
                       ("codex_cli", "observe")):
        assert any(line.startswith(f"{host:<12} {tier}") for line in lines), host
    assert "measured only, no control" in text and "[VERIFY]" in text
    data = []
    cc_cli.run_hosts(as_json=True, out=data.append)
    hosts = {h["id"]: h for h in json.loads(data[0])}
    assert hosts["gateway"]["capabilities"]["hard_token_cap"] and not hosts["codex_cli"]["capabilities"]["proxy_tools"]


def test_dashboard_hides_control_panels_for_a_measure_only_host(tmp_path, monkeypatch):
    from budgetpilot.dashboard import server

    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    shutil.copy(CODEX, home / "sessions" / CODEX.name)
    monkeypatch.setenv("CODEX_HOME", str(home))
    conn = hookdb.open_ready()
    import_all(conn)
    summary = server.api_summary(conn)
    assert summary["host_adapter"]["id"] == "codex_cli"
    assert summary["host_adapter"]["tier_label"] == "measured only, no control"
    assert summary["policy"]["show_gate"] is False and "ref_usd" not in summary["tokens_session"]
    assert 'document.querySelectorAll("[data-control]")' in server.PAGE and server.PAGE.count("data-control>") == 3


def test_dashboard_shows_control_panels_for_claude_code():
    from budgetpilot.dashboard import server

    conn = hookdb.open_ready()
    summary = server.api_summary(conn)
    assert summary["host_adapter"]["id"] == "claude_code" and summary["policy"]["show_gate"] is True


def test_doctor_summarises_each_host():
    lines = []
    cc_cli._doctor_hosts(lambda status, title, detail="", hint="": lines.append(f"[{status}] {title}: {detail}"))
    assert len(lines) == 4 and all(l.startswith("[PASS] Host ") for l in lines)

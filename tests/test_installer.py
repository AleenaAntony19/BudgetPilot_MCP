"""Settings merge/removal and the capture hook (real process)."""

import json
import subprocess
import sys

from budgetpilot import installer


def _user_settings():
    return {
        "model": "sonnet",
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]},
            ]
        },
    }


def test_merge_keeps_foreign_hooks_and_is_idempotent():
    settings = _user_settings()
    groups = installer.build_hook_groups(installer.HOOK_EVENTS, installer.hook_command(installer.HOOK_MODULE))
    installer.merge_hooks(settings, groups, installer.HOOK_MODULE)
    once = json.dumps(settings, sort_keys=True)
    installer.merge_hooks(settings, groups, installer.HOOK_MODULE)
    assert json.dumps(settings, sort_keys=True) == once

    assert settings["model"] == "sonnet"
    commands = [h["command"] for g in settings["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert "my-linter" in commands
    assert sum(installer.HOOK_MODULE in c for c in commands) == 1
    assert set(installer.count_hooks(settings, installer.HOOK_MODULE)) == {e for e, _, _ in installer.HOOK_EVENTS}


def test_remove_restores_original_settings():
    settings = _user_settings()
    groups = installer.build_hook_groups(installer.HOOK_EVENTS, installer.hook_command(installer.HOOK_MODULE))
    installer.merge_hooks(settings, groups, installer.HOOK_MODULE)
    removed = installer.remove_hooks(settings, installer.HOOK_MODULE)
    assert removed == len(installer.HOOK_EVENTS)
    assert settings == _user_settings()


def test_registered_groups_follow_claude_code_schema():
    groups = installer.build_hook_groups(installer.HOOK_EVENTS, "PY -m budgetpilot.adapters.hooks")
    assert "matcher" not in groups["Stop"][0]
    assert groups["PreToolUse"][0]["matcher"] == "Read|Grep|Glob|Bash|Edit|MultiEdit|Write"
    hook = groups["PostToolUseFailure"][0]["hooks"][0]
    assert hook == {"type": "command", "command": "PY -m budgetpilot.adapters.hooks", "timeout": 10}
    assert "TaskStart" not in groups and "TaskEnd" not in groups


def test_register_backs_up_and_unregister_cleans(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    path = repo / ".claude" / "settings.local.json"
    path.write_text(json.dumps(_user_settings()))

    written, backup = installer.register(installer.CAPTURE_MODULE, installer.CAPTURE_EVENTS, scope="project", repo=repo)
    assert written == path and backup is not None and json.loads(backup.read_text()) == _user_settings()
    assert installer.count_hooks(json.loads(path.read_text()), installer.CAPTURE_MODULE)

    _, removed = installer.unregister(installer.CAPTURE_MODULE, scope="project", repo=repo)
    assert removed == len(installer.CAPTURE_EVENTS)
    assert json.loads(path.read_text()) == _user_settings()


def test_hook_command_quotes_paths_with_spaces():
    cmd = installer.hook_command("budgetpilot.adapters.hooks", python="/opt/my env/bin/python")
    assert cmd == "'/opt/my env/bin/python' -m budgetpilot.adapters.hooks"


def test_capture_hook_writes_payload_and_stays_silent(tmp_path):
    env = {"BP_HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "CLAUDE_CODE_MESSAGING_TOKEN": "secret-value"}
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "/x"}}
    proc = subprocess.run([sys.executable, "-m", "budgetpilot.capture_hook"], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0 and proc.stdout == ""
    files = list((tmp_path / "captures").glob("*-PreToolUse-Read-*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    assert record["payload"] == payload
    assert record["env"]["CLAUDE_CODE_MESSAGING_TOKEN"] == "<set>"
    assert "secret-value" not in files[0].read_text()


def test_capture_hook_survives_garbage_input(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "budgetpilot.capture_hook"], input="not json{",
                          capture_output=True, text=True, env={"BP_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0 and proc.stdout == ""

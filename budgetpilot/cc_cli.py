"""
CLI commands for the Claude Code integration: install, uninstall, doctor,
mode, dashboard and transcript-check. `budgetpilot.cli` wires them to `bp`.

Everything `bp install` changes is merged (never overwritten), backed up, and
recorded in `$BP_HOME/install.json` so `bp uninstall` removes exactly that.
"""

import glob
import json
import os
from pathlib import Path
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from budgetpilot import installer
from budgetpilot.core import state

MCP_SERVER_NAME = "budgetpilot"
MCP_MODULE = "budgetpilot.adapters.mcp_server"
CLAUDE_MD_BEGIN = "<!-- budgetpilot:begin -->"
CLAUDE_MD_END = "<!-- budgetpilot:end -->"
CLAUDE_MD_BLOCK = f"""{CLAUDE_MD_BEGIN}
## BudgetPilot commands
If the user's whole message is exactly one of these, call the MCP tool and reply with one line:
- `bp_mode_on`, `bp_mode_off`, `bp_mode_observe` -> tool `bp_mode` with mode `on`, `off` or `observe`
- `bp_dashboard` -> tool `bp_dashboard`
- `bp_status` -> tool `bp_status`
{CLAUDE_MD_END}
"""
EXPECTED_MCP_TOOLS = {
    "bp_status", "bp_set_budget", "bp_request_scope", "bp_report", "bp_list_dir", "bp_search", "bp_read_file",
    "bp_edit_file", "bp_run_tests", "bp_mode", "bp_dashboard", "bp_recovery_choose", "bp_recovery_options",
}
DEFAULT_DASHBOARD_PORT = 8765


# ------------------------------------------------------------------ locating Claude Code
def find_claude() -> Optional[str]:
    """
    The `claude` executable: $BP_CLAUDE_BIN, then PATH, then the binary bundled
    with the Claude Code extension of VS Code-family editors (newest version).
    """
    env = os.environ.get("BP_CLAUDE_BIN")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("claude")
    if found:
        return found
    home = str(Path.home())
    candidates: List[str] = []
    for pattern in (
        ".vscode/extensions", ".vscode-server/extensions", ".antigravity*/extensions", ".cursor/extensions",
        ".windsurf/extensions", ".vscode-oss/extensions",
    ):
        candidates += glob.glob(os.path.join(home, pattern, "anthropic.claude-code-*", "resources", "native-binary",
                                             "claude*"))
    candidates = [c for c in candidates if os.path.isfile(c) and os.access(c, os.X_OK)]
    return max(candidates, key=os.path.getmtime) if candidates else None


def claude_version(binary: str) -> str:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20)
        return out.stdout.strip() or out.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error: {exc}"


# ------------------------------------------------------------------ install manifest
def _manifest_path() -> Path:
    return state.get_bp_home() / "install.json"


def _load_manifest() -> Dict[str, Any]:
    try:
        data = json.loads(_manifest_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_manifest(data: Dict[str, Any]) -> None:
    _manifest_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def _remember(kind: str, entry: Dict[str, Any]) -> None:
    manifest = _load_manifest()
    items = manifest.setdefault(kind, [])
    if entry not in items:
        items.append(entry)
    _save_manifest(manifest)


# ------------------------------------------------------------------ MCP registration
def mcp_server_block(repo: Optional[Path] = None) -> Dict[str, Any]:
    args = ["-m", MCP_MODULE]
    if repo:
        args += ["--repo", str(Path(repo).resolve())]
    block: Dict[str, Any] = {"type": "stdio", "command": sys.executable, "args": args}
    if os.environ.get("BP_HOME"):
        block["env"] = {"BP_HOME": str(state.get_bp_home())}
    return block


def register_mcp(scope: str, repo: Optional[Path], claude: Optional[str]) -> Tuple[bool, str]:
    block = mcp_server_block(repo if scope == "project" else None)
    if not claude:
        snippet = json.dumps({"mcpServers": {MCP_SERVER_NAME: block}}, indent=2)
        return False, (
            "The `claude` command was not found, so the MCP server was not registered.\n"
            "Add it yourself: open /mcp in the Claude Code panel, or put this in .mcp.json:\n" + snippet
        )
    cwd = str(Path(repo).resolve()) if repo else None
    subprocess.run([claude, "mcp", "remove", "--scope", scope, MCP_SERVER_NAME], capture_output=True, text=True,
                   cwd=cwd, timeout=60)
    command = [claude, "mcp", "add", "--scope", scope, MCP_SERVER_NAME]
    for key, value in (block.get("env") or {}).items():
        command += ["-e", f"{key}={value}"]
    command += ["--", block["command"], *block["args"]]
    try:
        out = subprocess.run(command, capture_output=True, text=True, cwd=cwd, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"`claude mcp add` failed: {exc}"
    if out.returncode != 0:
        return False, f"`claude mcp add` failed: {(out.stderr or out.stdout).strip()[:300]}"
    _remember("mcp", {"scope": scope, "repo": cwd})
    return True, f"MCP server '{MCP_SERVER_NAME}' registered ({scope} scope)."


def unregister_mcp(claude: Optional[str]) -> List[str]:
    messages = []
    for entry in _load_manifest().get("mcp", []):
        if not claude:
            messages.append(f"MCP server ({entry['scope']} scope) not removed: `claude` command not found. "
                            f"Run: claude mcp remove --scope {entry['scope']} {MCP_SERVER_NAME}")
            continue
        out = subprocess.run([claude, "mcp", "remove", "--scope", entry["scope"], MCP_SERVER_NAME],
                             capture_output=True, text=True, cwd=entry.get("repo"), timeout=60)
        messages.append(f"MCP server removed ({entry['scope']} scope)." if out.returncode == 0
                        else f"MCP server ({entry['scope']} scope) was already gone.")
    return messages


# ------------------------------------------------------------------ CLAUDE.md fallback
def claude_md_path(scope: str, repo: Optional[Path]) -> Path:
    if scope == "user":
        return installer.claude_config_dir() / "CLAUDE.md"
    return (Path(repo).resolve() if repo else Path.cwd()) / "CLAUDE.md"


def add_claude_md(path: Path) -> bool:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if CLAUDE_MD_BEGIN in text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    separator = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    path.write_text(text + separator + CLAUDE_MD_BLOCK, encoding="utf-8")
    return True


def remove_claude_md(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    start, end = text.find(CLAUDE_MD_BEGIN), text.find(CLAUDE_MD_END)
    if start < 0 or end < 0:
        return False
    cleaned = (text[:start].rstrip("\n") + "\n" + text[end + len(CLAUDE_MD_END):].lstrip("\n")).strip("\n")
    if cleaned:
        path.write_text(cleaned + "\n", encoding="utf-8")
    else:
        path.unlink()
    return True


# ------------------------------------------------------------------ install / uninstall
BILLING_CHOICES = {"1": "subscription", "2": "api", "3": "cloud"}


def set_intended_billing(mode: Optional[str]) -> Optional[str]:
    """Store billing.intended_mode in config.json (None leaves it as it is)."""
    if mode not in ("subscription", "api", "cloud"):
        return None
    cfg = state.read_config()
    section = dict(cfg.get("billing") or {})
    section["intended_mode"] = mode
    state.update_config(billing=section)
    return mode


def _ask_intended_billing(out: Callable[[str], None]) -> Optional[str]:
    out("How do you pay for Claude Code? This only decides whether BudgetPilot warns you when a session")
    out("would be billed differently, and whether it shows dollar figures.")
    out("  1) Pro / Max / Team plan   2) API key (per token)   3) cloud account (Bedrock, Vertex, Foundry)")
    answer = input("Choose 1-3, or press Enter to skip: ").strip()
    return BILLING_CHOICES.get(answer)


def run_install(scope: str = "user", repo: Optional[str] = None, hooks: bool = False, mcp: bool = False,
                claude_md: bool = False, yes: bool = False, out: Callable[[str], None] = print,
                billing: Optional[str] = None) -> int:
    repo_path = Path(repo).resolve() if repo else (Path.cwd() if scope == "project" else None)
    if not (hooks or mcp or claude_md):
        hooks = mcp = True
    settings = installer.settings_path(scope, repo_path)
    claude = find_claude()

    plan = []
    if hooks:
        plan.append(f"merge BudgetPilot hooks into {settings} (a backup is kept)")
    if mcp:
        plan.append(f"register the MCP server '{MCP_SERVER_NAME}' ({scope} scope)"
                    + ("" if claude else " - `claude` not found, instructions will be printed"))
    if claude_md:
        plan.append(f"add the command block to {claude_md_path(scope, repo_path)}")
    out("BudgetPilot install will:")
    for item in plan:
        out(f"  - {item}")
    if not yes:
        if not sys.stdin.isatty():
            out("Not confirmed. Re-run with --yes.")
            return 1
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            out("Cancelled.")
            return 1

    if hooks:
        path, backup = installer.register(installer.HOOK_MODULE, installer.HOOK_EVENTS, scope=scope, repo=repo_path)
        _remember("hooks", {"settings": str(path)})
        out(f"Hooks registered in {path}" + (f" (backup: {backup.name})" if backup else ""))
    if mcp:
        _, message = register_mcp(scope, repo_path, claude)
        out(message)
    if claude_md:
        path = claude_md_path(scope, repo_path)
        added = add_claude_md(path)
        _remember("claude_md", {"path": str(path)})
        out(f"Command block {'added to' if added else 'already in'} {path}")

    from budgetpilot.core.store import init_db

    init_db().close()
    intended = billing
    if intended is None and not yes and sys.stdin.isatty() and not _intended_billing():
        intended = _ask_intended_billing(out)
    if set_intended_billing(intended):
        out(f"Billing: you use {intended} billing (change with: bp install --billing ... or config.json).")
    if claude:
        from budgetpilot.hosts.billing import refresh_auth_cache

        refresh_auth_cache(claude)  # caches login method and plan only; no personal fields
    out("")
    out(f"Done. Mode is {state.get_mode().upper()} (the mode is shared by all projects).")
    out("In Claude Code, type bp_mode_on to start and bp_dashboard to open the dashboard.")
    out("Hooks are read when a session starts: restart open Claude Code sessions.")
    return 0


def run_uninstall(out: Callable[[str], None] = print) -> int:
    manifest = _load_manifest()
    removed_any = False
    for entry in manifest.get("hooks", []):
        path = Path(entry["settings"])
        if path.exists():
            settings = installer.load_settings(path)
            removed = installer.remove_hooks(settings, installer.HOOK_MODULE)
            if removed:
                installer.write_settings(path, settings)
            out(f"Removed {removed} hook entries from {path}")
            removed_any = removed_any or bool(removed)
    for message in unregister_mcp(find_claude()):
        out(message)
        removed_any = True
    for entry in manifest.get("claude_md", []):
        if remove_claude_md(Path(entry["path"])):
            out(f"Removed the command block from {entry['path']}")
            removed_any = True
    try:
        _manifest_path().unlink()
    except OSError:
        pass
    if not removed_any:
        out("Nothing to remove: BudgetPilot was not installed through `bp install`.")
    out(f"Your data in {state.get_bp_home()} was left in place.")
    return 0


# ------------------------------------------------------------------ doctor
def doctor_payloads(repo: str) -> Dict[str, Dict[str, Any]]:
    """Representative payloads in the documented contract (docs/HOOK_CONTRACT.md)."""
    base = {"session_id": "bp-doctor", "transcript_path": os.path.join(repo, "none.jsonl"), "cwd": repo,
            "prompt_id": "bp-doctor-task", "permission_mode": "default", "effort": {"level": "medium"}}
    big = os.path.join(repo, "big.py")
    return {
        "UserPromptSubmit": {**base, "hook_event_name": "UserPromptSubmit", "prompt": "Fix the bug in the parser."},
        "PreToolUse Read": {**base, "hook_event_name": "PreToolUse", "tool_name": "Read",
                            "tool_input": {"file_path": big}, "tool_use_id": "d1"},
        "PreToolUse Bash": {**base, "hook_event_name": "PreToolUse", "tool_name": "Bash",
                            "tool_input": {"command": "git status"}, "tool_use_id": "d2"},
        "PostToolUse Bash": {**base, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                             "tool_input": {"command": "git status"}, "tool_use_id": "d2", "duration_ms": 5,
                             "tool_response": {"stdout": "clean", "stderr": "", "interrupted": False,
                                               "isImage": False, "noOutputExpected": False}},
        "Stop": {**base, "hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "done"},
    }


def measure_hooks(runs: int = 10) -> Dict[str, Any]:
    """
    Wall time of the real hook process per invocation, in an isolated temporary
    BP_HOME (the user's data is not touched). Also checks the Read rewrite.
    """
    results: Dict[str, Any] = {"timings": {}, "ok": True, "errors": []}
    with tempfile.TemporaryDirectory(prefix="bp-doctor-") as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        with open(os.path.join(repo, "big.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n" * 3000)
        open(os.path.join(repo, "none.jsonl"), "w").close()  # an (empty) transcript, so Stop does not wait for one
        home = os.path.join(tmp, "home")
        command = [sys.executable, "-m", installer.HOOK_MODULE]
        env = {k: v for k, v in os.environ.items() if not k.startswith("BP_")}

        def call(payload: Dict[str, Any], mode: str) -> Tuple[float, subprocess.CompletedProcess]:
            started = time.perf_counter()
            proc = subprocess.run(command, input=json.dumps(payload), capture_output=True, text=True,
                                  env={**env, "BP_HOME": home, "BP_MODE": mode, "BP_HITL": "0"}, timeout=60)
            return (time.perf_counter() - started) * 1000.0, proc

        payloads = doctor_payloads(repo)
        for name, payload in payloads.items():
            samples = []
            for i in range(max(1, runs)):
                elapsed, proc = call({**payload, "tool_use_id": f"{payload.get('tool_use_id', 'x')}-{i}"}, "enforce")
                samples.append(elapsed)
                if proc.returncode != 0:
                    results["ok"] = False
                    results["errors"].append(f"{name}: exit code {proc.returncode}")
                if proc.stdout.strip():
                    try:
                        parsed = json.loads(proc.stdout)
                    except ValueError:
                        results["ok"] = False
                        results["errors"].append(f"{name}: stdout is not JSON")
                        continue
                    if name == "PreToolUse Read" and i == 0:
                        updated = parsed.get("hookSpecificOutput", {}).get("updatedInput", {})
                        results["read_rewrite_limit"] = updated.get("limit")
            results["timings"][name] = _percentiles(samples)
        off = [call(payloads["PreToolUse Bash"], "off")[0] for _ in range(max(1, runs))]
        results["timings"]["mode off"] = _percentiles(off)
        bare = []
        for _ in range(max(1, runs)):
            started = time.perf_counter()
            subprocess.run([sys.executable, "-c", "pass"], capture_output=True, timeout=60)
            bare.append((time.perf_counter() - started) * 1000.0)
        results["timings"]["python start-up alone"] = _percentiles(bare)
        log = os.path.join(home, "hook_errors.log")
        if os.path.exists(log):
            results["ok"] = False
            results["errors"].append(open(log, encoding="utf-8").read()[-300:])
    return results


def _percentiles(samples: List[float]) -> Dict[str, float]:
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return {"p50": round(statistics.median(ordered), 1), "p95": round(p95, 1), "n": len(ordered)}


def mcp_handshake(timeout: float = 60.0) -> Dict[str, Any]:
    """Start the MCP server over stdio with the installed `mcp` client and list its tools."""
    code = (
        "import asyncio, json, sys\n"
        "from mcp import ClientSession, StdioServerParameters\n"
        "from mcp.client.stdio import stdio_client\n"
        "async def main():\n"
        f"    params = StdioServerParameters(command=sys.executable, args=['-m', '{MCP_MODULE}'])\n"
        "    async with stdio_client(params) as (read, write):\n"
        "        async with ClientSession(read, write) as session:\n"
        "            await session.initialize()\n"
        "            tools = (await session.list_tools()).tools\n"
        "            print(json.dumps([{'name': t.name, 'description': t.description or '',\n"
        "                               'schema': getattr(t, 'input_schema', None) or getattr(t, 'inputSchema', None)}\n"
        "                              for t in tools]))\n"
        "asyncio.run(main())\n"
    )
    with tempfile.TemporaryDirectory(prefix="bp-doctor-mcp-") as tmp:
        env = {**os.environ, "BP_HOME": os.path.join(tmp, "home")}
        try:
            proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.SubprocessError as exc:
            return {"ok": False, "error": str(exc)}
    lines = [line for line in proc.stdout.splitlines() if line.startswith("[")]
    if proc.returncode != 0 or not lines:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip()[-400:]}
    tools = json.loads(lines[-1])
    from budgetpilot.core.ccgate import est_tokens

    names = {t["name"] for t in tools}
    longest = max((len(t["description"].split()) for t in tools), default=0)
    return {
        "ok": names == EXPECTED_MCP_TOOLS, "tools": sorted(names),
        "missing": sorted(EXPECTED_MCP_TOOLS - names), "unexpected": sorted(names - EXPECTED_MCP_TOOLS),
        "schema_tokens_est": est_tokens(json.dumps(tools)), "longest_description_words": longest,
    }


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def run_doctor(claude: bool = False, runs: int = 10, out: Callable[[str], None] = print) -> int:
    """PASS / WARN / FAIL per check, each with a fix hint. Exit code 1 if anything failed."""
    failures = 0

    def report(status: str, title: str, detail: str = "", hint: str = "") -> None:
        nonlocal failures
        failures += status == "FAIL"
        out(f"[{status}] {title}" + (f": {detail}" if detail else ""))
        if hint and status != "PASS":
            out(f"       fix: {hint}")

    out("BudgetPilot doctor")
    out("")
    version = sys.version_info
    report("PASS" if version >= (3, 11) else "FAIL", "Python >= 3.11", f"{version.major}.{version.minor}.{version.micro}",
           "install Python 3.11 or newer")

    try:
        from importlib.metadata import version as package_version

        mcp_version = package_version("mcp")
        ok = int(mcp_version.split(".")[0]) >= 2
        report("PASS" if ok else "FAIL", "mcp >= 2", mcp_version, 'pip install "mcp>=2.0.0"')
    except Exception as exc:
        report("FAIL", "mcp >= 2", str(exc), 'pip install "mcp>=2.0.0"')

    _doctor_billing(report)
    _doctor_prices(report)

    home = state.get_bp_home()
    try:
        from budgetpilot.core.store import LATEST_SCHEMA_VERSION, get_current_schema_version, init_db

        conn = init_db()
        current = get_current_schema_version(conn)
        conn.execute("INSERT INTO connections (name, kind, last_seen, detail) VALUES ('doctor', 'cli', ?, '{}') "
                     "ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen", (time.time(),))
        conn.commit()
        conn.close()
        report("PASS" if current == LATEST_SCHEMA_VERSION else "FAIL", "Database writable",
               f"{home / 'bp.db'} (schema v{current})", "check file permissions on the BudgetPilot home directory")
    except Exception as exc:
        report("FAIL", "Database writable", str(exc)[:200], f"check permissions on {home}")

    try:
        mode_bits = home.stat().st_mode & 0o777
        open_to_others = bool(mode_bits & 0o022)
        report("WARN" if open_to_others else "PASS", "Home directory permissions", f"{home} is {oct(mode_bits)[2:]}",
               f"chmod 700 {home}")
    except OSError as exc:
        report("WARN", "Home directory permissions", str(exc))

    out(f"       mode: {state.get_mode()} · budget: {state.get_budget_slider()} · "
        f"human-in-the-loop: {'on' if state.hitl_enabled() else 'off'}")

    measured = measure_hooks(runs)
    detail = "exit 0 and valid output for every payload" if measured["ok"] else "; ".join(measured["errors"])[:300]
    report("PASS" if measured["ok"] else "FAIL", "Hook round-trip (real process)", detail,
           f"see {home / 'hook_errors.log'}")
    limit = measured.get("read_rewrite_limit")
    report("PASS" if limit else "FAIL", "Read of a 3,000-line file is windowed by updatedInput",
           f"limit={limit}" if limit else "no rewrite returned")
    out(f"       hook latency, wall time per invocation over {runs} runs each (this machine):")
    for name, t in measured["timings"].items():
        out(f"         {name:<24} p50 {t['p50']:>6.1f} ms   p95 {t['p95']:>6.1f} ms")

    handshake = mcp_handshake()
    if handshake.get("ok"):
        report("PASS", "MCP server handshake", f"{len(handshake['tools'])} tools")
    else:
        problem = handshake.get("error") or f"missing {handshake.get('missing')}, unexpected {handshake.get('unexpected')}"
        report("FAIL", "MCP server handshake", str(problem)[:300], f"{sys.executable} -m {MCP_MODULE}")
    if "schema_tokens_est" in handshake:
        out(f"       resident MCP schema: ~{handshake['schema_tokens_est']} tokens (estimated), "
            f"longest description {handshake['longest_description_words']} words")

    report("PASS" if port_is_free(DEFAULT_DASHBOARD_PORT) else "WARN", f"Dashboard port {DEFAULT_DASHBOARD_PORT}",
           "free" if port_is_free(DEFAULT_DASHBOARD_PORT) else "in use (the dashboard will pick the next free port)")

    if claude:
        out("")
        out("Claude Code")
        binary = find_claude()
        report("PASS" if binary else "FAIL", "claude executable", f"{binary} ({claude_version(binary)})" if binary else "",
               "install Claude Code, or set BP_CLAUDE_BIN to the claude binary")
        events = {event for event, _, _ in installer.HOOK_EVENTS}
        found: Dict[str, List[str]] = {}
        for scope, repo in (("user", None), ("project", Path.cwd())):
            path = installer.settings_path(scope, repo)
            try:
                registered = set(installer.count_hooks(installer.load_settings(path), installer.HOOK_MODULE))
            except (OSError, ValueError):
                registered = set()
            if registered:
                found[str(path)] = sorted(events - registered)
        if not found:
            report("FAIL", "Hooks registered", "not found in user or project settings", "bp install --yes")
        for path, missing in found.items():
            report("FAIL" if missing else "PASS", "Hooks registered", f"{path}" + (f" (missing {missing})" if missing else
                   f" ({len(events)} events)"), "bp install --yes")
        from budgetpilot.core import cc_ledger

        newest = cc_ledger.newest_transcript()
        if not newest:
            report("WARN", "Transcript parse", "no Claude Code transcript found yet")
        else:
            try:
                check = cc_ledger.check_transcript(newest)
                usable = check["assistant_rows"] == 0 or check["assistant_rows_with_usage"] > 0
                report("PASS" if usable else "FAIL", "Transcript parse",
                       f"{check['unique_messages']} messages, {check['totals']['output_tokens']} output tokens "
                       f"({os.path.basename(newest)})", "run: bp transcript-check")
            except Exception as exc:
                report("FAIL", "Transcript parse", str(exc)[:200])
        try:
            from budgetpilot.core import hookdb

            conn = hookdb.open_ready()
            calibration = cc_ledger.calibration(conn) if conn else {"ratio": None}
            if conn:
                conn.close()
            if calibration.get("ratio") is None:
                out("       estimator calibration: no data yet")
            else:
                report("PASS" if calibration["ok"] else "WARN", "Estimator calibration (derived)",
                       f"estimated / derived-actual = {calibration['ratio']} over {calibration['tasks']} tasks",
                       'adjust "chars_per_token" in config.json')
        except Exception:
            pass

    out("")
    out("All checks passed." if not failures else f"{failures} check(s) failed.")
    return 1 if failures else 0


# ------------------------------------------------------------------ small commands
def _intended_billing() -> Optional[str]:
    from budgetpilot.hosts.billing import intended_mode

    return intended_mode(state.read_config())


def _doctor_billing(report: Callable[..., None]) -> None:
    """Billing detection and the key-set check. Variable names only, never values."""
    from budgetpilot.hosts.billing import KEY_VARS, detect_claude_code, read_auth_cache, refresh_auth_cache

    cfg = state.read_config()
    intended = _intended_billing()
    binary = find_claude()
    if binary:
        refresh_auth_cache(binary)
    for name in KEY_VARS:
        if not os.environ.get(name):
            report("PASS", f"{name} is not set")
        elif intended == "subscription":
            report("FAIL", f"{name} is set", "you said you use a plan, but this shell bills the API per token",
                   f"unset {name}")
        elif intended in ("api", "cloud"):
            report("PASS", f"{name} is set", f"expected: you said you use {intended} billing")
        else:
            report("WARN", f"{name} is set", "Claude Code will bill the API instead of your Pro/Max plan",
                   f"unset {name}")
    billing = detect_claude_code(os.environ, cwd=os.getcwd(), config=cfg)
    label = f"{billing.mode} ({billing.confidence} confidence" + (", set by you" if billing.source == "user_override"
                                                                   else "") + f"), provider {billing.provider}"
    evidence = "; ".join(billing.evidence) or "no signal"
    report("PASS" if billing.mode != "unknown" else "WARN", "Billing detection", f"{label}: {evidence}",
           "run `bp doctor` with the `claude` command available, or set billing.mode_override in config.json")
    auth = read_auth_cache()
    if auth is None:
        report("WARN", "Claude Code login cache", "not cached yet", "run bp doctor where `claude` is available")


def maybe_auto_refresh() -> Optional[Dict[str, Any]]:
    """`prices.auto_refresh: true` in config.json: refresh a stale price book (CLI only, never a hook)."""
    from budgetpilot.core import pricebook

    section = state.read_config().get("prices") or {}
    if not (isinstance(section, dict) and section.get("auto_refresh")) or not pricebook.is_stale():
        return None
    return pricebook.refresh()


def _doctor_prices(report: Callable[..., None]) -> None:
    from budgetpilot.core import pricebook

    maybe_auto_refresh()
    book = pricebook.load(force=True)
    age = pricebook.age_days(book)
    report("WARN" if pricebook.is_stale(book) else "PASS", "Price book",
           f"fetched {book.get('fetched_at') or 'never'}" + (f", {age:.0f} days ago" if age is not None else "")
           + f", {len(book['entries'])} rates", "bp prices refresh")
    try:
        from budgetpilot.core import hookdb

        conn = hookdb.open_ready()
        if conn is None:
            return
        try:
            since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 30 * 86400))
            models = [r[0] for r in conn.execute(
                "SELECT DISTINCT model FROM cc_turns WHERE ts >= ? AND model IS NOT NULL", (since,))]
        finally:
            conn.close()
        unpriced = [m for m in models if not pricebook.price_for("anthropic", m, None, book)]
        if unpriced:
            report("WARN", "Unpriced models in the last 30 days", ", ".join(unpriced),
                   "bp prices refresh, or bp prices set anthropic MODEL ...")
    except Exception:
        pass


def run_mode(mode: str, out: Callable[[str], None] = print) -> int:
    from budgetpilot.core import commands

    out(commands.set_mode_text(mode))
    return 0


def run_dashboard(port: Optional[int] = None, open_browser: bool = True, stop: bool = False,
                  out: Callable[[str], None] = print) -> int:
    try:
        from budgetpilot import dashboard
    except ImportError as exc:
        out(f"BudgetPilot: the dashboard is not available ({exc}).")
        return 1
    if not stop:
        try:
            maybe_auto_refresh()  # only with prices.auto_refresh: true, and only when the book is stale
        except Exception:
            pass
    if stop:
        out("Dashboard stopped." if dashboard.stop() else "The dashboard was not running.")
        return 0
    try:
        out(f"BudgetPilot dashboard → {dashboard.ensure_running(port=port, open_browser=open_browser)}")
        return 0
    except Exception as exc:
        out(f"BudgetPilot: could not start the dashboard ({exc}).")
        return 1


def current_policy() -> Dict[str, Any]:
    """Display policy for this shell's Claude Code billing (detected now, presence only)."""
    from budgetpilot.hosts import get_adapter
    from budgetpilot.hosts.billing import detect_claude_code
    from budgetpilot.hosts.policy import report_policy

    billing = detect_claude_code(os.environ, cwd=os.getcwd(), config=state.read_config())
    return report_policy(billing, get_adapter("claude_code").capabilities())


def money(value: Any, policy: Dict[str, Any]) -> Optional[str]:
    from budgetpilot.hosts.policy import money as _money

    return _money(value, policy)


def run_transcript_check(path: Optional[str] = None, as_json: bool = False, out: Callable[[str], None] = print) -> int:
    from budgetpilot.core import cc_ledger

    target = path or cc_ledger.newest_transcript()
    if not target or not os.path.isfile(target):
        out("No transcript found. Pass a path: bp transcript-check ~/.claude/projects/<project>/<session>.jsonl")
        return 1
    report = cc_ledger.check_transcript(target)
    if as_json:
        from budgetpilot.hosts.policy import strip_dollars

        policy = current_policy()
        out(json.dumps({**strip_dollars(report, policy), "policy": policy}, indent=2))
        return 0
    totals = report["totals"]
    out(f"Transcript: {report['path']}")
    out(f"  rows {report['rows']} (unparsable {report['unparsable_rows']}), assistant rows {report['assistant_rows']}")
    out(f"  with usage {report['assistant_rows_with_usage']}, without usage {report['assistant_rows_without_usage']}")
    out(f"  unique messages {report['unique_messages']} (up to {report['max_rows_per_message']} rows per message)")
    out(f"  rows with thinking_tokens {report['rows_with_thinking_tokens']}, "
        f"with 5m/1h cache-write split {report['rows_with_cache_write_split']}")
    out(f"  tokens: input {totals['input_tokens']:,} · cache write {totals['cache_write']:,} · "
        f"cache read {totals['cache_read']:,} · output {totals['output_tokens']:,} "
        f"(thinking {totals['thinking_tokens']:,}, included in output)")
    policy = current_policy()
    dollars = money(report["ref_usd"], policy)
    out(f"  cache-hit ratio {totals['cache_hit_ratio']} · {policy['units_label']} {report['billing_units']:,}"
        + (f" · {policy['dollars_label']} {dollars}" if dollars else "")
        + (" (price guessed: unknown model)" if report["price_guess"] and dollars else ""))
    out(f"  billing: {policy['billing_mode']}" + (" (set by you)" if policy["set_by_user"] else "")
        + f". {policy['note']}")
    out(f"  models: {', '.join(report['models']) or 'none'}")
    out("Compare with `claude -p --output-format json` usage for the same session; they should match.")
    return 0


# ------------------------------------------------------------------ prices, reconcile, status line
def run_prices(action: str = "show", confirm: bool = False, as_json: bool = False, provider: str = "anthropic",
               model: Optional[str] = None, rates: Optional[Dict[str, float]] = None, source_url: str = "",
               out: Callable[[str], None] = print) -> int:
    from budgetpilot.core import pricebook

    if action == "refresh":
        report = pricebook.refresh(providers=(provider,), confirm=confirm)
        if as_json:
            out(json.dumps(report, indent=2))
            return 0 if report["ok"] else 1
        for name, status in report["providers"].items():
            out(f"{name}: {status}")
        if report.get("error"):
            out(report["error"])
            return 1
        out(f"{len(report['changes'])} rate change(s), {len(report['new_models'])} new model(s); "
            f"written to {report['written']} (previous copy: .bak)")
        for model_id, key, old, new in report["changes"]:
            out(f"  {model_id} {key}: {old} -> {new} per MTok (valid from today; older usage keeps the old rate)")
        return 0
    if action == "set":
        if not model or not rates:
            out("usage: bp prices set PROVIDER MODEL --input X --cache-write-5m X --cache-write-1h X "
                "--cache-read X --output X [--source URL]")
            return 1
        try:
            entry = pricebook.set_rates(provider, model, rates, source_url or "set by user")
        except pricebook.PriceBookError as exc:
            out(f"rejected: {exc}")
            return 1
        out(f"Set {provider}/{model} from today: " + ", ".join(f"{k} {v}" for k, v in entry["rates"].items())
            + " per MTok")
        return 0
    book = pricebook.load(force=True)
    age = pricebook.age_days(book)
    if as_json:
        out(json.dumps({"fetched_at": book.get("fetched_at"), "age_days": age, "stale": pricebook.is_stale(book),
                        "user_file": book.get("user_file"), "entries": book["entries"]}, indent=2, default=str))
        return 0
    out(f"Price book fetched {book.get('fetched_at') or 'never'}"
        + (f" ({age:.0f} days ago" + (", STALE: run bp prices refresh)" if pricebook.is_stale(book) else ")")
           if age is not None else ""))
    out(f"  file: {book.get('user_file') or 'bundled snapshot only'}")
    out(f"  {'provider/model':<34} {'input':>8} {'5m write':>9} {'1h write':>9} {'read':>7} {'output':>8}  from")
    for e in sorted(book["entries"], key=lambda e: (e["provider"], e["model_id"], e.get("valid_from") or "")):
        r = e["rates"]
        out(f"  {e['provider'] + '/' + e['model_id']:<34} {r['input']:>8g} {r['cache_write_5m']:>9g} "
            f"{r['cache_write_1h']:>9g} {r['cache_read']:>7g} {r['output']:>8g}  "
            f"{(e.get('valid_from') or 'always')[:10]} ({e.get('source')})")
    return 0


def run_reconcile(console: str, model_col: Optional[str] = None, cost_col: Optional[str] = None,
                  date_col: Optional[str] = None, as_json: bool = False, out: Callable[[str], None] = print) -> int:
    from budgetpilot.core import canary, hookdb

    try:
        rows = canary.read_console_export(console, model_col, cost_col, date_col)
    except (OSError, ValueError) as exc:
        out(f"Cannot read {console}: {exc}")
        return 1
    conn = hookdb.open_ready()
    if conn is None:
        out("No BudgetPilot database yet.")
        return 1
    try:
        result = canary.reconcile_console(conn, rows)
    finally:
        conn.close()
    if as_json:
        out(json.dumps(result, indent=2))
        return 0
    out(f"Exported usage vs BudgetPilot ledger, {result['since'] or 'all dates'} to {result['until'] or 'now'}:")
    for m in result["per_model"]:
        diff = "n/a" if m["diff_pct"] is None else f"{m['diff_pct']:+.1f}%"
        out(f"  {m['model']:<32} export ${m['exported_usd']:.4f}  BudgetPilot ${m['budgetpilot_usd']:.4f}  {diff}")
    out(result["note"])
    return 0


def run_statusline(stdin: Any = None, out: Callable[[str], None] = print) -> int:
    """
    A Claude Code status line command. Records Claude Code's own session cost
    next to BudgetPilot's (the drift check) and prints one short line. Never
    fails: any problem prints a plain line and exits 0.
    """
    line = "BudgetPilot"
    try:
        raw = (stdin or sys.stdin).read()
        data = json.loads(raw) if raw.strip() else {}
        mode = state.get_mode()
        line = f"BudgetPilot {dict(enforce='ON', observe='OBSERVE', off='OFF').get(mode, mode)}"
        model = (data.get("model") or {}).get("display_name")
        used = (data.get("context_window") or {}).get("used_percentage")
        parts = [line] + ([model] if model else []) + ([f"context {used}%"] if used is not None else [])
        session_id = str(data.get("session_id") or "")
        cost = (data.get("cost") or {}).get("total_cost_usd")
        if mode != "off" and session_id and cost is not None:
            from budgetpilot.core import canary, hookdb

            conn = hookdb.open_ready()
            if conn is not None:
                try:
                    verdict = canary.record(conn, session_id, cost, "statusline", str(data.get("transcript_path") or ""))
                    if verdict.get("drift"):
                        parts.append("prices may be stale")
                finally:
                    conn.close()
        line = " · ".join(parts)
    except Exception as exc:
        try:
            from budgetpilot.core import hookdb

            hookdb.log_error("statusline", exc)
        except Exception:
            pass
    out(line)
    return 0

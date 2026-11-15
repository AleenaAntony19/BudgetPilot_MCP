"""
Local BudgetPilot dashboard: start, reuse and stop the server process.

The server (budgetpilot.dashboard.server) binds 127.0.0.1 only, reads the
database read-only, makes no external requests and exits after 30 idle
minutes. Standard library only.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time

from budgetpilot.core.state import get_bp_home

DEFAULT_PORT = 8765


def pidfile():
    return get_bp_home() / "dashboard.pid"


def _read_pidfile():
    try:
        data = json.loads(pidfile().read_text(encoding="utf-8"))
        return int(data["pid"]), int(data["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def running():
    """(pid, port) of a live dashboard, else None."""
    info = _read_pidfile()
    if info and _alive(info[0]) and _listening(info[1]):
        return info
    return None


def url_for(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def ensure_running(port=None, open_browser: bool = False, wait_seconds: float = 6.0) -> str:
    """Return the URL of the dashboard, starting it detached if it is not running."""
    info = running()
    if info is None:
        try:
            pidfile().unlink()
        except OSError:
            pass
        command = [sys.executable, "-m", "budgetpilot.dashboard.server", "--port", str(port or DEFAULT_PORT)]
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        deadline = time.time() + wait_seconds
        while time.time() < deadline and info is None:
            time.sleep(0.1)
            info = running()
        if info is None:
            raise RuntimeError("the dashboard process did not start")
    url = url_for(info[1])
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass
    return url


def stop() -> bool:
    info = _read_pidfile()
    if not info or not _alive(info[0]):
        return False
    try:
        os.kill(info[0], signal.SIGTERM)
    except OSError:
        return False
    try:
        pidfile().unlink()
    except OSError:
        pass
    return True

"""
Storage layer for the Claude Code hook engine.

Used by the hook process on every tool call, so it is standard library only
and never waits long: connections use a 50 ms lock timeout, log-style writes
that hit a locked database are queued in `outbox.jsonl`, and every public
function fails open (returns a neutral value) instead of raising.

Tables are created by migration 4 in budgetpilot.core.store. The heavier
store module is imported only when a table is missing.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from budgetpilot.core.state import get_bp_home

LOCK_TIMEOUT_S = 0.05
HEARTBEAT_SECONDS = 10
OUTBOX_MAX_BYTES = 5_000_000
ERROR_LOG_MAX_BYTES = 1_000_000


def db_path() -> Path:
    return get_bp_home() / "bp.db"


def connect(path: Optional[Path] = None, timeout: float = LOCK_TIMEOUT_S) -> sqlite3.Connection:
    """Short-lived autocommit connection that gives up quickly on a locked database."""
    conn = sqlite3.connect(str(path or db_path()), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)};")
    # WAL + NORMAL: a commit does not fsync, so a hook never waits on the disk.
    # A power cut can lose the last few telemetry rows, never corrupt the file.
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def ensure_schema() -> bool:
    """Run migrations (slow path, only when a hook table is missing)."""
    try:
        from budgetpilot.core.store import init_db

        init_db(db_path()).close()
        return True
    except Exception as exc:  # pragma: no cover - defensive
        log_error("ensure_schema", exc)
        return False


def open_ready() -> Optional[sqlite3.Connection]:
    """Connection with the hook tables present, or None (caller fails open)."""
    try:
        conn = connect()
        try:
            # `usage_cursors` is the last object the newest migration creates, so
            # its presence means the schema is current. One cheap query per hook.
            conn.execute("SELECT 1 FROM usage_cursors LIMIT 1;")
            return conn
        except sqlite3.OperationalError as exc:
            conn.close()
            if "no such table" not in str(exc).lower():
                return None
        if not ensure_schema():
            return None
        return connect()
    except Exception as exc:
        log_error("open_ready", exc)
        return None


# ------------------------------------------------------------------ error log
def log_error(where: str, exc: BaseException) -> None:
    """Append a truncated error line to hook_errors.log (rotated at 1 MB). Never raises."""
    try:
        path = get_bp_home() / "hook_errors.log"
        if path.exists() and path.stat().st_size > ERROR_LOG_MAX_BYTES:
            os.replace(path, path.with_name("hook_errors.log.1"))
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {where}: {type(exc).__name__}: {str(exc)[:500]}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ------------------------------------------------------------------ outbox
def _outbox_path() -> Path:
    return get_bp_home() / "outbox.jsonl"


def queue_write(sql: str, params: Iterable[Any]) -> None:
    try:
        path = _outbox_path()
        if path.exists() and path.stat().st_size > OUTBOX_MAX_BYTES:
            return
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"sql": sql, "params": list(params)}) + "\n")
    except Exception as exc:
        log_error("queue_write", exc)


def write(conn: Optional[sqlite3.Connection], sql: str, params: Iterable[Any] = ()) -> bool:
    """Log-style write. On a locked or missing database the row goes to the outbox."""
    params = list(params)
    if conn is not None:
        try:
            conn.execute(sql, params)
            return True
        except sqlite3.Error:
            pass
    queue_write(sql, params)
    return False


def flush_outbox(conn: Optional[sqlite3.Connection]) -> int:
    """Replay queued writes. Rows that still fail stay queued."""
    path = _outbox_path()
    if conn is None or not path.exists():
        return 0
    try:
        claimed = path.with_name(f"outbox.{os.getpid()}.flushing")
        os.replace(path, claimed)
    except OSError:
        return 0
    done, remaining = 0, []
    try:
        for line in claimed.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                conn.execute(item["sql"], item["params"])
                done += 1
            except sqlite3.OperationalError:
                remaining.append(line)
            except (ValueError, KeyError, sqlite3.Error):
                continue  # malformed or permanently invalid row: drop it
    finally:
        try:
            claimed.unlink()
        except OSError:
            pass
    if remaining:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(remaining) + "\n")
        except OSError:
            pass
    return done


# ------------------------------------------------------------------ identity
def repo_id_for(cwd: str) -> str:
    """sha1 of the git remote URL, else of the resolved repository root."""
    root = str(Path(cwd).resolve()) if cwd else ""
    key = root
    try:
        git_config = Path(root) / ".git" / "config"
        if git_config.is_file():
            for line in git_config.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("url ="):
                    key = line.split("=", 1)[1].strip()
                    break
    except OSError:
        pass
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ sessions
def touch_session(conn: sqlite3.Connection, session_id: str, cwd: str, transcript_path: str, host: str = "") -> None:
    now = time.time()
    write(
        conn,
        "INSERT INTO cc_sessions (session_id, first_seen, last_seen, cwd, repo_id, transcript_path, host) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET last_seen=excluded.last_seen, "
        "transcript_path=COALESCE(excluded.transcript_path, transcript_path)",
        (session_id, now, now, cwd, repo_id_for(cwd), transcript_path or None, host),
    )


def contract_sent(conn: sqlite3.Connection, session_id: str) -> bool:
    try:
        row = conn.execute("SELECT contract_sent FROM cc_sessions WHERE session_id=?", (session_id,)).fetchone()
        return bool(row and row[0])
    except sqlite3.Error:
        return True  # unknown: do not risk injecting twice


def mark_contract_sent(conn: sqlite3.Connection, session_id: str) -> None:
    write(conn, "UPDATE cc_sessions SET contract_sent=1 WHERE session_id=?", (session_id,))


# ------------------------------------------------------------------ tasks
TASK_COLUMNS = (
    "task_id, session_id, ts, repo_id, cwd, prompt_tokens_est, scope, scope_conf, mode, budget_slider, "
    "ctx_budget, out_budget, ctx_used_est, out_used, status, overhead_tokens_est, pending_recovery, state"
)


def _row_to_task(row: sqlite3.Row) -> Dict[str, Any]:
    task = dict(row)
    try:
        task["state"] = json.loads(task.get("state") or "{}")
    except ValueError:
        task["state"] = {}
    if not isinstance(task["state"], dict):
        task["state"] = {}
    return task


def create_task(conn: sqlite3.Connection, task: Dict[str, Any]) -> bool:
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO tasks_bp ({TASK_COLUMNS}) VALUES ({', '.join('?' * 18)})",
            (
                task["task_id"], task["session_id"], task.get("ts", time.time()), task.get("repo_id", ""),
                task.get("cwd", ""), task.get("prompt_tokens_est", 0), task.get("scope", 1),
                task.get("scope_conf", 0.0), task.get("mode", ""), task.get("budget_slider", ""),
                task.get("ctx_budget", 0), task.get("out_budget", 0), task.get("ctx_used_est", 0),
                task.get("out_used", 0), task.get("status", "active"), task.get("overhead_tokens_est", 0),
                task.get("pending_recovery"), json.dumps(task.get("state") or {}),
            ),
        )
        return True
    except sqlite3.Error as exc:
        log_error("create_task", exc)
        return False


def get_task(conn: sqlite3.Connection, task_id: str) -> Optional[Dict[str, Any]]:
    try:
        row = conn.execute(f"SELECT {TASK_COLUMNS} FROM tasks_bp WHERE task_id=?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None
    except sqlite3.Error:
        return None


def latest_task(conn: sqlite3.Connection, session_id: Optional[str] = None,
                pending_only: bool = False) -> Optional[Dict[str, Any]]:
    where, params = [], []
    if session_id:
        where.append("session_id=?")
        params.append(session_id)
    if pending_only:
        where.append("pending_recovery IS NOT NULL")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    try:
        row = conn.execute(
            f"SELECT {TASK_COLUMNS} FROM tasks_bp {clause} ORDER BY ts DESC LIMIT 1", params
        ).fetchone()
        return _row_to_task(row) if row else None
    except sqlite3.Error:
        return None


class TaskTxn:
    """
    Read-modify-write of one task row under BEGIN IMMEDIATE, so parallel hook
    processes (Claude Code runs tool calls concurrently) cannot lose updates.
    `task` is None when the row does not exist or the database is busy; the
    caller then fails open.
    """

    def __init__(self, conn: Optional[sqlite3.Connection], task_id: Optional[str]) -> None:
        self.conn = conn
        self.task_id = task_id
        self.task: Optional[Dict[str, Any]] = None
        self._open = False

    def __enter__(self) -> "TaskTxn":
        if self.conn is None or not self.task_id:
            return self
        try:
            self.conn.execute("BEGIN IMMEDIATE;")
            self._open = True
            self.task = get_task(self.conn, self.task_id)
        except sqlite3.Error:
            self.task = None
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self._open:
            return False
        try:
            if exc_type is None and self.task is not None:
                t = self.task
                self.conn.execute(
                    "UPDATE tasks_bp SET scope=?, ctx_budget=?, out_budget=?, ctx_used_est=?, out_used=?, "
                    "status=?, overhead_tokens_est=?, pending_recovery=?, state=? WHERE task_id=?",
                    (t["scope"], t["ctx_budget"], t["out_budget"], t["ctx_used_est"], t["out_used"],
                     t["status"], t["overhead_tokens_est"], t.get("pending_recovery"),
                     json.dumps(t["state"]), t["task_id"]),
                )
                self.conn.execute("COMMIT;")
            else:
                self.conn.execute("ROLLBACK;")
        except sqlite3.Error as db_exc:
            log_error("TaskTxn", db_exc)
            try:
                self.conn.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
        return False


# ------------------------------------------------------------------ events
def log_gate_event(conn: Optional[sqlite3.Connection], session_id: str, task_id: Optional[str], tool: str,
                   verdict: str, rule: str, reason: str, tokens_avoided_est: int, latency_ms: float) -> None:
    write(
        conn,
        "INSERT INTO gate_events (session_id, task_id, ts, tool, verdict, rule, reason, tokens_avoided_est, latency_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, task_id, time.time(), tool, verdict, rule, reason[:300], int(tokens_avoided_est),
         round(latency_ms, 2)),
    )


def log_tool_obs(conn: Optional[sqlite3.Connection], session_id: str, task_id: Optional[str], tool: str,
                 result_tokens_est: int, trimmed_tokens_est: int = 0, truncated: bool = False) -> None:
    write(
        conn,
        "INSERT INTO tool_obs (session_id, task_id, ts, tool, result_tokens_est, trimmed_tokens_est, truncated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, task_id, time.time(), tool, int(result_tokens_est), int(trimmed_tokens_est), int(truncated)),
    )


# ------------------------------------------------------------------ connections
def heartbeat(conn: Optional[sqlite3.Connection], name: str, kind: str, detail: Optional[Dict[str, Any]] = None,
              min_interval: float = HEARTBEAT_SECONDS) -> None:
    """Update the `connections` row at most once per `min_interval` seconds (stamp file check)."""
    try:
        stamp = get_bp_home() / f".hb_{name}"
        now = time.time()
        if min_interval and stamp.exists() and now - stamp.stat().st_mtime < min_interval:
            return
        stamp.touch()
        write(
            conn,
            "INSERT INTO connections (name, kind, last_seen, detail) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, last_seen=excluded.last_seen, detail=excluded.detail",
            (name, kind, now, json.dumps(detail or {})),
        )
    except Exception as exc:
        log_error("heartbeat", exc)


def git_untracked(repo: str, rel_path: str) -> bool:
    """True when git reports the path as untracked (`??`), i.e. created during the task."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--", rel_path], cwd=repo, capture_output=True, text=True,
            timeout=5, check=False,
        )
        return out.stdout.startswith("??")
    except (OSError, subprocess.SubprocessError):
        return False

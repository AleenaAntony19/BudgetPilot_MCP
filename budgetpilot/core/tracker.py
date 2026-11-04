"""
BudgetPilot Task State Lifecycle and Repository Snapshot Tracker.
Phase B: Task lifecycle state machine, read/touch set tracking, non-destructive git snapshots,
and SQLite session persistence.

Strict Rule: This module must NOT import ollama, mcp, or fastapi.
"""

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import logging
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from budgetpilot.core.store import get_connection, init_db, safe_execute_write

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    """
    Lifecycle phases for an agent task execution session.
    """
    IDLE = "idle"
    STARTING = "starting"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    DONE = "done"
    FAILED = "failed"


@dataclass
class TaskState:
    """
    Tracks runtime working state, file sets, and repository snapshot for a task session.
    """
    session_id: str
    status: TaskStatus = TaskStatus.IDLE
    read_set: Set[str] = field(default_factory=set)
    touched_set: Set[str] = field(default_factory=set)
    snapshot_ref: Optional[str] = None
    initial_head: Optional[str] = None
    repo_root: Optional[Path] = None
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def current_scope(self) -> int:
        return int(self.metadata.get("current_scope", self.metadata.get("scope", 1)))

    @current_scope.setter
    def current_scope(self, val: int) -> None:
        self.metadata["current_scope"] = int(val)
        self.metadata["scope"] = int(val)

    @property
    def read_files(self) -> Set[str]:
        return self.read_set

    @property
    def touched_files(self) -> Set[str]:
        return self.touched_set

    @property
    def task_id(self) -> str:
        return str(self.metadata.get("task_id", self.session_id))

    @property
    def denial_count(self) -> int:
        return int(self.metadata.get("deny_count", 0))

    @property
    def override_active(self) -> bool:
        return bool(self.metadata.get("override_active", False))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status.value,
            "read_set": sorted(list(self.read_set)),
            "touched_set": sorted(list(self.touched_set)),
            "snapshot_ref": self.snapshot_ref,
            "initial_head": self.initial_head,
            "repo_root": str(self.repo_root) if self.repo_root else None,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskState":
        status_val = data.get("status", "idle")
        status = status_val if isinstance(status_val, TaskStatus) else TaskStatus(str(status_val).lower())
        repo_root = Path(data["repo_root"]) if data.get("repo_root") else None

        return cls(
            session_id=data["session_id"],
            status=status,
            read_set=set(data.get("read_set", [])),
            touched_set=set(data.get("touched_set", [])),
            snapshot_ref=data.get("snapshot_ref"),
            initial_head=data.get("initial_head"),
            repo_root=repo_root,
            created_at=float(data.get("created_at", time.time())),
            metadata=data.get("metadata", {}),
        )


def create_git_snapshot(repo_root: Optional[Union[str, Path]]) -> Tuple[Optional[str], Optional[str]]:
    """
    Creates a non-destructive repository snapshot at task start using 'git stash create' and HEAD.
    Returns (snapshot_ref, initial_head).
    Gracefully returns (None, None) if outside a git repository, if git is missing, or on error.
    """
    if not repo_root:
        return None, None

    root = Path(repo_root).expanduser().resolve()
    if not (root / ".git").exists() and not (root.parent / ".git").exists():
        return None, None

    git_bin = shutil.which("git")
    if not git_bin:
        return None, None

    try:
        # Check if inside work tree and get initial HEAD
        proc_head = subprocess.run(
            [git_bin, "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc_head.returncode != 0:
            return None, None
        initial_head = proc_head.stdout.strip()

        # Non-destructively snapshot working tree modifications
        proc_stash = subprocess.run(
            [git_bin, "stash", "create"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=15,
        )
        stash_ref = proc_stash.stdout.strip()
        # If uncommitted changes exist, stash_ref is a commit SHA.
        # If working tree is clean, stash_ref is empty -> snapshot_ref is HEAD.
        snapshot_ref = stash_ref if stash_ref else initial_head

        return snapshot_ref, initial_head
    except Exception as exc:
        logger.debug("Git snapshot failed: %s", exc)
        return None, None


class TaskTracker:
    """
    Manages task lifecycle transitions, file read/touch sets, and SQLite persistence.
    """

    def __init__(
        self,
        session_id: str,
        repo_root: Optional[Union[str, Path]] = None,
        db_path: Optional[Union[str, Path]] = None,
        mode: str = "observe",
    ) -> None:
        self.db_path = db_path
        self.state = TaskState(
            session_id=session_id,
            status=TaskStatus.IDLE,
            repo_root=Path(repo_root).expanduser().resolve() if repo_root else None,
        )
        self.mode = mode
        self.state.metadata["mode"] = mode

    def _normalize_path(self, path: Union[str, Path]) -> str:
        if not path:
            return ""
        p = Path(path)
        if self.state.repo_root and p.is_absolute():
            try:
                return str(p.relative_to(self.state.repo_root))
            except ValueError:
                pass
        return str(p)

    def record_read(self, path: Union[str, Path]) -> None:
        """Add a path to the session's read_set."""
        norm = self._normalize_path(path)
        if norm:
            self.state.read_set.add(norm)

    def record_touch(self, path: Union[str, Path]) -> None:
        """Add a path to the session's touched_set."""
        norm = self._normalize_path(path)
        if norm:
            self.state.touched_set.add(norm)

    def record_tool_call(
        self,
        tool_name_or_event: Union[str, Any],
        arguments: Optional[Dict[str, Any]] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Inspect a tool call and automatically update read or touched sets, or scope."""
        if hasattr(tool_name_or_event, "payload") and isinstance(tool_name_or_event.payload, dict):
            name = str(tool_name_or_event.payload.get("tool_name") or getattr(tool_name_or_event, "event_type", "")).strip().lower()
            args_dict = tool_name_or_event.payload.get("arguments") or {}
        else:
            name = str(tool_name_or_event).strip().lower()
            args_dict = arguments or {}

        path = args_dict.get("path") or args_dict.get("file_path") or args_dict.get("file")
        if path:
            if name in ("read_file", "list_dir", "search"):
                self.record_read(path)
            elif name in ("edit_file", "write_file", "append_file"):
                self.record_touch(path)
        if name == "request_scope":
            curr = int(self.state.metadata.get("current_scope", self.state.metadata.get("scope", 1)))
            self.state.metadata["current_scope"] = min(3, curr + 1)
            self.state.metadata["scope"] = self.state.metadata["current_scope"]

    def start_task(
        self,
        repo_root: Optional[Union[str, Path]] = None,
        mode: Optional[str] = None,
    ) -> TaskState:
        """
        Transition task to EXECUTING, capture git snapshot, and persist to sessions/task_events.
        """
        if repo_root:
            self.state.repo_root = Path(repo_root).expanduser().resolve()
        if mode:
            self.mode = mode

        # 1. Capture git snapshot
        snapshot_ref, initial_head = create_git_snapshot(self.state.repo_root)
        self.state.snapshot_ref = snapshot_ref
        self.state.initial_head = initial_head
        self.state.status = TaskStatus.EXECUTING

        # 2. Persist session record
        session_meta = {
            "snapshot_ref": snapshot_ref,
            "initial_head": initial_head,
            "repo_root": str(self.state.repo_root) if self.state.repo_root else None,
        }

        upsert_session_sql = """
        INSERT INTO sessions (session_id, mode, status, metadata)
        VALUES (?, ?, 'executing', ?)
        ON CONFLICT(session_id) DO UPDATE SET
            status = 'executing',
            mode = excluded.mode,
            metadata = excluded.metadata,
            updated_at = CURRENT_TIMESTAMP;
        """
        safe_execute_write(
            conn_or_path=self.db_path,
            sql=upsert_session_sql,
            params=(self.state.session_id, self.mode, json.dumps(session_meta)),
        )

        # 3. Log task_start event
        task_event_sql = """
        INSERT INTO task_events (session_id, event_type, payload, metadata)
        VALUES (?, 'task_start', ?, ?);
        """
        start_payload = {
            "status": "executing",
            "snapshot_ref": snapshot_ref,
            "initial_head": initial_head,
        }
        safe_execute_write(
            conn_or_path=self.db_path,
            sql=task_event_sql,
            params=(
                self.state.session_id,
                json.dumps(start_payload),
                json.dumps({"mode": self.mode}),
            ),
        )

        return self.state

    def update_status(
        self,
        new_status: TaskStatus,
        payload: Optional[Dict[str, Any]] = None,
    ) -> TaskState:
        """
        Transition task status and record progress event.
        """
        self.state.status = new_status
        ev_payload = dict(payload or {})
        ev_payload["status"] = new_status.value
        ev_payload["read_count"] = len(self.state.read_set)
        ev_payload["touched_count"] = len(self.state.touched_set)

        # Update session
        update_session_sql = """
        UPDATE sessions
        SET status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE session_id = ?;
        """
        safe_execute_write(
            conn_or_path=self.db_path,
            sql=update_session_sql,
            params=(new_status.value, self.state.session_id),
        )

        # Log event
        task_event_sql = """
        INSERT INTO task_events (session_id, event_type, payload)
        VALUES (?, ?, ?);
        """
        safe_execute_write(
            conn_or_path=self.db_path,
            sql=task_event_sql,
            params=(
                self.state.session_id,
                f"task_{new_status.value}",
                json.dumps(ev_payload),
            ),
        )

        return self.state

    def end_task(
        self,
        success: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TaskState:
        """
        Conclude task session, setting status to DONE or FAILED.
        """
        final_status = TaskStatus.DONE if success else TaskStatus.FAILED
        self.state.status = final_status

        end_payload = dict(metadata or {})
        end_payload["success"] = success
        end_payload["final_status"] = final_status.value
        end_payload["read_set"] = sorted(list(self.state.read_set))
        end_payload["touched_set"] = sorted(list(self.state.touched_set))

        # Update session
        update_session_sql = """
        UPDATE sessions
        SET status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE session_id = ?;
        """
        safe_execute_write(
            conn_or_path=self.db_path,
            sql=update_session_sql,
            params=(final_status.value, self.state.session_id),
        )

        # Log task_end event
        task_event_sql = """
        INSERT INTO task_events (session_id, event_type, payload)
        VALUES (?, 'task_end', ?);
        """
        safe_execute_write(
            conn_or_path=self.db_path,
            sql=task_event_sql,
            params=(self.state.session_id, json.dumps(end_payload)),
        )

        return self.state

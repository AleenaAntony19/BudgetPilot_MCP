"""
BudgetPilot SQLite Event Store and Migration Subsystem.
Phase A: Database initialization, WAL pragmas, schema versioning, fail-safe logging, and legacy migration.

Strict Rule: This module must NOT import ollama, mcp, or fastapi.
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional, Tuple, Union

from budgetpilot.config import get_bp_home
from budgetpilot.core.safety import redact_data, redact_secrets

logger = logging.getLogger(__name__)

# Canonical list of core domain tables
CORE_TABLES = [
    "sessions",
    "model_calls",
    "tool_events",
    "task_events",
    "scope_predictions",
    "gate_decisions",
    "verifications",
    "recovery_decisions",
    "recovery_outcomes",
    "usage",
    "avoided_estimates",
    "lambda_table",
    "rollouts",
    "reports",
]

# Forward-only migrations registry: (version, description, ddl)
MIGRATIONS: List[Tuple[int, str, str]] = [
    (
        1,
        "Initial core schema: 14 domain tables and performance indices",
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            mode TEXT DEFAULT 'observe',
            budget_slider TEXT DEFAULT 'balanced',
            total_cost REAL DEFAULT 0.0,
            total_tokens INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            metadata TEXT
        );

        CREATE TABLE IF NOT EXISTS model_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            model TEXT NOT NULL,
            provider TEXT DEFAULT 'ollama',
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            estimated_cost REAL DEFAULT 0.0,
            total_time REAL DEFAULT 0.0,
            load_time REAL DEFAULT 0.0,
            prompt_time REAL DEFAULT 0.0,
            generation_time REAL DEFAULT 0.0,
            query_size TEXT,
            source TEXT DEFAULT 'live',
            legacy_id INTEGER,
            prompt TEXT,
            response TEXT,
            metadata TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS tool_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tool_name TEXT NOT NULL,
            arguments TEXT,
            call_id TEXT,
            result TEXT,
            duration REAL DEFAULT 0.0,
            is_error INTEGER DEFAULT 0,
            metadata TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            event_type TEXT NOT NULL,
            payload TEXT,
            metadata TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS scope_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scope INTEGER NOT NULL,
            confidence REAL DEFAULT 1.0,
            budget_tokens INTEGER DEFAULT 4000,
            max_read_files INTEGER DEFAULT 3,
            rationale TEXT,
            prompt_snippet TEXT,
            metadata TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS gate_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tool_name TEXT NOT NULL,
            verdict TEXT NOT NULL,
            reason TEXT,
            original_call TEXT,
            rewritten_call TEXT,
            metadata TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            test_command TEXT NOT NULL,
            passed INTEGER NOT NULL,
            exit_code INTEGER,
            stdout TEXT,
            stderr TEXT,
            duration REAL DEFAULT 0.0,
            failure_class TEXT,
            metadata TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS recovery_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            failure_class TEXT NOT NULL,
            action TEXT NOT NULL,
            target_tier TEXT DEFAULT 'cheap',
            cost_penalty_lambda REAL DEFAULT 0.0,
            confidence REAL DEFAULT 1.0,
            rationale TEXT,
            metadata TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS recovery_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recovery_decision_id INTEGER,
            session_id TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            action TEXT NOT NULL,
            succeeded INTEGER NOT NULL,
            tokens_spent INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            duration REAL DEFAULT 0.0,
            stderr TEXT,
            metadata TEXT,
            FOREIGN KEY(recovery_decision_id) REFERENCES recovery_decisions(id),
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tier TEXT,
            model TEXT,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            metadata TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS avoided_estimates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            mechanism TEXT,
            baseline_cost_usd REAL DEFAULT 0.0,
            actual_cost_usd REAL DEFAULT 0.0,
            saved_cost_usd REAL DEFAULT 0.0,
            metadata TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS lambda_table (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            budget_slider TEXT NOT NULL,
            action TEXT NOT NULL,
            lambda_penalty REAL NOT NULL,
            alpha_risk REAL DEFAULT 0.1,
            calibrated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT
        );

        CREATE TABLE IF NOT EXISTS rollouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            task_id TEXT,
            repo_name TEXT,
            initial_scope INTEGER,
            failure_induced TEXT,
            recovery_trajectory TEXT,
            final_verdict TEXT,
            total_cost_usd REAL DEFAULT 0.0,
            metadata TEXT
        );

        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            report_type TEXT DEFAULT 'session_summary',
            summary TEXT,
            total_cost_usd REAL DEFAULT 0.0,
            savings_usd REAL DEFAULT 0.0,
            metadata TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        );

        CREATE INDEX IF NOT EXISTS idx_model_calls_session ON model_calls(session_id);
        CREATE INDEX IF NOT EXISTS idx_model_calls_legacy ON model_calls(source, legacy_id);
        CREATE INDEX IF NOT EXISTS idx_tool_events_session ON tool_events(session_id);
        CREATE INDEX IF NOT EXISTS idx_gate_decisions_session ON gate_decisions(session_id);
        CREATE INDEX IF NOT EXISTS idx_verifications_session ON verifications(session_id);
        CREATE INDEX IF NOT EXISTS idx_recovery_decisions_session ON recovery_decisions(session_id);
        """
    )
]


def resolve_db_path(db_path: Optional[Union[Path, str]] = None) -> Path:
    """
    Resolve target SQLite database path. Defaults to ~/.budgetpilot/bp.db (or BP_HOME).
    """
    if db_path is None:
        return get_bp_home() / "bp.db"
    return Path(db_path).expanduser().resolve()


def get_connection(
    db_path: Optional[Union[Path, str]] = None,
    timeout: float = 10.0,
) -> sqlite3.Connection:
    """
    Create a SQLite connection with WAL mode and foreign keys enabled.
    """
    resolved_path = resolve_db_path(db_path)
    if str(resolved_path) != ":memory:":
        resolved_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(resolved_path), timeout=timeout)
    conn.row_factory = sqlite3.Row

    # Enforce SQLite WAL mode and foreign keys
    if str(resolved_path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def get_current_schema_version(conn: sqlite3.Connection) -> int:
    """
    Retrieve currently applied schema version from the schema_version tracking table.
    """
    conn.execute("""
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        description TEXT
    );
    """)
    cursor = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version;")
    row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def apply_migrations(conn: sqlite3.Connection) -> int:
    """
    Execute forward-only migrations idempotently.
    Returns the count of newly applied migrations.
    """
    current_version = get_current_schema_version(conn)
    applied_count = 0

    for version, description, ddl in MIGRATIONS:
        if version > current_version:
            logger.info("Applying migration %d: %s", version, description)
            with conn:
                conn.executescript(ddl)
                conn.execute(
                    "INSERT INTO schema_version (version, description) VALUES (?, ?);",
                    (version, description),
                )
            current_version = version
            applied_count += 1

    return applied_count


def init_db(db_path: Optional[Union[Path, str]] = None) -> sqlite3.Connection:
    """
    Initialize SQLite database at ~/.budgetpilot/bp.db (or custom db_path).
    Enables WAL mode, foreign keys, and executes all forward-only migrations.
    Returns an open connection.
    """
    conn = get_connection(db_path)
    apply_migrations(conn)
    return conn


def log_fallback_error(
    error: Exception,
    sql: str,
    params: Any = None,
    fallback_path: Optional[Union[Path, str]] = None,
) -> None:
    """
    Log failed database writes to fallback.jsonl in JSON Lines format.
    Never raises an exception, ensuring fail-open telemetry resilience.
    """
    try:
        if fallback_path is None:
            resolved_fallback = get_bp_home() / "fallback.jsonl"
        else:
            resolved_fallback = Path(fallback_path).expanduser().resolve()

        resolved_fallback.parent.mkdir(parents=True, exist_ok=True)
        sanitized_params = redact_data(params) if params is not None else None
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error_type": type(error).__name__,
            "error_message": redact_secrets(str(error)),
            "sql": redact_secrets(sql),
            "params": repr(sanitized_params) if sanitized_params is not None else None,
        }
        with open(resolved_fallback, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        logger.critical("Failed to write to fallback.jsonl: %s", exc)


def safe_execute_write(
    conn_or_path: Union[sqlite3.Connection, Path, str, None],
    sql: str,
    params: Union[tuple, list, dict] = (),
    fallback_path: Optional[Union[Path, str]] = None,
) -> bool:
    """
    Safely execute a write SQL query. If SQLite encounters any operational or locking errors,
    the event is written to fallback.jsonl without propagating an exception to the caller.
    Automatically redacts sensitive secrets (AWS keys, tokens, bearer strings) prior to write.

    Returns:
        True if the write succeeded, False if it failed and logged to fallback.jsonl.
    """
    sanitized_params = redact_data(params)

    close_when_done = False
    if isinstance(conn_or_path, sqlite3.Connection):
        conn = conn_or_path
    else:
        try:
            conn = get_connection(conn_or_path)
            close_when_done = True
        except Exception as e:
            log_fallback_error(e, sql, sanitized_params, fallback_path)
            return False

    try:
        with conn:
            conn.execute(sql, sanitized_params)
        return True
    except Exception as e:
        log_fallback_error(e, sql, sanitized_params, fallback_path)
        return False
    finally:
        if close_when_done:
            try:
                conn.close()
            except Exception:
                pass


def migrate_legacy_interactions(
    db_path: Optional[Union[Path, str]] = None,
    legacy_db_path: Optional[Union[Path, str]] = None,
) -> int:
    """
    Check if a legacy budgetpilot.db exists with an 'interactions' table, and idempotently
    copy its rows into 'model_calls' with source='legacy'.

    Returns:
        The number of newly migrated interaction rows.
    """
    if legacy_db_path is None:
        legacy_path = Path.cwd() / "budgetpilot.db"
    else:
        legacy_path = Path(legacy_db_path).expanduser().resolve()

    if not legacy_path.exists() or not legacy_path.is_file():
        return 0

    # Inspect legacy database
    try:
        legacy_conn = sqlite3.connect(str(legacy_path))
        legacy_cursor = legacy_conn.cursor()
        legacy_cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='interactions';"
        )
        if not legacy_cursor.fetchone():
            legacy_conn.close()
            return 0

        # Fetch legacy columns
        legacy_cursor.execute("PRAGMA table_info(interactions);")
        legacy_columns = [col[1] for col in legacy_cursor.fetchall()]

        # Query all interactions
        legacy_cursor.execute("SELECT * FROM interactions;")
        legacy_rows = legacy_cursor.fetchall()
        legacy_conn.close()
    except Exception as e:
        logger.warning("Could not read legacy interactions from %s: %s", legacy_path, e)
        return 0

    if not legacy_rows:
        return 0

    target_conn = init_db(db_path)
    migrated_count = 0

    try:
        # Determine existing migrated legacy IDs
        cursor = target_conn.cursor()
        cursor.execute("SELECT legacy_id FROM model_calls WHERE source = 'legacy' AND legacy_id IS NOT NULL;")
        existing_ids = {row[0] for row in cursor.fetchall()}

        for row in legacy_rows:
            row_dict = dict(zip(legacy_columns, row))
            legacy_id = row_dict.get("id")

            if legacy_id in existing_ids:
                continue

            insert_sql = """
            INSERT INTO model_calls (
                timestamp,
                model,
                provider,
                input_tokens,
                output_tokens,
                total_tokens,
                estimated_cost,
                total_time,
                load_time,
                prompt_time,
                generation_time,
                query_size,
                source,
                legacy_id,
                metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """

            metadata_json = json.dumps({"legacy_migration": True, "legacy_id": legacy_id})

            cursor.execute(
                insert_sql,
                (
                    row_dict.get("timestamp"),
                    row_dict.get("model", "qwen2.5:3b"),
                    "ollama",
                    int(row_dict.get("input_tokens") or 0),
                    int(row_dict.get("output_tokens") or 0),
                    int(row_dict.get("total_tokens") or 0),
                    float(row_dict.get("estimated_cost") or 0.0),
                    float(row_dict.get("total_time") or 0.0),
                    float(row_dict.get("load_time") or 0.0),
                    float(row_dict.get("prompt_time") or 0.0),
                    float(row_dict.get("generation_time") or 0.0),
                    row_dict.get("query_size"),
                    "legacy",
                    legacy_id,
                    metadata_json,
                ),
            )
            existing_ids.add(legacy_id)
            migrated_count += 1

        target_conn.commit()
    finally:
        target_conn.close()

    return migrated_count

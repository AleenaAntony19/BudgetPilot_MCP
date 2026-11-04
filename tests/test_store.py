"""
Unit tests for BudgetPilot SQLite store, migrations, fail-safe logging, and legacy migration (Phase A).
"""

import json
from pathlib import Path
import sqlite3
import tempfile
import pytest

from budgetpilot.core.store import (
    CORE_TABLES,
    apply_migrations,
    get_connection,
    get_current_schema_version,
    init_db,
    log_fallback_error,
    migrate_legacy_interactions,
    safe_execute_write,
)


def test_init_db_creates_all_tables_and_wal():
    """Verify init_db creates all core tables, enables WAL, and sets schema_version."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_bp.db"
        conn = init_db(db_path)

        # 1. Verify WAL mode
        cursor = conn.execute("PRAGMA journal_mode;")
        journal_mode = cursor.fetchone()[0]
        assert journal_mode.lower() == "wal"

        # 2. Verify foreign keys enabled
        cursor = conn.execute("PRAGMA foreign_keys;")
        assert cursor.fetchone()[0] == 1

        # 3. Verify schema_version table and version
        version = get_current_schema_version(conn)
        assert version >= 1

        # 4. Verify all 14 core tables exist
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
        )
        existing_tables = {row[0] for row in cursor.fetchall()}

        for table in CORE_TABLES:
            assert table in existing_tables, f"Core table '{table}' was not created."

        conn.close()


def test_migrations_idempotency():
    """Verify that migrations can be run multiple times idempotently without duplicating or failing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "idempotent.db"
        conn1 = init_db(db_path)
        v1 = get_current_schema_version(conn1)
        conn1.close()

        # Re-open and re-apply
        conn2 = get_connection(db_path)
        newly_applied = apply_migrations(conn2)
        assert newly_applied == 0
        v2 = get_current_schema_version(conn2)
        assert v1 == v2
        conn2.close()


def test_safe_execute_write_success():
    """Verify safe_execute_write succeeds under normal conditions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "safe_test.db"
        init_db(db_path)

        success = safe_execute_write(
            db_path,
            "INSERT INTO sessions (session_id, mode) VALUES (?, ?);",
            ("sess_101", "observe"),
        )
        assert success is True

        conn = get_connection(db_path)
        row = conn.execute("SELECT mode FROM sessions WHERE session_id = 'sess_101';").fetchone()
        assert row is not None
        assert row["mode"] == "observe"
        conn.close()


def test_fail_safe_logging_on_locked_or_broken_db():
    """
    Verify fail-open behavior: when database writes encounter an error,
    it must log to fallback.jsonl without raising an exception to the caller.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "locked.db"
        fallback_path = Path(tmpdir) / "fallback.jsonl"
        init_db(db_path)

        # 1. Lock the database exclusively with another connection
        locking_conn = sqlite3.connect(str(db_path), timeout=0.1)
        locking_conn.execute("BEGIN EXCLUSIVE TRANSACTION;")

        # 2. Attempt safe_execute_write on a locked DB with 0 timeout
        # Using a direct connection with tiny timeout to trigger operational error immediately
        target_conn = sqlite3.connect(str(db_path), timeout=0.01)

        result = safe_execute_write(
            conn_or_path=target_conn,
            sql="INSERT INTO sessions (session_id) VALUES ('sess_locked');",
            params=(),
            fallback_path=fallback_path,
        )

        # Must not raise, and must return False
        assert result is False

        # Release the lock
        locking_conn.rollback()
        locking_conn.close()
        target_conn.close()

        # 3. Verify fallback.jsonl was created and contains valid JSON error record
        assert fallback_path.exists(), "fallback.jsonl was not created on write failure"
        with open(fallback_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert "timestamp" in entry
        assert "error_type" in entry
        assert "locked" in entry["error_message"].lower() or "busy" in entry["error_message"].lower()
        assert "INSERT INTO sessions" in entry["sql"]


def test_migrate_legacy_interactions_idempotent():
    """
    Verify migrate_legacy_interactions copies rows from legacy budgetpilot.db
    into model_calls with source='legacy', and is completely idempotent.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        legacy_db = Path(tmpdir) / "budgetpilot.db"
        target_db = Path(tmpdir) / "bp.db"

        # Create mock legacy database with interactions table
        leg_conn = sqlite3.connect(str(legacy_db))
        leg_conn.execute("""
        CREATE TABLE interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            estimated_cost REAL,
            total_time REAL,
            load_time REAL,
            prompt_time REAL,
            generation_time REAL,
            query_size TEXT
        );
        """)
        leg_conn.execute("""
        INSERT INTO interactions (model, input_tokens, output_tokens, total_tokens, estimated_cost, total_time, load_time, prompt_time, generation_time, query_size)
        VALUES ('qwen2.5:3b', 40, 20, 60, 0.0001, 0.85, 0.40, 0.03, 0.42, 'small');
        """)
        leg_conn.execute("""
        INSERT INTO interactions (model, input_tokens, output_tokens, total_tokens, estimated_cost, total_time, load_time, prompt_time, generation_time, query_size)
        VALUES ('qwen2.5:3b', 120, 80, 200, 0.0003, 1.95, 0.41, 0.05, 1.49, 'medium');
        """)
        leg_conn.commit()
        leg_conn.close()

        # 1. First migration run
        migrated_1 = migrate_legacy_interactions(
            db_path=target_db,
            legacy_db_path=legacy_db,
        )
        assert migrated_1 == 2

        # Verify migrated records in target
        target_conn = get_connection(target_db)
        cursor = target_conn.execute("SELECT * FROM model_calls WHERE source = 'legacy';")
        rows = cursor.fetchall()
        assert len(rows) == 2
        assert rows[0]["source"] == "legacy"
        assert rows[0]["legacy_id"] is not None
        assert rows[0]["total_tokens"] in (60, 200)
        target_conn.close()

        # 2. Second migration run should be completely idempotent (0 newly migrated)
        migrated_2 = migrate_legacy_interactions(
            db_path=target_db,
            legacy_db_path=legacy_db,
        )
        assert migrated_2 == 0

        # Verify row count is still exactly 2
        target_conn = get_connection(target_db)
        cursor = target_conn.execute("SELECT COUNT(*) FROM model_calls WHERE source = 'legacy';")
        assert cursor.fetchone()[0] == 2
        target_conn.close()


def test_migrate_legacy_interactions_nonexistent_db():
    """Verify migrate_legacy_interactions gracefully handles non-existent legacy DB."""
    with tempfile.TemporaryDirectory() as tmpdir:
        target_db = Path(tmpdir) / "bp.db"
        non_existent = Path(tmpdir) / "does_not_exist.db"

        migrated = migrate_legacy_interactions(
            db_path=target_db,
            legacy_db_path=non_existent,
        )
        assert migrated == 0

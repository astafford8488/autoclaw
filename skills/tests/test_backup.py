"""Tests for anah-orchestrator backup and persistence hardening."""

import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "anah-orchestrator" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "anah-cerebellum" / "scripts"))
import backup
import cerebellum


class TestSchemaVersioning:
    """Schema migration system."""

    def test_fresh_db_gets_all_migrations(self, tmp_path):
        db_file = tmp_path / "test.db"
        with patch.object(cerebellum, "DB_FILE", db_file):
            db = cerebellum.get_db()
            version = cerebellum._get_schema_version(db)
        assert version == cerebellum.CURRENT_SCHEMA_VERSION
        db.close()

    def test_schema_version_table_exists(self, tmp_path):
        db_file = tmp_path / "test.db"
        with patch.object(cerebellum, "DB_FILE", db_file):
            db = cerebellum.get_db()
            tables = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
        assert "schema_version" in tables
        db.close()

    def test_all_tables_created(self, tmp_path):
        db_file = tmp_path / "test.db"
        with patch.object(cerebellum, "DB_FILE", db_file):
            db = cerebellum.get_db()
            tables = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
        for expected in ("health_logs", "task_queue", "generated_goals", "agent_actions", "schema_version"):
            assert expected in tables
        db.close()

    def test_v2_indexes_exist(self, tmp_path):
        db_file = tmp_path / "test.db"
        with patch.object(cerebellum, "DB_FILE", db_file):
            db = cerebellum.get_db()
            indexes = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()]
        assert "idx_task_queue_completed" in indexes
        assert "idx_generated_goals_status" in indexes
        assert "idx_health_logs_check" in indexes
        db.close()

    def test_v3_columns_exist(self, tmp_path):
        db_file = tmp_path / "test.db"
        with patch.object(cerebellum, "DB_FILE", db_file):
            db = cerebellum.get_db()
            cols = [r[1] for r in db.execute("PRAGMA table_info(generated_goals)").fetchall()]
        assert "topic_hash" in cols
        assert "discord_message_id" in cols
        assert "expires_at" in cols
        db.close()

    def test_idempotent_migrations(self, tmp_path):
        """Running get_db() twice shouldn't fail."""
        db_file = tmp_path / "test.db"
        with patch.object(cerebellum, "DB_FILE", db_file):
            db1 = cerebellum.get_db()
            db1.close()
            db2 = cerebellum.get_db()
            version = cerebellum._get_schema_version(db2)
        assert version == cerebellum.CURRENT_SCHEMA_VERSION
        db2.close()


class TestBackupCreate:
    """Database backup creation."""

    def test_create_backup(self, tmp_path):
        db_file = tmp_path / "anah.db"
        backups_dir = tmp_path / "backups"
        # Create a real DB
        db = sqlite3.connect(str(db_file))
        db.execute("CREATE TABLE test (id INTEGER)")
        db.execute("INSERT INTO test VALUES (42)")
        db.commit()
        db.close()

        with patch.object(backup, "DB_FILE", db_file), \
             patch.object(backup, "BACKUPS_DIR", backups_dir):
            result = backup.create_backup(tag="test")
        assert "path" in result
        assert result["size_bytes"] > 0
        assert Path(result["path"]).exists()

    def test_create_backup_no_db(self, tmp_path):
        with patch.object(backup, "DB_FILE", tmp_path / "nope.db"):
            result = backup.create_backup()
        assert "error" in result

    def test_backup_tag_in_filename(self, tmp_path):
        db_file = tmp_path / "anah.db"
        backups_dir = tmp_path / "backups"
        sqlite3.connect(str(db_file)).close()

        with patch.object(backup, "DB_FILE", db_file), \
             patch.object(backup, "BACKUPS_DIR", backups_dir):
            result = backup.create_backup(tag="pre-train")
        assert "pre-train" in result["path"]


class TestBackupRestore:
    """Database restore from backup."""

    def test_restore_from_backup(self, tmp_path):
        db_file = tmp_path / "anah.db"
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()

        # Create backup
        backup_file = backups_dir / "anah-test.db"
        db = sqlite3.connect(str(backup_file))
        db.execute("CREATE TABLE test (val TEXT)")
        db.execute("INSERT INTO test VALUES ('restored')")
        db.commit()
        db.close()

        # Corrupt the main DB
        db_file.write_text("corrupt data")

        with patch.object(backup, "DB_FILE", db_file), \
             patch.object(backup, "BACKUPS_DIR", backups_dir):
            result = backup.restore_backup(str(backup_file))
        assert "restored_from" in result
        # Verify restored data
        db = sqlite3.connect(str(db_file))
        val = db.execute("SELECT val FROM test").fetchone()[0]
        assert val == "restored"
        db.close()

    def test_restore_latest(self, tmp_path):
        db_file = tmp_path / "anah.db"
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()

        # Create two backups
        for name in ["anah-20260101-000000.db", "anah-20260102-000000.db"]:
            f = backups_dir / name
            db = sqlite3.connect(str(f))
            db.execute("CREATE TABLE test (id INTEGER)")
            db.commit()
            db.close()

        with patch.object(backup, "DB_FILE", db_file), \
             patch.object(backup, "BACKUPS_DIR", backups_dir):
            result = backup.restore_backup()
        assert "20260102" in result["restored_from"]

    def test_restore_no_backups(self, tmp_path):
        with patch.object(backup, "BACKUPS_DIR", tmp_path / "empty"):
            result = backup.restore_backup()
        assert "error" in result


class TestIntegrity:
    """Database integrity checking."""

    def test_healthy_db(self, tmp_path):
        db_file = tmp_path / "anah.db"
        sqlite3.connect(str(db_file)).close()
        with patch.object(backup, "DB_FILE", db_file):
            result = backup.check_integrity()
        assert result["status"] == "ok"

    def test_missing_db(self, tmp_path):
        with patch.object(backup, "DB_FILE", tmp_path / "nope.db"):
            result = backup.check_integrity()
        assert result["status"] == "missing"

    def test_corrupt_db(self, tmp_path):
        db_file = tmp_path / "anah.db"
        db_file.write_text("not a database")
        with patch.object(backup, "DB_FILE", db_file):
            result = backup.check_integrity()
        assert result["status"] in ("corrupt", "error")


class TestPruning:
    """Data pruning."""

    def test_prune_old_health_logs(self, tmp_path):
        db_file = tmp_path / "anah.db"
        db = sqlite3.connect(str(db_file))
        db.execute("CREATE TABLE health_logs (id INTEGER PRIMARY KEY, timestamp REAL, level INTEGER, check_name TEXT, passed INTEGER)")
        db.execute("CREATE TABLE task_queue (id INTEGER PRIMARY KEY, status TEXT, completed_at REAL)")
        db.execute("CREATE TABLE generated_goals (id INTEGER PRIMARY KEY, status TEXT, timestamp REAL)")
        old_ts = time.time() - 30 * 86400  # 30 days ago
        db.execute("INSERT INTO health_logs VALUES (1, ?, 1, 'test', 1)", (old_ts,))
        db.execute("INSERT INTO health_logs VALUES (2, ?, 1, 'test', 1)", (time.time(),))
        db.commit()
        db.close()

        with patch.object(backup, "DB_FILE", db_file):
            result = backup.prune_old_data()
        assert result["health_logs"] == 1  # Old one pruned

    def test_prune_keeps_recent(self, tmp_path):
        db_file = tmp_path / "anah.db"
        db = sqlite3.connect(str(db_file))
        db.execute("CREATE TABLE health_logs (id INTEGER PRIMARY KEY, timestamp REAL, level INTEGER, check_name TEXT, passed INTEGER)")
        db.execute("CREATE TABLE task_queue (id INTEGER PRIMARY KEY, status TEXT, completed_at REAL)")
        db.execute("CREATE TABLE generated_goals (id INTEGER PRIMARY KEY, status TEXT, timestamp REAL)")
        db.execute("INSERT INTO health_logs VALUES (1, ?, 1, 'test', 1)", (time.time(),))
        db.commit()
        db.close()

        with patch.object(backup, "DB_FILE", db_file):
            result = backup.prune_old_data()
        assert result["health_logs"] == 0


class TestRotation:
    """Backup rotation."""

    def test_rotate_deletes_excess(self, tmp_path):
        """With many backups across many weeks, rotation deletes excess."""
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        import os
        base_time = time.time()
        # Create 20 daily backups (spans multiple weeks)
        for i in range(20):
            f = backups_dir / f"anah-20260{4 if i < 10 else 3}{i % 10 + 10:02d}-120000.db"
            f.write_text("x")
            mtime = base_time - (i * 86400)
            os.utime(str(f), (mtime, mtime))

        with patch.object(backup, "BACKUPS_DIR", backups_dir):
            result = backup.rotate_backups()
        # Should keep at most 7 daily + 4 weekly = 11
        assert result["kept"] <= 11
        assert result["deleted"] >= 9
        # At least some were deleted
        assert result["deleted"] > 0


class TestMaintenance:
    """Full maintenance cycle."""

    def test_maintenance_runs(self, tmp_path):
        db_file = tmp_path / "anah.db"
        backups_dir = tmp_path / "backups"
        traj_dir = tmp_path / "trajectories"
        traj_dir.mkdir()

        # Create a simple DB
        db = sqlite3.connect(str(db_file))
        db.execute("CREATE TABLE health_logs (id INTEGER PRIMARY KEY, timestamp REAL, level INTEGER, check_name TEXT, passed INTEGER)")
        db.execute("CREATE TABLE task_queue (id INTEGER PRIMARY KEY, status TEXT, completed_at REAL)")
        db.execute("CREATE TABLE generated_goals (id INTEGER PRIMARY KEY, status TEXT, timestamp REAL)")
        db.commit()
        db.close()

        with patch.object(backup, "DB_FILE", db_file), \
             patch.object(backup, "BACKUPS_DIR", backups_dir), \
             patch.object(backup, "ANAH_DIR", tmp_path):
            result = backup.run_maintenance()
        assert "backup" in result
        assert "integrity" in result
        assert "pruned" in result
        assert result["integrity"]["status"] == "ok"

"""Tests for anah-cerebellum — L4 performance monitoring and pattern detection."""

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "anah-cerebellum" / "scripts"))
import cerebellum


class TestDatabaseSetup:
    """Schema creation and DB access."""

    def test_ensure_schema_creates_tables(self, anah_db):
        """ensure_schema should create all required tables."""
        # anah_db fixture already creates tables, verify they exist
        tables = [r[0] for r in anah_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "health_logs" in tables
        assert "task_queue" in tables
        assert "generated_goals" in tables

    def test_db_connection(self, anah_dir):
        """get_db should return a working connection."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cerebellum, "DB_FILE", anah_dir / "anah.db")
            db = cerebellum.get_db()
            assert db is not None
            db.close()


class TestIngestion:
    """Brainstem result ingestion into health_logs."""

    def test_ingest_brainstem_results(self, anah_db, sample_brainstem_results):
        """Ingesting brainstem results should populate health_logs."""
        cerebellum.ingest_brainstem_results(anah_db, sample_brainstem_results["results"])
        count = anah_db.execute("SELECT COUNT(*) FROM health_logs").fetchone()[0]
        assert count == 8  # 4 L1 + 3 L2 + 1 L3

    def test_ingest_preserves_check_levels(self, anah_db, sample_brainstem_results):
        """Ingested records should preserve the check level."""
        cerebellum.ingest_brainstem_results(anah_db, sample_brainstem_results["results"])
        levels = [r[0] for r in anah_db.execute("SELECT DISTINCT level FROM health_logs").fetchall()]
        assert set(levels) == {1, 2, 3}

    def test_ingest_handles_details_json(self, anah_db, sample_brainstem_results):
        """Details field should be stored as JSON string."""
        cerebellum.ingest_brainstem_results(anah_db, sample_brainstem_results["results"])
        row = anah_db.execute(
            "SELECT details FROM health_logs WHERE check_name='compute_resources'").fetchone()
        assert row is not None
        details = json.loads(row[0])
        assert "cpu" in details

    def test_ingest_null_details(self, anah_db, sample_brainstem_results):
        """Checks with no details should store NULL."""
        cerebellum.ingest_brainstem_results(anah_db, sample_brainstem_results["results"])
        row = anah_db.execute(
            "SELECT details FROM health_logs WHERE check_name='network_connectivity'").fetchone()
        assert row[0] is None


class TestPatternDetection:
    """Pattern analysis from health data."""

    def test_detect_idle_opportunity(self, anah_db):
        """Idle system with empty queue should detect opportunity."""
        state = {"levels": {"1": {"status": "healthy"}}, "gating": {"l1_healthy": True}}
        patterns = cerebellum.analyze(anah_db, state)
        categories = [p.category for p in patterns]
        assert "idle_opportunity" in categories

    def test_detect_recurring_failures(self, anah_db):
        """Multiple failures of the same check should be flagged."""
        now = time.time()
        for i in range(5):
            anah_db.execute(
                "INSERT INTO health_logs (timestamp, level, check_name, passed, duration_ms, message) "
                "VALUES (?, 1, 'network_connectivity', 0, 100, 'Failed')",
                (now - i * 60,))
        anah_db.commit()
        state = {"levels": {}, "gating": {"l1_healthy": True}}
        patterns = cerebellum.analyze(anah_db, state)
        recurring = [p for p in patterns if p.category == "recurring_failure"]
        assert len(recurring) > 0

    def test_build_context_structure(self, anah_db):
        """Context should include health_score, queue stats, and gating."""
        state = {"levels": {"1": {"status": "healthy"}}, "gating": {"l1_healthy": True}}
        ctx = cerebellum.build_context(anah_db, state)
        assert "health_score" in ctx
        assert "queue" in ctx
        assert "gating" in ctx
        assert isinstance(ctx["queue"], dict)

    def test_context_queue_counts(self, anah_db):
        """Queue stats should reflect actual DB state."""
        now = time.time()
        anah_db.execute(
            "INSERT INTO task_queue (created_at, title, status) VALUES (?, 'test', 'queued')",
            (now,))
        anah_db.execute(
            "INSERT INTO task_queue (created_at, title, status) VALUES (?, 'test2', 'completed')",
            (now,))
        anah_db.commit()
        state = {"levels": {}, "gating": {"l1_healthy": True}}
        ctx = cerebellum.build_context(anah_db, state)
        assert ctx["queue"]["queued"] == 1
        assert ctx["queue"]["completed"] == 1


class TestSecurity:
    """Security boundary tests."""

    def test_no_sql_injection_in_ingest(self, anah_db):
        """Malicious check names should not cause SQL injection."""
        evil_results = [{
            "name": "'; DROP TABLE health_logs; --",
            "level": 1, "passed": True, "duration_ms": 0,
            "message": "test", "details": None,
        }]
        cerebellum.ingest_brainstem_results(anah_db, evil_results)
        # Table should still exist
        count = anah_db.execute("SELECT COUNT(*) FROM health_logs").fetchone()[0]
        assert count == 1

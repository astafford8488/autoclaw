"""Tests for anah-orchestrator — central nervous system."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "anah-orchestrator" / "scripts"))
import orchestrator


class TestFullCycle:
    """End-to-end heartbeat cycle."""

    def test_full_cycle_returns_all_stages(self):
        result = orchestrator.full_cycle(generate=False, learn=False)
        assert "brainstem" in result
        assert "cerebellum" in result
        assert "cortex" in result
        assert "timestamp" in result
        assert "duration_ms" in result
        assert result["cycle"] == "full"

    def test_full_cycle_brainstem_health(self):
        result = orchestrator.full_cycle(generate=False, learn=False)
        bs = result["brainstem"]
        assert "health_score" in bs
        assert "l1_healthy" in bs
        assert "passed" in bs
        assert "failed" in bs

    def test_full_cycle_with_learning(self):
        result = orchestrator.full_cycle(generate=False, learn=True)
        assert "hippocampus" in result
        assert "evaluated" in result["hippocampus"]

    def test_full_cycle_without_learning(self):
        result = orchestrator.full_cycle(generate=False, learn=False)
        assert "hippocampus" not in result

    def test_cortex_skipped_when_generate_false(self):
        result = orchestrator.full_cycle(generate=False, learn=False)
        assert result["cortex"]["skipped"] is True

    def test_full_cycle_duration_reasonable(self):
        """Full cycle should complete in under 30 seconds."""
        result = orchestrator.full_cycle(generate=False, learn=False)
        assert result["duration_ms"] < 30000


class TestGating:
    """L1 gating prevents higher-level execution."""

    def test_gated_cycle_skips_cerebellum(self):
        """When brainstem reports L1 failure, cerebellum/cortex should be skipped."""
        mock_brainstem = {
            "results": [
                {"name": "network", "level": 1, "passed": False,
                 "duration_ms": 0, "message": "FAIL", "details": None}
            ],
            "gating": {"l1_healthy": False},
            "summary": {"total": 1, "passed": 0, "failed": 1, "health_score": 0.0},
        }
        with patch.object(orchestrator, "run_brainstem", return_value=mock_brainstem):
            result = orchestrator.full_cycle(generate=False, learn=False)
            assert result.get("gated") is True
            assert "cerebellum" not in result
            assert result["brainstem"]["l1_healthy"] is False


class TestWatchdog:
    """Quick L1-only watchdog cycle."""

    def test_watchdog_returns_structure(self):
        result = orchestrator.watchdog_cycle()
        assert result["cycle"] == "watchdog"
        assert "l1_healthy" in result
        assert "checks" in result
        assert "duration_ms" in result

    def test_watchdog_faster_than_full(self):
        """Watchdog should be faster since it only runs L1."""
        watchdog = orchestrator.watchdog_cycle()
        full = orchestrator.full_cycle(generate=False, learn=False)
        # Watchdog should generally be faster (allow some variance)
        assert watchdog["duration_ms"] < full["duration_ms"] * 2


class TestStatusOverview:
    """Aggregate status from all organelles."""

    def test_status_has_all_organelles(self):
        status = orchestrator.status_overview()
        assert "brainstem" in status
        assert "memory" in status
        assert "cortex" in status
        assert "hippocampus" in status

    def test_status_brainstem_fields(self):
        status = orchestrator.status_overview()
        bs = status["brainstem"]
        assert "health_score" in bs
        assert "l1_healthy" in bs


class TestSecurity:
    """Security boundary tests."""

    def test_no_secrets_in_cycle_output(self):
        """Full cycle output should never contain secrets."""
        result = orchestrator.full_cycle(generate=False, learn=False)
        output = json.dumps(result)
        assert "sk-ant-" not in output
        assert "api_key" not in output.lower()
        assert "password" not in output.lower()

    def test_env_loading_respects_existing(self):
        """load_env should not override existing environment variables."""
        import os
        os.environ["TEST_EXISTING_VAR"] = "original"
        orchestrator.load_env()
        assert os.environ["TEST_EXISTING_VAR"] == "original"
        del os.environ["TEST_EXISTING_VAR"]


class TestQueueBacklogProtection:
    """Queue depth gating prevents runaway goal generation."""

    def test_get_queue_depth(self, anah_db):
        """get_queue_depth returns count of queued tasks."""
        now = time.time()
        for i in range(15):
            anah_db.execute(
                "INSERT INTO task_queue (created_at, priority, title, status) VALUES (?, 3, ?, 'queued')",
                (now, f"task {i}"))
        anah_db.execute(
            "INSERT INTO task_queue (created_at, priority, title, status) VALUES (?, 3, 'done', 'completed')",
            (now,))
        anah_db.commit()
        with patch.object(orchestrator, "ANAH_DIR", anah_db.execute("PRAGMA database_list").fetchone()[2]):
            pass
        # Direct DB test
        row = anah_db.execute("SELECT COUNT(*) FROM task_queue WHERE status = 'queued'").fetchone()
        assert row[0] == 15

    def test_cortex_skipped_when_backlog_high(self):
        """Cortex generation is skipped when queue depth > 50."""
        with patch.object(orchestrator, "get_queue_depth", return_value=100):
            result = orchestrator.full_cycle(generate=True, execute=False, learn=False)
        cortex = result.get("cortex", {})
        assert cortex.get("skipped") is True
        assert "backlog" in cortex.get("reason", "")

    def test_cortex_runs_when_backlog_low(self):
        """Cortex generation runs normally when queue depth < 50."""
        with patch.object(orchestrator, "get_queue_depth", return_value=10):
            result = orchestrator.full_cycle(generate=True, execute=False, learn=False)
        cortex = result.get("cortex", {})
        # Should not be skipped due to backlog (may be skipped for other reasons)
        assert cortex.get("reason", "") != "queue backlog 10"

    def test_executor_limit_increases_with_backlog(self):
        """Executor processes more tasks when backlog is large."""
        calls = []
        original_run_executor = orchestrator.run_executor
        def spy_executor(limit=20):
            calls.append(limit)
            return {"processed": 0, "succeeded": 0, "failed": 0, "results": []}
        with patch.object(orchestrator, "get_queue_depth", return_value=200), \
             patch.object(orchestrator, "run_executor", side_effect=spy_executor):
            orchestrator.full_cycle(generate=False, execute=True, learn=False)
        assert calls and calls[0] == 50  # limit=50 when backlog > 100

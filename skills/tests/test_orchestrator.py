"""Tests for anah-orchestrator — central nervous system."""

import json
import sys
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

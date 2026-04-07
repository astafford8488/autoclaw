"""Tests for anah-orchestrator cron bridge."""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "anah-orchestrator" / "scripts"))
import cron_bridge


@pytest.fixture
def mock_orchestrator():
    """Mock the orchestrator module that cron_bridge imports locally."""
    mock = MagicMock()
    with patch.dict(sys.modules, {"orchestrator": mock}):
        yield mock


class TestHeartbeatBridge:
    """Heartbeat command via cron bridge."""

    def test_heartbeat_returns_structured_summary(self, mock_orchestrator):
        mock_orchestrator.full_cycle.return_value = {
            "timestamp": 1000, "cycle": "full",
            "brainstem": {"health_score": 0.95, "passed": 10, "failed": 0, "l1_healthy": True, "levels_checked": 14},
            "cerebellum": {"patterns": 2, "health_score": 0.95, "queue": {}},
            "cortex": {"enacted": [], "count": 3},
            "executor": {"processed": 2, "succeeded": 2, "failed": 0, "results": []},
            "hippocampus": {"evaluated": 1, "extracted": 1, "results": []},
            "trajectories": {"exported": 5},
            "duration_ms": 1200,
        }
        result = cron_bridge.heartbeat()
        assert result["ok"] is True
        assert result["command"] == "heartbeat"
        assert result["health_score"] == 0.95
        assert result["goals_generated"] == 3
        assert result["tasks_succeeded"] == 2
        assert result["skills_extracted"] == 1
        assert result["trajectories_exported"] == 5
        assert "summary" in result

    def test_heartbeat_gated(self, mock_orchestrator):
        mock_orchestrator.full_cycle.return_value = {
            "brainstem": {"health_score": 0.3}, "gated": True, "duration_ms": 50,
        }
        result = cron_bridge.heartbeat()
        assert result["ok"] is True
        assert result["gated"] is True
        assert "GATED" in result["summary"]

    def test_heartbeat_error(self, mock_orchestrator):
        mock_orchestrator.full_cycle.side_effect = RuntimeError("brainstem exploded")
        result = cron_bridge.heartbeat()
        assert result["ok"] is False
        assert "brainstem exploded" in result["error"]


class TestWatchdogBridge:
    """Watchdog command via cron bridge."""

    def test_watchdog_healthy(self, mock_orchestrator):
        mock_orchestrator.watchdog_cycle.return_value = {
            "l1_healthy": True, "checks": {"total": 5, "passed": 5}, "duration_ms": 30,
        }
        result = cron_bridge.watchdog()
        assert result["ok"] is True
        assert result["healthy"] is True
        assert "L1 healthy" in result["summary"]

    def test_watchdog_unhealthy(self, mock_orchestrator):
        mock_orchestrator.watchdog_cycle.return_value = {
            "l1_healthy": False, "checks": {"total": 5, "passed": 3}, "duration_ms": 30,
        }
        result = cron_bridge.watchdog()
        assert result["ok"] is True
        assert result["healthy"] is False
        assert "UNHEALTHY" in result["summary"]


class TestStatusBridge:
    """Status command via cron bridge."""

    def test_status_returns_overview(self, mock_orchestrator):
        mock_orchestrator.status_overview.return_value = {
            "brainstem": {"health_score": 0.9}, "memory": {"used": 100},
        }
        result = cron_bridge.status()
        assert result["ok"] is True
        assert result["command"] == "status"
        assert result["brainstem"]["health_score"] == 0.9


class TestTrainBridge:
    """Train command via cron bridge."""

    def test_train_success(self):
        mock_result = {"sft": {"after_dedup": 10}, "modelfile": {"training_examples": 10}}
        with patch.dict(sys.modules, {"trainer": MagicMock()}):
            sys.modules["trainer"].run_training.return_value = mock_result
            result = cron_bridge.train()
        assert result["ok"] is True
        assert result["command"] == "train"

    def test_train_error(self):
        with patch.dict(sys.modules, {"trainer": MagicMock()}):
            sys.modules["trainer"].run_training.side_effect = RuntimeError("no data")
            result = cron_bridge.train()
        assert result["ok"] is False
        assert "no data" in result["error"]


class TestExportBridge:
    """Export command via cron bridge."""

    def test_export_success(self, mock_orchestrator):
        mock_orchestrator.run_trajectory_export.return_value = {"exported": 3, "path": "/tmp/trajs.json"}
        result = cron_bridge.export_trajectories()
        assert result["ok"] is True
        assert result["exported"] == 3


class TestFormatSummary:
    """Summary formatting."""

    def test_format_includes_all_stats(self):
        result = {
            "brainstem": {"health_score": 0.85},
            "cortex": {"count": 2},
            "executor": {"processed": 3, "succeeded": 2},
            "hippocampus": {"extracted": 1},
            "trajectories": {"exported": 4},
            "duration_ms": 500,
        }
        summary = cron_bridge.format_heartbeat_summary(result)
        assert "85%" in summary["summary"]
        assert "Goals: 2" in summary["summary"]
        assert "Tasks: 2/3" in summary["summary"]
        assert "Skills: +1" in summary["summary"]
        assert "Trajectories: 4" in summary["summary"]

    def test_format_omits_zero_stats(self):
        result = {
            "brainstem": {"health_score": 1.0},
            "cortex": {"count": 0},
            "executor": {"processed": 0, "succeeded": 0},
            "hippocampus": {"extracted": 0},
            "trajectories": {"exported": 0},
            "duration_ms": 100,
        }
        summary = cron_bridge.format_heartbeat_summary(result)
        assert "Goals" not in summary["summary"]
        assert "Tasks" not in summary["summary"]


class TestCommandRegistry:
    """CLI command routing."""

    def test_all_commands_registered(self):
        assert set(cron_bridge.COMMANDS.keys()) == {"heartbeat", "watchdog", "status", "train", "export"}

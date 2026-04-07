"""Tests for anah-trainer — trajectory training pipeline."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "anah-trainer" / "scripts"))
import trainer


@pytest.fixture
def training_env(tmp_path):
    """Set up a temporary training environment with sample trajectories."""
    traj_dir = tmp_path / "trajectories"
    traj_dir.mkdir()
    training_dir = tmp_path / "training"
    training_dir.mkdir()

    # Create sample trajectories
    now = time.time()
    trajectories = [
        {
            "conversations": [
                {"from": "system", "value": "You are an autonomous task executor."},
                {"from": "human", "value": "Task: Optimize Queue Management"},
                {"from": "gpt", "value": json.dumps({"status": "completed", "summary": "Optimized queue"})},
            ],
            "metadata": {
                "task_id": 1, "title": "Optimize Queue Management",
                "source": "l5_generated", "outcome": "completed",
                "priority": 6, "duration_ms": 2500, "timestamp": now,
            },
        },
        {
            "conversations": [
                {"from": "system", "value": "You are an autonomous task executor."},
                {"from": "human", "value": "Task: Enhance Logging"},
                {"from": "gpt", "value": json.dumps({"status": "completed", "summary": "Enhanced logging"})},
            ],
            "metadata": {
                "task_id": 2, "title": "Enhance Logging",
                "source": "l5_generated", "outcome": "completed",
                "priority": 8, "duration_ms": 3000, "timestamp": now,
            },
        },
        {
            "conversations": [
                {"from": "system", "value": "You are an autonomous task executor."},
                {"from": "human", "value": "Task: Failing task"},
                {"from": "gpt", "value": json.dumps({"error": "something broke"})},
            ],
            "metadata": {
                "task_id": 3, "title": "Failing task",
                "source": "l5_generated", "outcome": "failed",
                "priority": 5, "duration_ms": 1000, "timestamp": now,
            },
        },
        {
            "conversations": [
                {"from": "system", "value": "Test"},
                {"from": "human", "value": "echo: test"},
                {"from": "gpt", "value": "echoed"},
            ],
            "metadata": {
                "task_id": 4, "title": "echo: trivial",
                "source": "manual", "outcome": "completed",
                "priority": 1, "duration_ms": 5, "timestamp": now,
            },
        },
    ]
    (traj_dir / "test_trajectories.json").write_text(json.dumps(trajectories))
    return tmp_path, traj_dir, training_dir


class TestTrajectoryLoading:
    """Loading trajectories from disk."""

    def test_load_all(self, training_env):
        tmp_path, traj_dir, _ = training_env
        with patch.object(trainer, "TRAJECTORIES_DIR", traj_dir):
            trajs = trainer.load_all_trajectories()
        assert len(trajs) == 4

    def test_load_empty_dir(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with patch.object(trainer, "TRAJECTORIES_DIR", empty):
            trajs = trainer.load_all_trajectories()
        assert trajs == []

    def test_load_nonexistent_dir(self, tmp_path):
        with patch.object(trainer, "TRAJECTORIES_DIR", tmp_path / "nope"):
            trajs = trainer.load_all_trajectories()
        assert trajs == []


class TestQualityFilter:
    """Quality filtering of trajectories."""

    def test_filters_success_only(self, training_env):
        _, traj_dir, _ = training_env
        with patch.object(trainer, "TRAJECTORIES_DIR", traj_dir):
            all_t = trainer.load_all_trajectories()
            filtered = trainer.filter_quality(all_t, success_only=True)
        # Should exclude the failed one and the trivial echo (too short duration)
        assert all(t["metadata"]["outcome"] == "completed" for t in filtered)

    def test_filters_trivial_duration(self, training_env):
        _, traj_dir, _ = training_env
        with patch.object(trainer, "TRAJECTORIES_DIR", traj_dir):
            all_t = trainer.load_all_trajectories()
            filtered = trainer.filter_quality(all_t, success_only=True)
        # echo task has 5ms duration, below MIN_DURATION_MS
        titles = [t["metadata"]["title"] for t in filtered]
        assert "echo: trivial" not in titles

    def test_filters_echo_handler(self, training_env):
        _, traj_dir, _ = training_env
        with patch.object(trainer, "TRAJECTORIES_DIR", traj_dir):
            all_t = trainer.load_all_trajectories()
            filtered = trainer.filter_quality(all_t, success_only=True)
        titles = [t["metadata"]["title"] for t in filtered]
        assert all(not t.startswith("echo:") for t in titles)

    def test_includes_failures_when_requested(self, training_env):
        _, traj_dir, _ = training_env
        with patch.object(trainer, "TRAJECTORIES_DIR", traj_dir):
            all_t = trainer.load_all_trajectories()
            filtered = trainer.filter_quality(all_t, success_only=False)
        outcomes = [t["metadata"]["outcome"] for t in filtered]
        assert "failed" in outcomes


class TestDeduplication:
    """Trajectory deduplication."""

    def test_removes_duplicates(self):
        trajs = [
            {"metadata": {"task_id": 1}, "conversations": []},
            {"metadata": {"task_id": 1}, "conversations": []},
            {"metadata": {"task_id": 2}, "conversations": []},
        ]
        unique = trainer.deduplicate(trajs)
        assert len(unique) == 2

    def test_keeps_unique(self):
        trajs = [
            {"metadata": {"task_id": 1}, "conversations": []},
            {"metadata": {"task_id": 2}, "conversations": []},
        ]
        unique = trainer.deduplicate(trajs)
        assert len(unique) == 2


class TestSFTDataset:
    """SFT dataset preparation."""

    def test_prepare_sft(self, training_env):
        tmp_path, traj_dir, training_dir = training_env
        with patch.object(trainer, "TRAJECTORIES_DIR", traj_dir), \
             patch.object(trainer, "TRAINING_DIR", training_dir):
            result = trainer.prepare_sft_dataset()
        assert result["total_trajectories"] == 4
        assert result["after_quality_filter"] >= 1
        assert Path(result["sft_path"]).exists()
        assert Path(result["sharegpt_path"]).exists()

    def test_sft_jsonl_format(self, training_env):
        tmp_path, traj_dir, training_dir = training_env
        with patch.object(trainer, "TRAJECTORIES_DIR", traj_dir), \
             patch.object(trainer, "TRAINING_DIR", training_dir):
            result = trainer.prepare_sft_dataset()
        sft_path = Path(result["sft_path"])
        for line in sft_path.read_text().strip().splitlines():
            entry = json.loads(line)
            assert "messages" in entry
            assert all("role" in m and "content" in m for m in entry["messages"])


class TestDPODataset:
    """DPO dataset preparation."""

    def test_prepare_dpo(self, training_env):
        tmp_path, traj_dir, training_dir = training_env
        with patch.object(trainer, "TRAJECTORIES_DIR", traj_dir), \
             patch.object(trainer, "TRAINING_DIR", training_dir):
            result = trainer.prepare_dpo_dataset()
        assert result["successes"] >= 1
        assert "dpo_path" in result


class TestModelfile:
    """Ollama Modelfile generation."""

    def test_create_modelfile(self, training_env):
        _, traj_dir, training_dir = training_env
        with patch.object(trainer, "TRAJECTORIES_DIR", traj_dir), \
             patch.object(trainer, "TRAINING_DIR", training_dir):
            # First prepare SFT
            trainer.prepare_sft_dataset()
            result = trainer.create_modelfile()
        assert "modelfile_path" in result
        assert result["training_examples"] >= 1
        content = Path(result["modelfile_path"]).read_text()
        assert "FROM" in content
        assert "SYSTEM" in content

    def test_create_modelfile_no_data(self, training_env):
        _, _, training_dir = training_env
        with patch.object(trainer, "TRAINING_DIR", training_dir):
            result = trainer.create_modelfile()
        assert "error" in result


class TestDatasetStats:
    """Statistics reporting."""

    def test_stats_structure(self, training_env):
        _, traj_dir, training_dir = training_env
        with patch.object(trainer, "TRAJECTORIES_DIR", traj_dir), \
             patch.object(trainer, "TRAINING_DIR", training_dir):
            stats = trainer.dataset_stats()
        assert stats["total_trajectories"] == 4
        assert "by_outcome" in stats
        assert "by_handler" in stats
        assert "duration_stats" in stats
        assert "quality_eligible" in stats

    def test_stats_counts_outcomes(self, training_env):
        _, traj_dir, training_dir = training_env
        with patch.object(trainer, "TRAJECTORIES_DIR", traj_dir), \
             patch.object(trainer, "TRAINING_DIR", training_dir):
            stats = trainer.dataset_stats()
        assert stats["by_outcome"]["completed"] == 3
        assert stats["by_outcome"]["failed"] == 1


class TestSecurity:
    """Security tests."""

    def test_no_secrets_in_sft_output(self, training_env):
        _, traj_dir, training_dir = training_env
        with patch.object(trainer, "TRAJECTORIES_DIR", traj_dir), \
             patch.object(trainer, "TRAINING_DIR", training_dir):
            result = trainer.prepare_sft_dataset()
        output = json.dumps(result)
        assert "sk-ant-" not in output
        assert "api_key" not in output.lower()

    def test_no_secrets_in_stats(self, training_env):
        _, traj_dir, training_dir = training_env
        with patch.object(trainer, "TRAJECTORIES_DIR", traj_dir), \
             patch.object(trainer, "TRAINING_DIR", training_dir):
            stats = trainer.dataset_stats()
        output = json.dumps(stats)
        assert "sk-ant-" not in output
        assert "password" not in output.lower()

"""Tests for anah-memory — bounded store and trajectory export."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "anah-memory" / "scripts"))
import memory


class TestBoundedMemory:
    """Character-limited memory with enforcement."""

    def test_write_within_limit(self, anah_dir):
        with patch.object(memory, "MEMORY_FILE", anah_dir / "MEMORY.md"):
            result = memory.write_memory("Hello world")
            assert "written" in result
            assert result["written"] == 11
            assert result["remaining"] == memory.MEMORY_LIMIT - 11

    def test_write_exceeds_limit_rejected(self, anah_dir):
        with patch.object(memory, "MEMORY_FILE", anah_dir / "MEMORY.md"):
            content = "x" * (memory.MEMORY_LIMIT + 1)
            result = memory.write_memory(content)
            assert "error" in result
            assert "exceeds limit" in result["error"]

    def test_write_at_exact_limit(self, anah_dir):
        with patch.object(memory, "MEMORY_FILE", anah_dir / "MEMORY.md"):
            content = "x" * memory.MEMORY_LIMIT
            result = memory.write_memory(content)
            assert "written" in result
            assert result["remaining"] == 0

    def test_read_missing_file_returns_empty(self, anah_dir):
        with patch.object(memory, "MEMORY_FILE", anah_dir / "MEMORY.md"):
            assert memory.read_memory() == ""

    def test_read_after_write(self, anah_dir):
        with patch.object(memory, "MEMORY_FILE", anah_dir / "MEMORY.md"):
            memory.write_memory("test content")
            assert memory.read_memory() == "test content"


class TestBoundedProfile:
    """System profile with separate limits."""

    def test_profile_write_within_limit(self, anah_dir):
        with patch.object(memory, "PROFILE_FILE", anah_dir / "SYSTEM_PROFILE.md"):
            result = memory.write_profile("System info")
            assert "written" in result

    def test_profile_exceeds_limit_rejected(self, anah_dir):
        with patch.object(memory, "PROFILE_FILE", anah_dir / "SYSTEM_PROFILE.md"):
            content = "x" * (memory.PROFILE_LIMIT + 1)
            result = memory.write_profile(content)
            assert "error" in result

    def test_profile_limit_different_from_memory(self):
        """Profile and memory should have different limits."""
        assert memory.PROFILE_LIMIT != memory.MEMORY_LIMIT
        assert memory.PROFILE_LIMIT == 1375
        assert memory.MEMORY_LIMIT == 2200


class TestMemoryStatus:
    """Status reporting."""

    def test_status_structure(self, anah_dir):
        with patch.object(memory, "MEMORY_FILE", anah_dir / "MEMORY.md"), \
             patch.object(memory, "PROFILE_FILE", anah_dir / "SYSTEM_PROFILE.md"), \
             patch.object(memory, "TRAJECTORIES_DIR", anah_dir / "trajectories"):
            status = memory.memory_status()
            assert "memory" in status
            assert "profile" in status
            assert "trajectories" in status
            assert "utilization" in status["memory"]
            assert "remaining" in status["memory"]

    def test_status_utilization_format(self, anah_dir):
        with patch.object(memory, "MEMORY_FILE", anah_dir / "MEMORY.md"), \
             patch.object(memory, "PROFILE_FILE", anah_dir / "SYSTEM_PROFILE.md"), \
             patch.object(memory, "TRAJECTORIES_DIR", anah_dir / "trajectories"):
            memory.write_memory("x" * 1100)  # 50% of 2200
            status = memory.memory_status()
            assert status["memory"]["utilization"] == "50.0%"


class TestConsolidation:
    """Memory consolidation when over limit."""

    def test_consolidate_within_limits_noop(self, anah_dir):
        with patch.object(memory, "MEMORY_FILE", anah_dir / "MEMORY.md"):
            memory.write_memory("short content")
            result = memory.consolidate_memory()
            assert result["status"] == "within_limits"

    def test_consolidate_truncates_to_fit(self, anah_dir):
        """Force consolidation by writing directly past the limit."""
        mem_file = anah_dir / "MEMORY.md"
        with patch.object(memory, "MEMORY_FILE", mem_file):
            # Write directly to bypass the limit check
            mem_file.write_text("x\n" * 2000)
            result = memory.consolidate_memory()
            assert result["status"] == "consolidated"
            assert result["after"] <= memory.MEMORY_LIMIT
            # Content should still be readable
            content = memory.read_memory()
            assert len(content) <= memory.MEMORY_LIMIT


class TestTrajectoryExport:
    """ShareGPT format trajectory export."""

    def test_export_task_trajectory(self, anah_dir, anah_db):
        """Completed task should export as ShareGPT conversations."""
        now = time.time()
        anah_db.execute(
            "INSERT INTO task_queue (id, created_at, started_at, completed_at, priority, source, title, description, status, result) "
            "VALUES (1, ?, ?, ?, 5, 'manual', 'test task', 'do something', 'completed', ?)",
            (now - 10, now - 5, now, json.dumps({"output": "done"})))
        anah_db.commit()

        with patch.object(memory, "DB_FILE", anah_dir / "anah.db"):
            traj = memory.export_task_trajectory(anah_db, 1)
            assert traj is not None
            assert "conversations" in traj
            assert "metadata" in traj

            # ShareGPT format: system, human, gpt
            convos = traj["conversations"]
            assert len(convos) == 3
            assert convos[0]["from"] == "system"
            assert convos[1]["from"] == "human"
            assert convos[2]["from"] == "gpt"

    def test_export_nonexistent_task(self, anah_db):
        traj = memory.export_task_trajectory(anah_db, 9999)
        assert traj is None

    def test_trajectory_metadata_fields(self, anah_dir, anah_db):
        now = time.time()
        anah_db.execute(
            "INSERT INTO task_queue (id, created_at, started_at, completed_at, priority, source, title, status, result) "
            "VALUES (1, ?, ?, ?, 5, 'l5_generated', 'test', 'completed', '{}')",
            (now - 10, now - 5, now))
        anah_db.commit()

        traj = memory.export_task_trajectory(anah_db, 1)
        meta = traj["metadata"]
        assert meta["task_id"] == 1
        assert meta["source"] == "l5_generated"
        assert meta["outcome"] == "completed"
        assert meta["duration_ms"] is not None
        assert meta["duration_ms"] > 0


class TestTrajectoryStorage:
    """Saving and pruning trajectory files."""

    def test_save_trajectories(self, anah_dir):
        with patch.object(memory, "TRAJECTORIES_DIR", anah_dir / "trajectories"):
            trajs = [{"conversations": [], "metadata": {"task_id": 1}}]
            path = memory.save_trajectories(trajs, "test.json")
            assert Path(path).exists()
            loaded = json.loads(Path(path).read_text())
            assert len(loaded) == 1

    def test_prune_trajectories(self, anah_dir):
        traj_dir = anah_dir / "trajectories"
        traj_dir.mkdir(exist_ok=True)
        with patch.object(memory, "TRAJECTORIES_DIR", traj_dir):
            # Create 5 files
            for i in range(5):
                (traj_dir / f"traj_{i}.json").write_text("[]")
            result = memory.prune_trajectories(keep=2)
            assert result["pruned"] == 3
            assert result["remaining"] == 2


class TestSecurity:
    """Security tests."""

    def test_memory_content_not_executable(self, anah_dir):
        """Memory should store text, not executable content."""
        with patch.object(memory, "MEMORY_FILE", anah_dir / "MEMORY.md"):
            evil = "$(rm -rf /)\n`cat /etc/passwd`\n<script>alert(1)</script>"
            memory.write_memory(evil)
            content = memory.read_memory()
            # Content stored as-is (plain text), not interpreted
            assert content == evil

    def test_trajectory_path_no_traversal(self, anah_dir):
        """Trajectory filenames should not allow path traversal."""
        with patch.object(memory, "TRAJECTORIES_DIR", anah_dir / "trajectories"):
            path = memory.save_trajectories([], "../../etc/evil.json")
            # Path should be sanitized to just the filename under trajectories/
            assert "evil.json" in path
            assert ".." not in path
            resolved = Path(path).resolve()
            traj_dir = (anah_dir / "trajectories").resolve()
            assert str(resolved).startswith(str(traj_dir))

"""Tests for anah-dashboard — API endpoints, approve/dismiss, SSE, and server."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "anah-dashboard" / "scripts"))
import dashboard


class TestHealthAPI:
    """Health status endpoint."""

    def test_health_returns_structure(self):
        result = dashboard.api_health()
        assert "health_score" in result
        assert "l1_healthy" in result
        assert "levels" in result

    def test_health_missing_state_file(self, anah_dir):
        with patch.object(dashboard, "ANAH_DIR", anah_dir):
            result = dashboard.api_health()
            assert "status" in result or "health_score" in result


class TestQueueAPI:
    """Task queue endpoint."""

    def test_queue_returns_counts_and_tasks(self, anah_db, anah_dir):
        now = time.time()
        anah_db.execute("INSERT INTO task_queue (created_at, title, status) VALUES (?, 'test', 'queued')", (now,))
        anah_db.execute("INSERT INTO task_queue (created_at, title, status) VALUES (?, 'done', 'completed')", (now,))
        anah_db.commit()

        with patch.object(dashboard, "DB_FILE", anah_dir / "anah.db"):
            with patch.object(dashboard, "get_db", return_value=anah_db):
                result = dashboard.api_queue()

        assert result["counts"]["queued"] == 1
        assert result["counts"]["completed"] == 1
        assert result["total"] == 2

    def test_queue_empty(self, anah_db):
        with patch.object(dashboard, "get_db", return_value=anah_db):
            result = dashboard.api_queue()
        assert result["total"] == 0


class TestGoalsAPI:
    """Goals endpoint."""

    def test_goals_returns_stats(self, anah_db):
        now = time.time()
        anah_db.execute(
            "INSERT INTO generated_goals (timestamp, title, priority, status, source, reasoning) "
            "VALUES (?, 'test goal', 5, 'enacted', 'llm', 'because')", (now,))
        anah_db.commit()
        with patch.object(dashboard, "get_db", return_value=anah_db):
            result = dashboard.api_goals()
        assert result["stats"]["enacted"] == 1
        assert len(result["goals"]) == 1

    def test_goals_includes_pending_approval(self, anah_db):
        now = time.time()
        anah_db.execute(
            "INSERT INTO generated_goals (timestamp, title, priority, status, source) "
            "VALUES (?, 'pending', 5, 'pending_approval', 'llm')", (now,))
        anah_db.commit()
        with patch.object(dashboard, "get_db", return_value=anah_db):
            result = dashboard.api_goals()
        assert result["stats"]["pending_approval"] == 1


class TestGoalHistory:
    """Paginated goal history endpoint."""

    def test_history_pagination(self, anah_db):
        now = time.time()
        for i in range(15):
            anah_db.execute(
                "INSERT INTO generated_goals (timestamp, title, priority, status, source) "
                "VALUES (?, ?, 5, 'enacted', 'llm')", (now - i, f"goal-{i}"))
        anah_db.commit()
        with patch.object(dashboard, "get_db", return_value=anah_db):
            result = dashboard.api_goals_history({"page": ["1"], "per_page": ["10"]})
        assert len(result["goals"]) == 10
        assert result["total"] == 15
        assert result["pages"] == 2

    def test_history_page_2(self, anah_db):
        now = time.time()
        for i in range(15):
            anah_db.execute(
                "INSERT INTO generated_goals (timestamp, title, priority, status, source) "
                "VALUES (?, ?, 5, 'enacted', 'llm')", (now - i, f"goal-{i}"))
        anah_db.commit()
        with patch.object(dashboard, "get_db", return_value=anah_db):
            result = dashboard.api_goals_history({"page": ["2"], "per_page": ["10"]})
        assert len(result["goals"]) == 5
        assert result["page"] == 2

    def test_history_status_filter(self, anah_db):
        now = time.time()
        anah_db.execute("INSERT INTO generated_goals (timestamp, title, priority, status, source) VALUES (?, 'a', 5, 'enacted', 'llm')", (now,))
        anah_db.execute("INSERT INTO generated_goals (timestamp, title, priority, status, source) VALUES (?, 'b', 5, 'dismissed', 'llm')", (now,))
        anah_db.commit()
        with patch.object(dashboard, "get_db", return_value=anah_db):
            result = dashboard.api_goals_history({"status": ["enacted"]})
        assert result["total"] == 1
        assert result["goals"][0]["title"] == "a"


class TestGoalApprove:
    """Goal approve/dismiss endpoints."""

    def test_approve_goal(self, anah_dir, anah_db):
        now = time.time()
        anah_db.execute(
            "INSERT INTO generated_goals (timestamp, title, priority, status, source, description, reasoning) "
            "VALUES (?, 'approve me', 5, 'pending_approval', 'llm', 'desc', 'reason')", (now,))
        anah_db.commit()
        goal_id = anah_db.execute("SELECT id FROM generated_goals").fetchone()[0]

        with patch.object(dashboard, "get_db", return_value=anah_db), \
             patch.object(dashboard, "DB_FILE", anah_dir / "anah.db"):
            result = dashboard.api_goals_approve({"goal_id": goal_id})
        assert "task_id" in result

    def test_dismiss_goal(self, anah_dir, anah_db):
        now = time.time()
        anah_db.execute(
            "INSERT INTO generated_goals (timestamp, title, priority, status, source) "
            "VALUES (?, 'dismiss me', 3, 'pending_approval', 'llm')", (now,))
        anah_db.commit()
        goal_id = anah_db.execute("SELECT id FROM generated_goals").fetchone()[0]

        with patch.object(dashboard, "get_db", return_value=anah_db), \
             patch.object(dashboard, "DB_FILE", anah_dir / "anah.db"):
            result = dashboard.api_goals_dismiss({"goal_id": goal_id})
        assert result["dismissed"] == goal_id

    def test_approve_no_id(self):
        result = dashboard.api_goals_approve({})
        assert "error" in result

    def test_dismiss_no_id(self):
        result = dashboard.api_goals_dismiss({})
        assert "error" in result


class TestHealthHistory:
    """Health history sparkline data."""

    def test_health_history(self, anah_db):
        now = time.time()
        for i in range(5):
            anah_db.execute(
                "INSERT INTO health_logs (timestamp, level, check_name, passed) VALUES (?, 1, 'test', ?)",
                (now - i * 60, 1 if i % 2 == 0 else 0))
        anah_db.commit()
        with patch.object(dashboard, "get_db", return_value=anah_db):
            result = dashboard.api_health_history({"limit": ["50"]})
        assert "points" in result
        assert len(result["points"]) > 0
        # Points should be oldest-first
        if len(result["points"]) > 1:
            assert result["points"][0]["ts"] <= result["points"][-1]["ts"]

    def test_health_history_empty(self, anah_db):
        with patch.object(dashboard, "get_db", return_value=anah_db):
            result = dashboard.api_health_history({})
        assert result["points"] == []


class TestTrainingAPI:
    """Training status endpoint."""

    def test_training_no_data(self, anah_dir):
        with patch.object(dashboard, "ANAH_DIR", anah_dir):
            result = dashboard.api_training()
        assert result["datasets"] == {}
        assert result["last_train"] is None

    def test_training_with_data(self, anah_dir):
        training_dir = anah_dir / "training"
        training_dir.mkdir()
        (training_dir / "sft_dataset.jsonl").write_text('{"a":1}\n{"b":2}\n')
        (training_dir / "Modelfile").write_text('FROM base\n')
        with patch.object(dashboard, "ANAH_DIR", anah_dir):
            result = dashboard.api_training()
        assert result["datasets"]["sft_dataset.jsonl"]["entries"] == 2
        assert "Modelfile" in result["datasets"]


class TestSSE:
    """SSE broadcast."""

    def test_broadcast_to_empty(self):
        # Should not raise
        dashboard.sse_broadcast("test", {"msg": "hi"})

    def test_broadcast_queues_message(self):
        import queue
        q = queue.Queue(maxsize=50)
        with dashboard._sse_lock:
            dashboard._sse_clients.append(q)
        try:
            dashboard.sse_broadcast("test_event", {"data": 1})
            msg = q.get_nowait()
            assert "event: test_event" in msg
            assert '"data": 1' in msg
        finally:
            with dashboard._sse_lock:
                dashboard._sse_clients.remove(q)


class TestSkillsAPI:
    def test_skills_returns_list(self, anah_dir):
        skill_dir = anah_dir / "skills" / "learned-test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text('---\nname: learned-test\ndescription: "A test skill"\n---\n# Test')
        with patch.object(dashboard, "ANAH_DIR", anah_dir):
            result = dashboard.api_skills()
        assert result["count"] == 1

    def test_skills_empty(self, anah_dir):
        with patch.object(dashboard, "ANAH_DIR", anah_dir):
            result = dashboard.api_skills()
        assert result["count"] == 0


class TestOverviewAPI:
    def test_overview_has_all_sections(self):
        result = dashboard.api_overview()
        for key in ("health", "queue", "goals", "skills", "memory", "timestamp"):
            assert key in result


class TestSecurity:
    def test_no_secrets_in_api_output(self):
        result = dashboard.api_overview()
        output = json.dumps(result, default=str)
        assert "sk-ant-" not in output
        assert "password" not in output.lower()

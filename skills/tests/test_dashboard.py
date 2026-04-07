"""Tests for anah-dashboard — API endpoints and server."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

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
            # No state.json exists — should return graceful response
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
        assert len(result["tasks"]) == 2

    def test_queue_empty(self, anah_db):
        with patch.object(dashboard, "get_db", return_value=anah_db):
            result = dashboard.api_queue()
        assert result["total"] == 0
        assert result["tasks"] == []


class TestGoalsAPI:
    """Goals endpoint."""

    def test_goals_returns_stats_and_list(self, anah_db):
        now = time.time()
        anah_db.execute(
            "INSERT INTO generated_goals (timestamp, title, priority, status, source, reasoning) "
            "VALUES (?, 'test goal', 5, 'enacted', 'llm', 'because')", (now,))
        anah_db.commit()

        with patch.object(dashboard, "get_db", return_value=anah_db):
            result = dashboard.api_goals()

        assert result["stats"]["total"] == 1
        assert result["stats"]["enacted"] == 1
        assert len(result["goals"]) == 1
        assert result["goals"][0]["title"] == "test goal"


class TestSkillsAPI:
    """Learned skills endpoint."""

    def test_skills_returns_list(self, anah_dir):
        # Create a mock learned skill
        skill_dir = anah_dir / "skills" / "learned-test"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text('---\nname: learned-test\ndescription: "A test skill"\n---\n# Test')

        with patch.object(dashboard, "ANAH_DIR", anah_dir):
            result = dashboard.api_skills()

        assert result["count"] == 1
        assert result["skills"][0]["name"] == "learned-test"

    def test_skills_empty(self, anah_dir):
        with patch.object(dashboard, "ANAH_DIR", anah_dir):
            result = dashboard.api_skills()
        assert result["count"] == 0


class TestMemoryAPI:
    """Memory utilization endpoint."""

    def test_memory_returns_limits(self):
        result = dashboard.api_memory()
        assert "memory" in result
        assert "profile" in result
        assert result["memory"]["limit"] == 2200
        assert result["profile"]["limit"] == 1375


class TestOverviewAPI:
    """Combined overview endpoint."""

    def test_overview_has_all_sections(self):
        result = dashboard.api_overview()
        assert "health" in result
        assert "queue" in result
        assert "goals" in result
        assert "skills" in result
        assert "memory" in result
        assert "timestamp" in result


class TestSecurity:
    """Security tests."""

    def test_no_secrets_in_api_output(self):
        result = dashboard.api_overview()
        output = json.dumps(result, default=str)
        assert "sk-ant-" not in output
        assert "api_key" not in output.lower()
        assert "password" not in output.lower()

"""Tests for anah-cortex — L5 goal generation."""

import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "anah-cortex" / "scripts"))
import cortex


class TestDeduplication:
    """Goal deduplication logic."""

    def test_titles_similar_exact_match(self):
        assert cortex.titles_similar("health report", "health report") is True

    def test_titles_similar_partial_overlap(self):
        assert cortex.titles_similar(
            "investigate network latency",
            "investigate network performance"
        ) is True  # 2/3 overlap >= 0.5

    def test_titles_dissimilar(self):
        assert cortex.titles_similar(
            "investigate network latency",
            "optimize database queries"
        ) is False

    def test_titles_similar_ignores_stop_words(self):
        """Stop words like 'a', 'the', 'and' should be filtered."""
        assert cortex.titles_similar(
            "the health report for the system",
            "a health report and system check"
        ) is True

    def test_dedup_filters_similar(self):
        goals = [
            cortex.Goal("health check report", 3, "desc", "reason", "pattern"),
            cortex.Goal("optimize database", 5, "desc", "reason", "pattern"),
        ]
        recent = ["health check assessment"]
        filtered = cortex.dedup_goals(goals, recent)
        # "health check report" is similar to "health check assessment"
        assert len(filtered) == 1
        assert filtered[0].title == "optimize database"

    def test_dedup_keeps_novel_goals(self):
        goals = [
            cortex.Goal("investigate memory leak", 7, "desc", "reason", "llm"),
        ]
        recent = ["health report", "disk cleanup"]
        filtered = cortex.dedup_goals(goals, recent)
        assert len(filtered) == 1

    def test_dedup_empty_recent(self):
        goals = [cortex.Goal("any goal", 3, "desc", "reason", "pattern")]
        filtered = cortex.dedup_goals(goals, [])
        assert len(filtered) == 1


class TestFallbackGeneration:
    """Pattern-based goal generation (no LLM needed)."""

    def test_fallback_generates_from_patterns(self):
        patterns = [{
            "title": "High failure rate",
            "category": "recurring_failure",
            "severity": "warning",
            "description": "Network checks failing repeatedly",
            "suggested_action": "Investigate network connectivity issues",
        }]
        goals = cortex.generate_goals_fallback({}, patterns)
        assert len(goals) == 1
        assert goals[0].title == "Investigate network connectivity issues"
        assert goals[0].source == "pattern"

    def test_fallback_priority_from_severity(self):
        patterns = [{
            "title": "Critical issue",
            "category": "failure",
            "severity": "critical",
            "description": "desc",
            "suggested_action": "Fix it",
        }]
        goals = cortex.generate_goals_fallback({}, patterns)
        assert goals[0].priority == 7  # critical = 7

    def test_fallback_idle_system_generates_health_report(self):
        context = {"health_score": 95, "queue": {"queued": 0, "running": 0}}
        goals = cortex.generate_goals_fallback(context, [])
        assert len(goals) == 1
        assert "health report" in goals[0].title.lower()

    def test_fallback_busy_system_no_idle_goal(self):
        context = {"health_score": 95, "queue": {"queued": 3, "running": 1}}
        goals = cortex.generate_goals_fallback(context, [])
        assert len(goals) == 0  # Not idle, no idle goal


class TestGoalLifecycle:
    """Goal logging, enqueuing, and dismissal."""

    def test_log_goal(self, anah_dir, anah_db):
        with patch.object(cortex, "DB_FILE", anah_dir / "anah.db"):
            goal = cortex.Goal("test goal", 5, "desc", "reason", "pattern")
            goal_id = cortex.log_goal(anah_db, goal)
            assert goal_id > 0

            row = anah_db.execute("SELECT * FROM generated_goals WHERE id = ?", (goal_id,)).fetchone()
            assert dict(row)["title"] == "test goal"
            assert dict(row)["status"] == "proposed"

    def test_enqueue_task(self, anah_db):
        goal = cortex.Goal("test goal", 5, "desc", "reason", "pattern")
        goal_id = cortex.log_goal(anah_db, goal)
        task_id = cortex.enqueue_task(anah_db, goal, goal_id)
        assert task_id > 0

        task = anah_db.execute("SELECT * FROM task_queue WHERE id = ?", (task_id,)).fetchone()
        assert dict(task)["status"] == "queued"
        assert dict(task)["source"] == "l5_generated"

        # Goal should be updated to enacted
        goal_row = anah_db.execute("SELECT * FROM generated_goals WHERE id = ?", (goal_id,)).fetchone()
        assert dict(goal_row)["status"] == "enacted"

    def test_dismiss_goal(self, anah_db):
        goal = cortex.Goal("test goal", 5, "desc", "reason", "pattern")
        goal_id = cortex.log_goal(anah_db, goal)
        cortex.dismiss_goal(anah_db, goal_id)

        row = anah_db.execute("SELECT * FROM generated_goals WHERE id = ?", (goal_id,)).fetchone()
        assert dict(row)["status"] == "dismissed"


class TestLLMGeneration:
    """LLM-based generation (mocked)."""

    def test_no_api_key_returns_empty(self):
        with patch.dict("os.environ", {}, clear=True):
            # Remove ANTHROPIC_API_KEY if present
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            goals = cortex.generate_goals_llm({}, "None")
            assert goals == []

    def test_llm_parses_json_response(self):
        """Mock a successful LLM response."""
        mock_response = json.dumps([
            {"title": "Investigate memory usage", "priority": 5,
             "description": "Memory trending up", "reasoning": "Preventive"}
        ])
        mock_data = {"content": [{"text": f"```json\n{mock_response}\n```"}]}

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.return_value.__enter__ = lambda s: s
                mock_urlopen.return_value.__exit__ = lambda s, *a: None
                mock_urlopen.return_value.read.return_value = json.dumps(mock_data).encode()
                goals = cortex.generate_goals_llm({}, "None")

        assert len(goals) == 1
        assert goals[0].title == "Investigate memory usage"
        assert goals[0].source == "llm"

    def test_llm_failure_returns_empty(self):
        """LLM errors should fail gracefully, not crash."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
                goals = cortex.generate_goals_llm({}, "None")
                assert goals == []


class TestSecurity:
    """Security boundary tests."""

    def test_api_key_not_in_goal_output(self, anah_db):
        """API keys should never appear in generated goals or task descriptions."""
        goal = cortex.Goal("test", 5, "desc", "reason", "pattern")
        goal_id = cortex.log_goal(anah_db, goal, context={"key": "should-not-leak"})
        row = anah_db.execute("SELECT * FROM generated_goals WHERE id = ?", (goal_id,)).fetchone()
        row_str = str(dict(row))
        assert "sk-ant-" not in row_str

    def test_sql_injection_in_goal_title(self, anah_db):
        """Malicious goal titles should be safely parameterized."""
        evil = cortex.Goal("'; DROP TABLE generated_goals; --", 5, "desc", "reason", "pattern")
        goal_id = cortex.log_goal(anah_db, evil)
        # Table should still exist
        count = anah_db.execute("SELECT COUNT(*) FROM generated_goals").fetchone()[0]
        assert count == 1

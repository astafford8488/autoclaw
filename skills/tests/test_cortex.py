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


class TestResponseParsing:
    """LLM response parsing (shared across providers)."""

    def test_parse_json_in_markdown_fence(self):
        content = '```json\n[{"title": "test", "priority": 3, "description": "d", "reasoning": "r"}]\n```'
        goals = cortex._parse_llm_response(content)
        assert len(goals) == 1
        assert goals[0].title == "test"

    def test_parse_bare_json(self):
        content = '[{"title": "test", "priority": 5, "description": "d", "reasoning": "r"}]'
        goals = cortex._parse_llm_response(content)
        assert len(goals) == 1

    def test_parse_single_goal_object(self):
        """Some models return a single object instead of an array."""
        content = '{"title": "solo goal", "priority": 3, "description": "d", "reasoning": "r"}'
        goals = cortex._parse_llm_response(content)
        assert len(goals) == 1
        assert goals[0].title == "solo goal"

    def test_parse_invalid_json_raises(self):
        with pytest.raises(Exception):
            cortex._parse_llm_response("not json at all")


class TestOllamaGeneration:
    """Ollama (local, free) provider."""

    def test_ollama_success(self):
        mock_response = json.dumps([
            {"title": "Optimize disk usage", "priority": 4,
             "description": "Disk at 80%", "reasoning": "Preventive"}
        ])
        mock_data = {"message": {"content": f"```json\n{mock_response}\n```"}}

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.read.return_value = json.dumps(mock_data).encode()
            goals = cortex.generate_goals_ollama({}, "None")

        assert len(goals) == 1
        assert goals[0].title == "Optimize disk usage"
        assert goals[0].source == "llm"

    def test_ollama_not_running_returns_empty(self):
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            goals = cortex.generate_goals_ollama({}, "None")
            assert goals == []

    def test_ollama_bad_response_returns_empty(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.read.return_value = b'{"message": {"content": "I cannot help"}}'
            goals = cortex.generate_goals_ollama({}, "None")
            assert goals == []


class TestHaikuGeneration:
    """Haiku (cheap cloud fallback) provider."""

    def test_haiku_success(self):
        mock_response = json.dumps([
            {"title": "Check backup integrity", "priority": 3,
             "description": "Verify backups", "reasoning": "Routine"}
        ])
        mock_data = {"content": [{"text": f"```json\n{mock_response}\n```"}]}

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.return_value.read.return_value = json.dumps(mock_data).encode()
                goals = cortex.generate_goals_haiku({}, "None")

        assert len(goals) == 1
        assert goals[0].title == "Check backup integrity"

    def test_haiku_no_api_key_returns_empty(self):
        import os
        os.environ.pop("ANTHROPIC_API_KEY", None)
        goals = cortex.generate_goals_haiku({}, "None")
        assert goals == []

    def test_haiku_failure_returns_empty(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("urllib.request.urlopen", side_effect=Exception("API error")):
                goals = cortex.generate_goals_haiku({}, "None")
                assert goals == []


class TestLLMFallbackChain:
    """Ollama → Haiku → empty fallback chain."""

    def test_ollama_success_skips_haiku(self):
        """When Ollama works, Haiku should not be called."""
        with patch.object(cortex, "generate_goals_ollama",
                          return_value=[cortex.Goal("from ollama", 3, "d", "r", "llm")]):
            with patch.object(cortex, "generate_goals_haiku") as mock_haiku:
                goals = cortex.generate_goals_llm({}, "None")
                assert len(goals) == 1
                assert goals[0].title == "from ollama"
                mock_haiku.assert_not_called()

    def test_ollama_fails_tries_haiku(self):
        """When Ollama fails, should fall through to Haiku."""
        with patch.object(cortex, "generate_goals_ollama", return_value=[]):
            with patch.object(cortex, "generate_goals_haiku",
                              return_value=[cortex.Goal("from haiku", 3, "d", "r", "llm")]):
                goals = cortex.generate_goals_llm({}, "None")
                assert len(goals) == 1
                assert goals[0].title == "from haiku"

    def test_both_fail_returns_empty(self):
        """When both providers fail, return empty for pattern fallback."""
        with patch.object(cortex, "generate_goals_ollama", return_value=[]):
            with patch.object(cortex, "generate_goals_haiku", return_value=[]):
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
        count = anah_db.execute("SELECT COUNT(*) FROM generated_goals").fetchone()[0]
        assert count == 1

    def test_sql_injection_in_goal_title(self, anah_db):
        """Malicious goal titles should be safely parameterized."""
        evil = cortex.Goal("'; DROP TABLE generated_goals; --", 5, "desc", "reason", "pattern")
        goal_id = cortex.log_goal(anah_db, evil)
        # Table should still exist
        count = anah_db.execute("SELECT COUNT(*) FROM generated_goals").fetchone()[0]
        assert count == 1

"""Tests for anah-cortex — L5 goal generation with smarter features."""

import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "anah-cortex" / "scripts"))
import cortex


# ---------------------------------------------------------------------------
# Topic hash
# ---------------------------------------------------------------------------
class TestTopicHash:
    def test_deterministic(self):
        h1 = cortex.compute_topic_hash("health report: check disk usage")
        h2 = cortex.compute_topic_hash("health report: check disk usage")
        assert h1 == h2

    def test_different_titles(self):
        h1 = cortex.compute_topic_hash("health report: disk usage")
        h2 = cortex.compute_topic_hash("cleanup: remove old logs")
        assert h1 != h2

    def test_word_order_independent(self):
        h1 = cortex.compute_topic_hash("disk usage check")
        h2 = cortex.compute_topic_hash("check usage disk")
        assert h1 == h2

    def test_stop_words_stripped(self):
        h1 = cortex.compute_topic_hash("the disk usage of the system")
        h2 = cortex.compute_topic_hash("disk usage system")
        assert h1 == h2

    def test_hash_length(self):
        assert len(cortex.compute_topic_hash("test title")) == 12


# ---------------------------------------------------------------------------
# Deduplication (updated signature: takes list[dict])
# ---------------------------------------------------------------------------
class TestDeduplication:
    def test_titles_similar_exact_match(self):
        assert cortex.titles_similar("health report", "health report") is True

    def test_titles_similar_partial_overlap(self):
        assert cortex.titles_similar(
            "investigate network latency",
            "investigate network performance"
        ) is True

    def test_titles_dissimilar(self):
        assert cortex.titles_similar(
            "investigate network latency",
            "optimize database queries"
        ) is False

    def test_titles_similar_ignores_stop_words(self):
        assert cortex.titles_similar(
            "the health report for the system",
            "a health report and system check"
        ) is True

    def test_dedup_word_overlap(self):
        goals = [
            cortex.Goal("health check report", 3, "desc", "reason", "pattern"),
            cortex.Goal("optimize database", 5, "desc", "reason", "pattern"),
        ]
        recent = [
            {"title": "health check assessment", "status": "enacted", "topic_hash": None},
        ]
        filtered = cortex.dedup_goals(goals, recent)
        assert len(filtered) == 1
        assert filtered[0].title == "optimize database"

    def test_dedup_topic_hash(self):
        goals = [cortex.Goal("check disk usage", 5, "", "", "llm")]
        h = cortex.compute_topic_hash("usage disk check")
        recent = [{"title": "totally different", "status": "enacted", "topic_hash": h}]
        filtered = cortex.dedup_goals(goals, recent)
        assert len(filtered) == 0

    def test_dedup_keeps_novel(self):
        goals = [cortex.Goal("investigate memory leak", 7, "d", "r", "llm")]
        recent = [{"title": "health report", "status": "enacted", "topic_hash": "abc"}]
        filtered = cortex.dedup_goals(goals, recent)
        assert len(filtered) == 1

    def test_dedup_empty_recent(self):
        goals = [cortex.Goal("any goal", 3, "d", "r", "pattern")]
        filtered = cortex.dedup_goals(goals, [])
        assert len(filtered) == 1

    def test_dismissed_not_used_for_word_overlap(self):
        goals = [cortex.Goal("health report: disk check", 5, "", "", "llm")]
        recent = [{"title": "health report: disk check", "status": "dismissed", "topic_hash": None}]
        filtered = cortex.dedup_goals(goals, recent)
        assert len(filtered) == 1

    def test_intra_batch_dedup(self):
        goals = [
            cortex.Goal("check disk usage", 5, "", "", "llm"),
            cortex.Goal("disk usage check", 3, "", "", "llm"),
        ]
        filtered = cortex.dedup_goals(goals, [])
        assert len(filtered) == 1


# ---------------------------------------------------------------------------
# Success rates
# ---------------------------------------------------------------------------
class TestSuccessRates:
    def test_empty_db(self, anah_db):
        rates = cortex.get_handler_success_rates(anah_db)
        assert rates == {}

    def test_rates_calculated(self, anah_dir, anah_db):
        now = time.time()
        anah_db.execute(
            "INSERT INTO task_queue (created_at, priority, source, title, description, status, completed_at) VALUES (?,?,?,?,?,?,?)",
            (now, 5, "l5", "health_report: disk", "", "completed", now),
        )
        anah_db.execute(
            "INSERT INTO task_queue (created_at, priority, source, title, description, status, completed_at) VALUES (?,?,?,?,?,?,?)",
            (now, 5, "l5", "health_report: cpu", "", "failed", now),
        )
        anah_db.commit()
        rates = cortex.get_handler_success_rates(anah_db)
        assert "health_report" in rates
        assert rates["health_report"]["total"] == 2
        assert rates["health_report"]["success_rate"] == 50.0

    def test_old_tasks_excluded(self, anah_db):
        old = time.time() - 30 * 86400
        anah_db.execute(
            "INSERT INTO task_queue (created_at, priority, source, title, description, status) VALUES (?,?,?,?,?,?)",
            (old, 5, "l5", "health_report: old", "", "completed"),
        )
        anah_db.commit()
        rates = cortex.get_handler_success_rates(anah_db)
        assert rates == {}

    def test_format_empty(self):
        assert "No task history" in cortex.format_success_rates({})

    def test_format_with_data(self):
        rates = {"health_report": {"total": 10, "completed": 8, "failed": 2, "success_rate": 80.0}}
        text = cortex.format_success_rates(rates)
        assert "80.0%" in text
        assert "good" in text


# ---------------------------------------------------------------------------
# Learned skills
# ---------------------------------------------------------------------------
class TestLearnedSkills:
    def test_no_skills_dir(self, tmp_path):
        with patch.object(cortex, "ANAH_DIR", tmp_path):
            skills = cortex.get_learned_skills()
        assert skills == []

    def test_reads_skills(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skill = skills_dir / "learned-test"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text('---\nname: learned-test\ndescription: "Test skill"\n---\nCategory: diagnostic\n')
        with patch.object(cortex, "ANAH_DIR", tmp_path):
            skills = cortex.get_learned_skills()
        assert len(skills) == 1
        assert skills[0]["name"] == "learned-test"

    def test_format_empty(self):
        assert "No learned skills" in cortex.format_skills([])

    def test_format_with_data(self):
        skills = [{"name": "learned-test", "description": "A test skill", "category": "diagnostic"}]
        text = cortex.format_skills(skills)
        assert "learned-test" in text


# ---------------------------------------------------------------------------
# Fallback generation
# ---------------------------------------------------------------------------
class TestFallbackGeneration:
    def test_generates_from_patterns(self):
        patterns = [{
            "title": "High failure rate", "category": "recurring_failure",
            "severity": "warning", "description": "Network checks failing",
            "suggested_action": "Investigate network connectivity",
        }]
        goals = cortex.generate_goals_fallback({}, patterns)
        assert len(goals) == 1
        assert goals[0].source == "pattern"

    def test_priority_from_severity(self):
        patterns = [{"title": "Critical", "category": "failure", "severity": "critical",
                      "description": "d", "suggested_action": "Fix it"}]
        goals = cortex.generate_goals_fallback({}, patterns)
        assert goals[0].priority == 7

    def test_idle_system_health_report(self):
        context = {"health_score": 95, "queue": {"queued": 0, "running": 0}}
        goals = cortex.generate_goals_fallback(context, [])
        assert len(goals) == 1
        assert "health report" in goals[0].title.lower()

    def test_busy_system_no_idle_goal(self):
        context = {"health_score": 95, "queue": {"queued": 3, "running": 1}}
        goals = cortex.generate_goals_fallback(context, [])
        assert len(goals) == 0


# ---------------------------------------------------------------------------
# Goal lifecycle
# ---------------------------------------------------------------------------
class TestGoalLifecycle:
    def test_log_goal_proposed(self, anah_dir, anah_db):
        with patch.object(cortex, "DB_FILE", anah_dir / "anah.db"), \
             patch.object(cortex, "GOAL_APPROVAL", False):
            goal = cortex.Goal("test goal", 5, "desc", "reason", "pattern")
            goal_id = cortex.log_goal(anah_db, goal)
        row = dict(anah_db.execute("SELECT * FROM generated_goals WHERE id = ?", (goal_id,)).fetchone())
        assert row["status"] == "proposed"

    def test_log_goal_pending_approval(self, anah_dir, anah_db):
        with patch.object(cortex, "DB_FILE", anah_dir / "anah.db"), \
             patch.object(cortex, "GOAL_APPROVAL", True):
            goal = cortex.Goal("test goal", 5, "desc", "reason", "llm")
            goal_id = cortex.log_goal(anah_db, goal, topic_hash="abc123", expires_at=time.time() + 300)
        row = dict(anah_db.execute("SELECT * FROM generated_goals WHERE id = ?", (goal_id,)).fetchone())
        assert row["status"] == "pending_approval"
        assert row["topic_hash"] == "abc123"
        assert row["expires_at"] is not None

    def test_enqueue_task(self, anah_dir, anah_db):
        with patch.object(cortex, "DB_FILE", anah_dir / "anah.db"):
            goal = cortex.Goal("test goal", 5, "desc", "reason", "pattern")
            goal_id = cortex.log_goal(anah_db, goal)
            task_id = cortex.enqueue_task(anah_db, goal, goal_id)
        task = dict(anah_db.execute("SELECT * FROM task_queue WHERE id = ?", (task_id,)).fetchone())
        assert task["status"] == "queued"
        assert task["source"] == "l5_generated"
        goal_row = dict(anah_db.execute("SELECT * FROM generated_goals WHERE id = ?", (goal_id,)).fetchone())
        assert goal_row["status"] == "enacted"

    def test_dismiss_goal(self, anah_dir, anah_db):
        with patch.object(cortex, "DB_FILE", anah_dir / "anah.db"):
            goal = cortex.Goal("test goal", 5, "desc", "reason", "pattern")
            goal_id = cortex.log_goal(anah_db, goal)
            cortex.dismiss_goal(anah_db, goal_id)
        row = dict(anah_db.execute("SELECT * FROM generated_goals WHERE id = ?", (goal_id,)).fetchone())
        assert row["status"] == "dismissed"

    def test_approve_goal(self, anah_dir, anah_db):
        with patch.object(cortex, "DB_FILE", anah_dir / "anah.db"):
            goal = cortex.Goal("approve me", 5, "desc", "reason", "llm")
            goal_id = cortex.log_goal(anah_db, goal)
            anah_db.execute("UPDATE generated_goals SET status = 'pending_approval' WHERE id = ?", (goal_id,))
            anah_db.commit()
            result = cortex.approve_goal(anah_db, goal_id)
        assert "task_id" in result
        assert result["approved"] == goal_id
        row = dict(anah_db.execute("SELECT status FROM generated_goals WHERE id = ?", (goal_id,)).fetchone())
        assert row["status"] == "enacted"

    def test_approve_nonexistent(self, anah_db):
        result = cortex.approve_goal(anah_db, 99999)
        assert "error" in result

    def test_approve_already_enacted(self, anah_dir, anah_db):
        with patch.object(cortex, "DB_FILE", anah_dir / "anah.db"):
            goal = cortex.Goal("done", 5, "desc", "reason", "llm")
            goal_id = cortex.log_goal(anah_db, goal)
            cortex.enqueue_task(anah_db, goal, goal_id)
            result = cortex.approve_goal(anah_db, goal_id)
        assert "error" in result


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------
class TestExpiry:
    def test_high_priority(self):
        with patch.object(cortex, "EXPIRY_HIGH", 300):
            exp = cortex.compute_expiry(8)
        assert abs(exp - (time.time() + 300)) < 2

    def test_medium_priority(self):
        with patch.object(cortex, "EXPIRY_MEDIUM", 900):
            exp = cortex.compute_expiry(5)
        assert abs(exp - (time.time() + 900)) < 2

    def test_low_priority(self):
        with patch.object(cortex, "EXPIRY_LOW", 1800):
            exp = cortex.compute_expiry(2)
        assert abs(exp - (time.time() + 1800)) < 2

    def test_expired_high_auto_enacts(self, anah_dir, anah_db):
        with patch.object(cortex, "DB_FILE", anah_dir / "anah.db"), \
             patch.object(cortex, "GOAL_APPROVAL", True):
            goal = cortex.Goal("urgent fix", 8, "desc", "reason", "llm")
            goal_id = cortex.log_goal(anah_db, goal, topic_hash="h1", expires_at=time.time() - 10)
            anah_db.execute("UPDATE generated_goals SET status = 'pending_approval' WHERE id = ?", (goal_id,))
            anah_db.commit()
            result = cortex.check_expired_approvals(anah_db)
        assert result["enacted"] == 1
        assert result["dismissed"] == 0

    def test_expired_medium_auto_enacts(self, anah_dir, anah_db):
        with patch.object(cortex, "DB_FILE", anah_dir / "anah.db"), \
             patch.object(cortex, "GOAL_APPROVAL", True):
            goal = cortex.Goal("medium task", 5, "desc", "reason", "llm")
            goal_id = cortex.log_goal(anah_db, goal, topic_hash="m1", expires_at=time.time() - 10)
            anah_db.execute("UPDATE generated_goals SET status = 'pending_approval' WHERE id = ?", (goal_id,))
            anah_db.commit()
            result = cortex.check_expired_approvals(anah_db)
        assert result["enacted"] == 1

    def test_expired_low_auto_dismisses(self, anah_dir, anah_db):
        with patch.object(cortex, "DB_FILE", anah_dir / "anah.db"), \
             patch.object(cortex, "GOAL_APPROVAL", True):
            goal = cortex.Goal("low prio", 2, "desc", "reason", "llm")
            goal_id = cortex.log_goal(anah_db, goal, topic_hash="l1", expires_at=time.time() - 10)
            anah_db.execute("UPDATE generated_goals SET status = 'pending_approval' WHERE id = ?", (goal_id,))
            anah_db.commit()
            result = cortex.check_expired_approvals(anah_db)
        assert result["dismissed"] == 1
        assert result["enacted"] == 0

    def test_unexpired_not_touched(self, anah_dir, anah_db):
        with patch.object(cortex, "DB_FILE", anah_dir / "anah.db"), \
             patch.object(cortex, "GOAL_APPROVAL", True):
            goal = cortex.Goal("still pending", 5, "desc", "reason", "llm")
            goal_id = cortex.log_goal(anah_db, goal, topic_hash="f1", expires_at=time.time() + 9999)
            anah_db.execute("UPDATE generated_goals SET status = 'pending_approval' WHERE id = ?", (goal_id,))
            anah_db.commit()
            result = cortex.check_expired_approvals(anah_db)
        assert result["enacted"] == 0
        assert result["dismissed"] == 0


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
class TestResponseParsing:
    def test_parse_json_fenced(self):
        content = '```json\n[{"title": "test", "priority": 3, "description": "d", "reasoning": "r"}]\n```'
        goals = cortex._parse_llm_response(content)
        assert len(goals) == 1
        assert goals[0].title == "test"

    def test_parse_bare_json(self):
        content = '[{"title": "test", "priority": 5, "description": "d", "reasoning": "r"}]'
        goals = cortex._parse_llm_response(content)
        assert len(goals) == 1

    def test_parse_single_object(self):
        content = '{"title": "solo", "priority": 3, "description": "d", "reasoning": "r"}'
        goals = cortex._parse_llm_response(content)
        assert len(goals) == 1

    def test_parse_invalid_json_raises(self):
        with pytest.raises(Exception):
            cortex._parse_llm_response("not json")

    def test_parse_analysis_response(self):
        content = '{"needs": ["disk"], "avoid": [], "leverage": ["health_report"]}'
        result = cortex._parse_analysis_response(content)
        assert result["needs"] == ["disk"]

    def test_parse_analysis_fallback(self):
        result = cortex._parse_analysis_response("not json")
        assert "needs" in result


# ---------------------------------------------------------------------------
# LLM fallback chain
# ---------------------------------------------------------------------------
class TestLLMFallbackChain:
    def test_ollama_success_skips_haiku(self):
        with patch.object(cortex, "_call_ollama",
                          return_value='[{"title": "from ollama", "priority": 3}]'):
            with patch.object(cortex, "_call_haiku") as mock_haiku:
                goals = cortex.generate_goals_llm({}, "None")
                assert len(goals) == 1
                assert goals[0].title == "from ollama"
                mock_haiku.assert_not_called()

    def test_ollama_fails_tries_haiku(self):
        with patch.object(cortex, "_call_ollama", return_value=None):
            with patch.object(cortex, "_call_haiku",
                              return_value='[{"title": "from haiku", "priority": 3}]'):
                goals = cortex.generate_goals_llm({}, "None")
                assert len(goals) == 1
                assert goals[0].title == "from haiku"

    def test_both_fail_returns_empty(self):
        with patch.object(cortex, "_call_ollama", return_value=None):
            with patch.object(cortex, "_call_haiku", return_value=None):
                goals = cortex.generate_goals_llm({}, "None")
                assert goals == []


# ---------------------------------------------------------------------------
# Two-phase generation
# ---------------------------------------------------------------------------
class TestTwoPhaseGeneration:
    def test_twophase_success(self, anah_dir, anah_db):
        call_count = {"n": 0}
        def mock_ollama(messages, timeout=60):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return '{"needs": ["memory"], "avoid": [], "leverage": []}'
            return '[{"title": "optimize memory", "priority": 5, "description": "d", "reasoning": "r"}]'

        with patch.object(cortex, "_call_ollama", side_effect=mock_ollama):
            goals = cortex.generate_goals_twophase(
                {"health_score": 80}, "None", {}, [])
        assert len(goals) == 1
        assert goals[0].title == "optimize memory"
        assert call_count["n"] == 2  # Analysis + Generation

    def test_twophase_analysis_fails_still_generates(self):
        """If analysis fails, should still attempt generation with defaults."""
        call_count = {"n": 0}
        def mock_ollama(messages, timeout=60):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return None  # Analysis fails
            return '[{"title": "fallback goal", "priority": 3}]'

        with patch.object(cortex, "_call_ollama", side_effect=mock_ollama), \
             patch.object(cortex, "_call_haiku", return_value=None):
            goals = cortex.generate_goals_twophase({}, "None", {}, [])
        assert len(goals) == 1

    def test_twophase_both_fail_returns_empty(self):
        with patch.object(cortex, "_call_ollama", return_value=None), \
             patch.object(cortex, "_call_haiku", return_value=None):
            goals = cortex.generate_goals_twophase({}, "None", {}, [])
        assert goals == []


# ---------------------------------------------------------------------------
# Run generation (integration)
# ---------------------------------------------------------------------------
class TestRunGeneration:
    def test_pattern_fallback(self, anah_dir, anah_db):
        context = {"health_score": 90, "queue": {"queued": 0, "running": 0}}
        patterns = [{"title": "Old logs", "category": "maintenance", "severity": "info",
                      "description": "1000 old logs", "suggested_action": "cleanup: purge old logs"}]
        with patch.object(cortex, "DB_FILE", anah_dir / "anah.db"), \
             patch.object(cortex, "ANAH_DIR", anah_dir), \
             patch.object(cortex, "GOAL_APPROVAL", False), \
             patch.object(cortex, "_call_ollama", return_value=None), \
             patch.object(cortex, "_call_haiku", return_value=None):
            results = cortex.run_generation(context, patterns)
        assert len(results) > 0
        assert results[0]["source"] == "pattern"
        assert results[0]["status"] == "enacted"

    def test_approval_mode(self, anah_dir, anah_db):
        context = {"health_score": 90, "queue": {"queued": 0, "running": 0}}
        patterns = [{"title": "Test", "category": "maint", "severity": "info",
                      "description": "Test", "suggested_action": "cleanup: test"}]
        with patch.object(cortex, "DB_FILE", anah_dir / "anah.db"), \
             patch.object(cortex, "ANAH_DIR", anah_dir), \
             patch.object(cortex, "GOAL_APPROVAL", True), \
             patch.object(cortex, "_call_ollama", return_value=None), \
             patch.object(cortex, "_call_haiku", return_value=None):
            results = cortex.run_generation(context, patterns)
        assert len(results) > 0
        assert results[0]["status"] == "pending_approval"
        assert "expires_at" in results[0]
        assert "topic_hash" in results[0]


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Goal chaining
# ---------------------------------------------------------------------------
class TestGoalChaining:
    def test_parse_chain_response(self):
        data = {"chain": True, "steps": [
            {"step": 1, "title": "research X", "priority": 5, "description": "d1", "reasoning": "r1"},
            {"step": 2, "title": "implement X", "priority": 5, "description": "d2", "reasoning": "r2", "depends_on": 1},
        ]}
        goals = cortex._parse_chain_response(data)
        assert len(goals) == 2
        assert goals[0].chain_id is not None
        assert goals[0].chain_step == 1
        assert goals[1].chain_step == 2
        assert goals[1].depends_on_goal_id == 1  # step number, not goal_id yet
        assert goals[0].chain_id == goals[1].chain_id

    def test_parse_chain_response_non_chain(self):
        data = [{"title": "solo task", "priority": 3}]
        goals = cortex._parse_chain_response(data)
        assert len(goals) == 1
        assert goals[0].chain_id is None

    def test_parse_chain_single_dict(self):
        data = {"title": "one goal", "priority": 4}
        goals = cortex._parse_chain_response(data)
        assert len(goals) == 1
        assert goals[0].title == "one goal"

    def test_chain_logged_with_ids(self, anah_dir, anah_db):
        """Chain goals logged to DB with resolved depends_on_goal_id."""
        context = {"health_score": 80, "queue": {"queued": 0, "running": 0}}
        chain_json = json.dumps({"chain": True, "steps": [
            {"step": 1, "title": "step one", "priority": 5, "description": "d", "reasoning": "r"},
            {"step": 2, "title": "step two", "priority": 5, "description": "d", "reasoning": "r", "depends_on": 1},
        ]})
        with patch.object(cortex, "DB_FILE", anah_dir / "anah.db"), \
             patch.object(cortex, "ANAH_DIR", anah_dir), \
             patch.object(cortex, "GOAL_APPROVAL", False), \
             patch.object(cortex, "_call_ollama", side_effect=[
                 '{"needs":["test"],"avoid":[],"leverage":[]}', chain_json]), \
             patch.object(cortex, "_call_haiku", return_value=None):
            results = cortex.run_generation(context, [])
        assert len(results) == 2
        # Step 1 should be enacted (no dependency)
        assert results[0]["status"] == "enacted"
        assert results[0]["chain_step"] == 1
        # Step 2 should be waiting (depends on step 1)
        assert results[1]["status"] == "waiting"
        assert results[1]["depends_on_goal_id"] == results[0]["goal_id"]

    def test_chain_promotion(self, anah_dir, anah_db):
        """Waiting chain step promotes when dependency task completes."""
        now = time.time()
        # Create step 1 goal (enacted, task completed)
        anah_db.execute(
            "INSERT INTO task_queue (id, created_at, priority, title, status) VALUES (100, ?, 5, 'step1', 'completed')",
            (now,))
        anah_db.execute(
            "INSERT INTO generated_goals (id, timestamp, title, priority, source, status, task_id, chain_id, chain_step) "
            "VALUES (200, ?, 'step1', 5, 'llm', 'enacted', 100, 'abc123', 1)", (now,))
        # Create step 2 goal (waiting on step 1)
        anah_db.execute(
            "INSERT INTO generated_goals (id, timestamp, title, priority, source, status, chain_id, chain_step, depends_on_goal_id) "
            "VALUES (201, ?, 'step2', 5, 'llm', 'waiting', 'abc123', 2, 200)", (now,))
        anah_db.commit()

        result = cortex.check_chain_promotions(anah_db)
        assert result["promoted"] == 1
        # Step 2 should now be enacted with a task
        row = dict(anah_db.execute("SELECT status, task_id FROM generated_goals WHERE id = 201").fetchone())
        assert row["status"] == "enacted"
        assert row["task_id"] is not None

    def test_chain_dismisses_on_failed_dep(self, anah_dir, anah_db):
        """Waiting chain step is dismissed when dependency task fails."""
        now = time.time()
        anah_db.execute(
            "INSERT INTO task_queue (id, created_at, priority, title, status) VALUES (100, ?, 5, 'step1', 'failed')",
            (now,))
        anah_db.execute(
            "INSERT INTO generated_goals (id, timestamp, title, priority, source, status, task_id, chain_id, chain_step) "
            "VALUES (200, ?, 'step1', 5, 'llm', 'enacted', 100, 'abc123', 1)", (now,))
        anah_db.execute(
            "INSERT INTO generated_goals (id, timestamp, title, priority, source, status, chain_id, chain_step, depends_on_goal_id) "
            "VALUES (201, ?, 'step2', 5, 'llm', 'waiting', 'abc123', 2, 200)", (now,))
        anah_db.commit()

        cortex.check_chain_promotions(anah_db)
        row = dict(anah_db.execute("SELECT status FROM generated_goals WHERE id = 201").fetchone())
        assert row["status"] == "dismissed"


# ---------------------------------------------------------------------------
# MCP tools formatting
# ---------------------------------------------------------------------------
class TestMCPTools:
    def test_format_mcp_tools(self):
        text = cortex.format_mcp_tools()
        assert "web_search" in text
        assert "web_fetch" in text
        assert "slack_send_message" in text

    def test_mcp_tools_in_generation_prompt(self):
        """GENERATION_PROMPT has {mcp_tools} placeholder."""
        assert "{mcp_tools}" in cortex.GENERATION_PROMPT


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
class TestMetacognitionIntegration:
    """Tests that cortex properly integrates metacognition strategy."""

    def test_analysis_prompt_has_strategy_placeholder(self):
        assert "{strategy}" in cortex.ANALYSIS_PROMPT

    def test_twophase_formats_strategy(self, anah_dir, anah_db):
        """generate_goals_twophase injects strategy text into ANALYSIS_PROMPT."""
        captured_messages = []

        def mock_ollama(messages, timeout=60):
            captured_messages.append(messages)
            return '{"needs": ["test"], "avoid": [], "leverage": []}'

        with patch.object(cortex, "_call_ollama", side_effect=mock_ollama), \
             patch.object(cortex, "DB_FILE", anah_dir / "anah.db"):
            # Mock strategy
            import metacognition
            with patch.object(metacognition, "load_strategy", return_value=[
                {"category": "avoid", "title": "Stop cleanup tasks", "confidence": 0.9, "timestamp": 1}
            ]):
                cortex.generate_goals_twophase({}, "None", {}, [])

        # First call should be analysis phase with strategy injected
        assert len(captured_messages) >= 1
        analysis_system = captured_messages[0][0]["content"]
        assert "Stop cleanup tasks" in analysis_system or "No strategic insights" in analysis_system

    def test_twophase_graceful_without_metacognition(self, anah_dir):
        """Doesn't crash if metacognition import fails."""
        captured = []

        def mock_ollama(messages, timeout=60):
            captured.append(messages)
            return '{"needs": ["test"], "avoid": [], "leverage": []}'

        with patch.object(cortex, "_call_ollama", side_effect=mock_ollama), \
             patch.dict("sys.modules", {"metacognition": None}):
            cortex.generate_goals_twophase({}, "None", {}, [])

        assert len(captured) >= 1
        analysis_system = captured[0][0]["content"]
        assert "No strategic insights" in analysis_system


class TestSecurity:
    def test_sql_injection_in_title(self, anah_dir, anah_db):
        with patch.object(cortex, "DB_FILE", anah_dir / "anah.db"):
            evil = cortex.Goal("'; DROP TABLE generated_goals; --", 5, "d", "r", "pattern")
            cortex.log_goal(anah_db, evil)
        count = anah_db.execute("SELECT COUNT(*) FROM generated_goals").fetchone()[0]
        assert count == 1

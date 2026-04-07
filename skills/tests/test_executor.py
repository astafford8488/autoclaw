"""Tests for anah-executor — task dequeuing, routing, and execution."""

import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "anah-executor" / "scripts"))
import executor


class TestDequeue:
    """Task dequeuing from the queue."""

    def test_dequeue_returns_highest_priority(self, anah_db):
        now = time.time()
        anah_db.execute(
            "INSERT INTO task_queue (created_at, priority, title, status) VALUES (?, 3, 'low prio', 'queued')", (now,))
        anah_db.execute(
            "INSERT INTO task_queue (created_at, priority, title, status) VALUES (?, 8, 'high prio', 'queued')", (now,))
        anah_db.commit()

        task = executor.dequeue_task(anah_db)
        assert task is not None
        assert task["title"] == "high prio"
        assert task["priority"] == 8

    def test_dequeue_sets_running_status(self, anah_db):
        now = time.time()
        anah_db.execute(
            "INSERT INTO task_queue (created_at, priority, title, status) VALUES (?, 5, 'test', 'queued')", (now,))
        anah_db.commit()

        task = executor.dequeue_task(anah_db)
        row = anah_db.execute("SELECT status FROM task_queue WHERE id = ?", (task["id"],)).fetchone()
        assert row["status"] == "running"

    def test_dequeue_empty_queue_returns_none(self, anah_db):
        assert executor.dequeue_task(anah_db) is None

    def test_dequeue_skips_non_queued(self, anah_db):
        now = time.time()
        anah_db.execute(
            "INSERT INTO task_queue (created_at, priority, title, status) VALUES (?, 5, 'running', 'running')", (now,))
        anah_db.execute(
            "INSERT INTO task_queue (created_at, priority, title, status) VALUES (?, 5, 'done', 'completed')", (now,))
        anah_db.commit()

        assert executor.dequeue_task(anah_db) is None

    def test_dequeue_fifo_within_same_priority(self, anah_db):
        anah_db.execute(
            "INSERT INTO task_queue (created_at, priority, title, status) VALUES (?, 5, 'first', 'queued')", (100.0,))
        anah_db.execute(
            "INSERT INTO task_queue (created_at, priority, title, status) VALUES (?, 5, 'second', 'queued')", (200.0,))
        anah_db.commit()

        task = executor.dequeue_task(anah_db)
        assert task["title"] == "first"


class TestCompleteAndFail:
    """Task completion and failure recording."""

    def test_complete_task(self, anah_db):
        now = time.time()
        anah_db.execute(
            "INSERT INTO task_queue (id, created_at, priority, title, status) VALUES (1, ?, 5, 'test', 'running')", (now,))
        anah_db.commit()

        executor.complete_task(anah_db, 1, {"output": "done"})
        row = anah_db.execute("SELECT * FROM task_queue WHERE id = 1").fetchone()
        assert dict(row)["status"] == "completed"
        assert json.loads(dict(row)["result"])["output"] == "done"
        assert dict(row)["completed_at"] is not None

    def test_fail_task(self, anah_db):
        now = time.time()
        anah_db.execute(
            "INSERT INTO task_queue (id, created_at, priority, title, status) VALUES (1, ?, 5, 'test', 'running')", (now,))
        anah_db.commit()

        executor.fail_task(anah_db, 1, "something broke")
        row = anah_db.execute("SELECT * FROM task_queue WHERE id = 1").fetchone()
        assert dict(row)["status"] == "failed"
        assert "something broke" in dict(row)["result"]


class TestRouting:
    """Task routing to appropriate handlers."""

    def test_route_health_report(self):
        assert executor.route_task({"title": "health_report: daily check"}) == "health_report"

    def test_route_system_health_report(self):
        assert executor.route_task({"title": "System health report: proactive assessment"}) == "health_report"

    def test_route_self_diagnostic(self):
        assert executor.route_task({"title": "self_diagnostic: check memory"}) == "self_diagnostic"

    def test_route_diagnostic_keyword(self):
        assert executor.route_task({"title": "Run full diagnostic on subsystems"}) == "self_diagnostic"

    def test_route_cleanup(self):
        assert executor.route_task({"title": "cleanup: old logs"}) == "cleanup"

    def test_route_echo(self):
        assert executor.route_task({"title": "echo: hello world"}) == "echo"

    def test_route_unknown_goes_to_ollama(self):
        assert executor.route_task({"title": "Automated Backup Configuration Review"}) == "ollama"

    def test_route_prune_keyword(self):
        assert executor.route_task({"title": "Prune old trajectory files"}) == "cleanup"


class TestEchoHandler:
    """Echo handler (simplest handler, good for testing)."""

    def test_echo_returns_description(self):
        result = executor.handle_echo({"title": "echo: test", "description": "hello world"})
        assert result.success is True
        assert result.result["echo"] == "hello world"
        assert result.result["handler"] == "echo"

    def test_echo_falls_back_to_title(self):
        result = executor.handle_echo({"title": "echo: fallback"})
        assert result.success is True
        assert result.result["echo"] == "echo: fallback"


class TestHealthReportHandler:
    """Health report handler."""

    def test_health_report_returns_score(self):
        result = executor.handle_health_report({"title": "health_report: test", "description": ""})
        assert result.success is True
        assert "health_score" in result.result
        assert "handler" in result.result
        assert result.result["handler"] == "health_report"
        assert result.duration_ms > 0

    def test_health_report_includes_patterns(self):
        result = executor.handle_health_report({"title": "health_report: test", "description": ""})
        assert "patterns_detected" in result.result
        assert isinstance(result.result.get("pattern_summaries"), list)


class TestSelfDiagnosticHandler:
    """Self-diagnostic handler."""

    def test_diagnostic_returns_recommendations(self):
        result = executor.handle_self_diagnostic({"title": "self_diagnostic: full", "description": ""})
        assert result.success is True
        assert "recommendations" in result.result
        assert isinstance(result.result["recommendations"], list)
        assert len(result.result["recommendations"]) > 0

    def test_diagnostic_includes_memory_status(self):
        result = executor.handle_self_diagnostic({"title": "self_diagnostic: full", "description": ""})
        assert "memory" in result.result


class TestCleanupHandler:
    """Cleanup handler."""

    def test_cleanup_runs_without_error(self):
        result = executor.handle_cleanup({"title": "cleanup: routine", "description": ""})
        assert result.success is True
        assert result.result["handler"] == "cleanup"
        assert "health_logs_pruned" in result.result
        assert "tasks_pruned" in result.result


class TestOllamaHandler:
    """Ollama general-purpose handler (mocked)."""

    def test_ollama_success(self):
        mock_response = json.dumps({
            "status": "completed",
            "summary": "Reviewed backup configuration",
            "findings": ["All configs up to date"],
            "recommendations": [],
        })
        mock_data = {"message": {"content": mock_response}}

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.read.return_value = json.dumps(mock_data).encode()
            result = executor.handle_ollama({"title": "Review backups", "description": "Check configs"})

        assert result.success is True
        assert result.result["handler"] == "ollama"
        assert result.result["summary"] == "Reviewed backup configuration"

    def test_ollama_not_running_fails_gracefully(self):
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            result = executor.handle_ollama({"title": "test task", "description": ""})
        assert result.success is False
        assert "Connection refused" in result.result["error"]

    def test_ollama_non_json_response_still_captured(self):
        mock_data = {"message": {"content": "I completed the backup review successfully."}}
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.read.return_value = json.dumps(mock_data).encode()
            result = executor.handle_ollama({"title": "test", "description": ""})
        assert result.success is True
        assert "backup review" in result.result.get("summary", "")


class TestExecuteNext:
    """Full execute_next flow."""

    def test_execute_next_processes_task(self, anah_db):
        now = time.time()
        anah_db.execute(
            "INSERT INTO task_queue (created_at, priority, title, description, status) "
            "VALUES (?, 5, 'echo: test', 'hello', 'queued')", (now,))
        anah_db.commit()

        with patch.object(executor, "get_db", return_value=anah_db):
            outcome = executor.execute_next(anah_db)

        assert outcome is not None
        assert outcome["handler"] == "echo"
        assert outcome["success"] is True

        # Task should be completed in DB
        row = anah_db.execute("SELECT status FROM task_queue WHERE id = ?", (outcome["task_id"],)).fetchone()
        assert row["status"] == "completed"

    def test_execute_next_empty_queue(self, anah_db):
        outcome = executor.execute_next(anah_db)
        assert outcome is None


class TestRunQueue:
    """Batch queue processing."""

    def test_run_queue_processes_multiple(self, anah_db):
        now = time.time()
        for i in range(3):
            anah_db.execute(
                "INSERT INTO task_queue (created_at, priority, title, description, status) "
                "VALUES (?, 5, ?, 'test', 'queued')", (now, f"echo: task {i}"))
        anah_db.commit()

        results = executor.run_queue(anah_db, limit=10)
        assert len(results) == 3
        assert all(r["success"] for r in results)

    def test_run_queue_respects_limit(self, anah_db):
        now = time.time()
        for i in range(5):
            anah_db.execute(
                "INSERT INTO task_queue (created_at, priority, title, description, status) "
                "VALUES (?, 5, ?, 'test', 'queued')", (now, f"echo: task {i}"))
        anah_db.commit()

        results = executor.run_queue(anah_db, limit=2)
        assert len(results) == 2

    def test_run_queue_stops_when_empty(self, anah_db):
        now = time.time()
        anah_db.execute(
            "INSERT INTO task_queue (created_at, priority, title, description, status) "
            "VALUES (?, 5, 'echo: only one', 'test', 'queued')", (now,))
        anah_db.commit()

        results = executor.run_queue(anah_db, limit=10)
        assert len(results) == 1


class TestQueueStatus:
    """Queue status reporting."""

    def test_status_counts(self, anah_db):
        now = time.time()
        anah_db.execute("INSERT INTO task_queue (created_at, title, status) VALUES (?, 'a', 'queued')", (now,))
        anah_db.execute("INSERT INTO task_queue (created_at, title, status) VALUES (?, 'b', 'queued')", (now,))
        anah_db.execute("INSERT INTO task_queue (created_at, title, status) VALUES (?, 'c', 'completed')", (now,))
        anah_db.commit()

        status = executor.queue_status(anah_db)
        assert status["counts"]["queued"] == 2
        assert status["counts"]["completed"] == 1
        assert status["total"] == 3
        assert status["next"]["title"] == "a" or status["next"]["title"] == "b"


class TestSecurity:
    """Security boundary tests."""

    def test_no_secrets_in_results(self, anah_db):
        now = time.time()
        anah_db.execute(
            "INSERT INTO task_queue (created_at, priority, title, description, status) "
            "VALUES (?, 5, 'echo: safe', 'no secrets here', 'queued')", (now,))
        anah_db.commit()

        outcome = executor.execute_next(anah_db)
        output = json.dumps(outcome)
        assert "sk-ant-" not in output
        assert "api_key" not in output.lower()

    def test_handler_crash_doesnt_leave_task_running(self, anah_db):
        """If a handler crashes, the task should be marked failed, not left running."""
        now = time.time()
        anah_db.execute(
            "INSERT INTO task_queue (created_at, priority, title, status) "
            "VALUES (?, 5, 'echo: crash test', 'queued')", (now,))
        anah_db.commit()

        def crashing_handler(task):
            raise RuntimeError("handler exploded")

        with patch.dict(executor.HANDLERS, {"echo": crashing_handler}):
            outcome = executor.execute_next(anah_db)

        assert outcome["success"] is False
        row = anah_db.execute("SELECT status FROM task_queue WHERE id = ?", (outcome["task_id"],)).fetchone()
        assert row["status"] == "failed"

    def test_sql_injection_in_task_result(self, anah_db):
        """Malicious result content should be safely stored."""
        now = time.time()
        anah_db.execute(
            "INSERT INTO task_queue (id, created_at, title, status) VALUES (1, ?, 'test', 'running')", (now,))
        anah_db.commit()

        evil_result = {"output": "'; DROP TABLE task_queue; --"}
        executor.complete_task(anah_db, 1, evil_result)
        count = anah_db.execute("SELECT COUNT(*) FROM task_queue").fetchone()[0]
        assert count == 1

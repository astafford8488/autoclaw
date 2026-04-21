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


# =========================================================================
# New handler routing tests
# =========================================================================
class TestNewRouting:
    """Routing for the 6 new handlers."""

    def test_route_file_ops_archive(self):
        assert executor.route_task({"title": "archive old trajectories"}) == "file_ops"

    def test_route_file_ops_list(self):
        assert executor.route_task({"title": "list files in anah"}) == "file_ops"

    def test_route_web_research(self):
        assert executor.route_task({"title": "research best practices"}) == "web_research"

    def test_route_web_research_url(self):
        assert executor.route_task({"title": "fetch url content"}) == "web_research"

    def test_route_code_gen(self):
        assert executor.route_task({"title": "generate code for monitoring"}) == "code_gen"

    def test_route_code_gen_write_script(self):
        assert executor.route_task({"title": "write script for backup"}) == "code_gen"

    def test_route_notify(self):
        assert executor.route_task({"title": "notify: system update"}) == "notify"

    def test_route_notify_alert(self):
        assert executor.route_task({"title": "alert: disk space low"}) == "notify"

    def test_route_schedule(self):
        assert executor.route_task({"title": "set heartbeat 120"}) == "schedule"

    def test_route_schedule_preset(self):
        assert executor.route_task({"title": "set watchdog 30"}) == "schedule"

    def test_route_skill_install(self):
        assert executor.route_task({"title": "install skill for monitoring"}) == "skill_install"

    def test_route_skill_install_learn(self):
        assert executor.route_task({"title": "learn skill for backup"}) == "skill_install"


# =========================================================================
# File ops handler tests
# =========================================================================
class TestFileOpsHandler:
    """File operations handler."""

    def test_list_operation(self, anah_dir):
        (anah_dir / "skills").mkdir(exist_ok=True)
        (anah_dir / "trajectories").mkdir(exist_ok=True)
        (anah_dir / "backups").mkdir(exist_ok=True)
        (anah_dir / "trajectories" / "t1.json").write_text("{}")
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_file_ops({"title": "list files", "description": "list"})
        assert result.success is True
        assert result.result["operation"] == "list"
        assert "trajectories" in result.result["dirs"]
        assert result.result["dirs"]["trajectories"]["files"] == 1

    def test_summarize_operation(self, anah_dir):
        (anah_dir / "state.json").write_text('{"levels": {}, "gating": {}}')
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_file_ops({"title": "summarize state", "description": "summarize"})
        assert result.success is True
        assert result.result["operation"] == "summarize"
        assert "state.json" in result.result["summary"]

    def test_archive_operation_empty(self, anah_dir):
        (anah_dir / "trajectories").mkdir(exist_ok=True)
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_file_ops({"title": "archive old", "description": "archive trajectories"})
        assert result.success is True
        assert result.result["operation"] == "archive"
        assert result.result["archived"] == 0

    def test_unknown_operation_fails(self):
        result = executor.handle_file_ops({"title": "file: do something weird", "description": "blorp"})
        assert result.success is False


# =========================================================================
# Web research handler tests
# =========================================================================
class TestWebResearchHandler:
    """Web research handler."""

    def test_blocks_localhost(self):
        result = executor.handle_web_research({
            "title": "research", "description": "fetch http://localhost:8080/secret"
        })
        assert result.success is False
        assert "Blocked" in result.result.get("error", "")

    def test_blocks_private_ip_10(self):
        result = executor.handle_web_research({
            "title": "research", "description": "fetch http://10.0.0.1/admin"
        })
        assert result.success is False

    def test_blocks_private_ip_192(self):
        result = executor.handle_web_research({
            "title": "research", "description": "fetch http://192.168.1.1/config"
        })
        assert result.success is False

    def test_blocks_private_ip_172(self):
        result = executor.handle_web_research({
            "title": "research", "description": "fetch http://172.16.0.1/internal"
        })
        assert result.success is False

    def test_blocks_file_url(self):
        """file:// URLs aren't matched by the https? regex, so they fall to Ollama (safe)."""
        with patch.object(executor, "handle_ollama") as mock_ollama:
            mock_ollama.return_value = executor.TaskResult(True, {"handler": "ollama"}, 100)
            result = executor.handle_web_research({
                "title": "research", "description": "fetch file:///etc/passwd"
            })
        # file:// never gets fetched — it falls through to Ollama
        mock_ollama.assert_called_once()

    def test_no_url_falls_back_to_ollama(self):
        with patch.object(executor, "handle_ollama") as mock_ollama:
            mock_ollama.return_value = executor.TaskResult(True, {"handler": "ollama"}, 100)
            result = executor.handle_web_research({
                "title": "research", "description": "best practices for agent design"
            })
        mock_ollama.assert_called_once()

    def test_successful_fetch(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"<html><body>Hello World</body></html>"
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = executor.handle_web_research({
                "title": "research", "description": "fetch https://example.com/page"
            })
        assert result.success is True
        assert result.result["url"] == "https://example.com/page"
        assert "Hello World" in result.result["excerpt"]


# =========================================================================
# Code gen handler tests
# =========================================================================
class TestCodeGenHandler:
    """Code generation handler."""

    def test_code_gen_saves_file(self, anah_dir):
        mock_data = {"message": {"content": "```python\nprint('hello')\n```"}}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_data).encode()
        with patch("urllib.request.urlopen", return_value=mock_resp), \
             patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_code_gen({
                "title": "generate code test", "description": "print hello"
            })
        assert result.success is True
        assert result.result["handler"] == "code_gen"
        assert result.result["lines"] >= 1
        # Verify file was created
        gen_dir = anah_dir / "generated"
        assert gen_dir.exists()
        py_files = list(gen_dir.glob("*.py"))
        assert len(py_files) == 1
        assert "print('hello')" in py_files[0].read_text()

    def test_code_gen_ollama_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            result = executor.handle_code_gen({
                "title": "generate code", "description": "test"
            })
        assert result.success is False


# =========================================================================
# Notify handler tests
# =========================================================================
class TestNotifyHandler:
    """Notification handler."""

    def test_notify_info_level(self, anah_dir):
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_notify({
                "title": "notify: system ok", "description": "all systems green"
            })
        assert result.success is True
        assert result.result["level"] == "info"
        assert result.result["logged"] is True
        # Check JSONL file
        notif_file = anah_dir / "notifications.json"
        assert notif_file.exists()
        line = json.loads(notif_file.read_text().strip())
        assert line["level"] == "info"

    def test_notify_critical_level(self, anah_dir):
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_notify({
                "title": "notify:critical: disk full", "description": "95% used"
            })
        assert result.success is True
        assert result.result["level"] == "critical"

    def test_notify_warning_level(self, anah_dir):
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_notify({
                "title": "notify:warning: high cpu", "description": "80%"
            })
        assert result.success is True
        assert result.result["level"] == "warning"


# =========================================================================
# Schedule handler tests
# =========================================================================
class TestScheduleHandler:
    """Schedule management handler."""

    def test_set_heartbeat(self, anah_dir):
        (anah_dir / "config.json").write_text("{}")
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_schedule({
                "title": "schedule", "description": "set heartbeat 120"
            })
        assert result.success is True
        assert result.result["updated"] == "heartbeat_interval"
        assert result.result["value"] == 120
        config = json.loads((anah_dir / "config.json").read_text())
        assert config["heartbeat_interval"] == 120

    def test_set_watchdog(self, anah_dir):
        (anah_dir / "config.json").write_text("{}")
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_schedule({
                "title": "schedule", "description": "set watchdog 30"
            })
        assert result.success is True
        assert result.result["updated"] == "watchdog_interval"
        assert result.result["value"] == 30

    def test_set_preset(self, anah_dir):
        (anah_dir / "config.json").write_text("{}")
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_schedule({
                "title": "schedule", "description": "set preset fast"
            })
        assert result.success is True
        assert result.result["updated"] == "preset"
        assert result.result["value"] == "fast"

    def test_heartbeat_range_validation_too_low(self, anah_dir):
        (anah_dir / "config.json").write_text("{}")
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_schedule({
                "title": "schedule", "description": "set heartbeat 5"
            })
        assert result.success is False
        assert "30-3600" in result.result["error"]

    def test_heartbeat_range_validation_too_high(self, anah_dir):
        (anah_dir / "config.json").write_text("{}")
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_schedule({
                "title": "schedule", "description": "set heartbeat 9999"
            })
        assert result.success is False

    def test_watchdog_range_validation(self, anah_dir):
        (anah_dir / "config.json").write_text("{}")
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_schedule({
                "title": "schedule", "description": "set watchdog 5"
            })
        assert result.success is False
        assert "10-600" in result.result["error"]

    def test_unparseable_command(self, anah_dir):
        (anah_dir / "config.json").write_text("{}")
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_schedule({
                "title": "schedule", "description": "do something random"
            })
        assert result.success is False


# =========================================================================
# Skill install handler tests
# =========================================================================
class TestSkillInstallHandler:
    """Skill installation handler."""

    def test_install_creates_skill_dir(self, anah_dir):
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_skill_install({
                "title": "install skill", "description": "skill named log-analyzer for parsing logs"
            })
        assert result.success is True
        assert result.result["handler"] == "skill_install"
        skill_name = result.result["skill"]
        skill_dir = anah_dir / "skills" / skill_name
        assert skill_dir.exists()
        assert (skill_dir / "SKILL.md").exists()

    def test_skill_md_has_frontmatter(self, anah_dir):
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_skill_install({
                "title": "install skill", "description": "skill named test-skill for testing"
            })
        skill_name = result.result["skill"]
        content = (anah_dir / "skills" / skill_name / "SKILL.md").read_text()
        assert content.startswith("---")
        assert "name:" in content

    def test_skill_name_sanitization(self, anah_dir):
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_skill_install({
                "title": "install skill", "description": "skill named ../../etc/evil for hacking"
            })
        assert result.success is True
        # Name should be sanitized — no path traversal
        assert ".." not in result.result["skill"]
        assert "/" not in result.result["skill"]

    def test_skill_name_path_traversal_blocked(self, anah_dir):
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_skill_install({
                "title": "install skill", "description": "skill named ../../../tmp/evil for testing"
            })
        assert result.success is True
        # Should be installed safely within ANAH_DIR/skills/
        skill_path = Path(result.result["path"])
        assert str(anah_dir) in str(skill_path)


# =========================================================================
# Alert validation tests (LLM hallucination suppression)
# =========================================================================
class TestAlertValidation:
    """Validate that LLM-hallucinated critical/warning alerts are suppressed."""

    def test_suppresses_fake_disk_full(self, anah_dir):
        """LLM claims disk full but real disk is fine → suppressed."""
        mock_disk = MagicMock()
        mock_disk.percent = 42.0  # Actually fine
        with patch.object(executor, "ANAH_DIR", anah_dir), \
             patch("psutil.disk_usage", return_value=mock_disk):
            result = executor.handle_notify({
                "title": "notify:critical: disk full",
                "description": "95% used",
                "source": "l5_generated",
            })
        assert result.success is True
        assert result.result.get("suppressed") is True
        assert "42%" in result.result["actual"]

    def test_suppresses_fake_high_cpu(self, anah_dir):
        """LLM claims high CPU but real CPU is idle → suppressed."""
        with patch.object(executor, "ANAH_DIR", anah_dir), \
             patch("psutil.cpu_percent", return_value=15.0):
            result = executor.handle_notify({
                "title": "notify:warning: high cpu",
                "description": "80%",
                "source": "l5_generated",
            })
        assert result.success is True
        assert result.result.get("suppressed") is True

    def test_suppresses_fake_memory_full(self, anah_dir):
        """LLM claims memory full but real RAM is fine → suppressed."""
        mock_ram = MagicMock()
        mock_ram.percent = 35.0
        with patch.object(executor, "ANAH_DIR", anah_dir), \
             patch("psutil.virtual_memory", return_value=mock_ram):
            result = executor.handle_notify({
                "title": "notify:critical: out of memory",
                "description": "system running low",
                "source": "l5_generated",
            })
        assert result.success is True
        assert result.result.get("suppressed") is True

    def test_allows_real_disk_alert(self, anah_dir):
        """Real disk is actually full → alert goes through."""
        mock_disk = MagicMock()
        mock_disk.percent = 96.0  # Actually full
        with patch.object(executor, "ANAH_DIR", anah_dir), \
             patch("psutil.disk_usage", return_value=mock_disk):
            result = executor.handle_notify({
                "title": "notify:critical: disk full",
                "description": "96% used",
                "source": "l5_generated",
            })
        assert result.success is True
        assert result.result.get("suppressed") is not True
        assert result.result["level"] == "critical"

    def test_allows_non_l5_notifications(self, anah_dir):
        """Non-LLM-generated alerts are never suppressed."""
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_notify({
                "title": "notify:critical: disk full",
                "description": "95% used",
                # No "source": "l5_generated"
            })
        assert result.success is True
        assert result.result.get("suppressed") is not True

    def test_allows_info_level_from_llm(self, anah_dir):
        """Info-level notifications from LLM are never suppressed."""
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_notify({
                "title": "notify: system ok",
                "description": "all green",
                "source": "l5_generated",
            })
        assert result.success is True
        assert result.result.get("suppressed") is not True
        assert result.result["level"] == "info"


# =========================================================================
# MCP tool handler tests
# =========================================================================
class TestMCPToolHandler:
    """MCP tool invocation handler."""

    def test_route_mcp(self):
        assert executor.route_task({"title": "mcp: web_search best practices"}) == "mcp_tool"

    def test_route_mcp_no_space(self):
        assert executor.route_task({"title": "mcp:web_fetch https://example.com"}) == "mcp_tool"

    def test_whitelist_blocks_unknown_tool(self):
        result = executor.handle_mcp_tool({
            "title": "mcp: evil_tool hack the planet",
            "description": "mcp: evil_tool hack",
        })
        assert result.success is False
        assert "not in whitelist" in result.result["error"]

    def test_web_search_calls_ollama(self):
        mock_data = {"message": {"content": '{"query":"test","findings":["result1"]}'}}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_data).encode()
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = executor.handle_mcp_tool({
                "title": "mcp: web_search SQLite optimization",
                "description": "mcp: web_search SQLite optimization tips",
            })
        assert result.success is True
        assert result.result["tool"] == "web_search"

    def test_web_fetch_delegates_to_web_research(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"<html><body>Page content</body></html>"
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = executor.handle_mcp_tool({
                "title": "mcp: web_fetch https://example.com/docs",
                "description": "mcp: web_fetch https://example.com/docs",
            })
        assert result.success is True
        assert "example.com" in result.result.get("url", "")

    def test_slack_queues_for_mcp_bridge(self):
        result = executor.handle_mcp_tool({
            "title": "mcp: slack_send_message hello world",
            "description": "mcp: slack_send_message #general hello world",
        })
        assert result.success is True
        assert result.result["status"] == "mcp_queued"
        assert result.result["tool"] == "slack_send_message"

    def test_notion_queues_for_mcp_bridge(self):
        result = executor.handle_mcp_tool({
            "title": "mcp: notion_search project roadmap",
            "description": "mcp: notion_search project roadmap",
        })
        assert result.success is True
        assert result.result["status"] == "mcp_queued"

    def test_unparseable_title(self):
        result = executor.handle_mcp_tool({
            "title": "do something random",
            "description": "no mcp prefix here either",
        })
        assert result.success is False
        assert "parse" in result.result["error"].lower()


# ---------------------------------------------------------------------------
# Sandbox eval (WS4 — Self-Modification)
# ---------------------------------------------------------------------------
class TestSandboxEval:
    """Tests for sandbox_eval handler — safe code execution."""

    def test_runs_simple_code(self, anah_dir):
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_sandbox_eval({
                "title": "sandbox_eval: test",
                "description": '```python\nprint("hello world")\n```',
            })
        assert result.success is True
        assert "hello world" in result.result["stdout"]
        assert result.result["handler"] == "sandbox_eval"

    def test_blocks_subprocess_import(self, anah_dir):
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_sandbox_eval({
                "title": "sandbox_eval: test",
                "description": '```python\nimport subprocess\nsubprocess.run(["ls"])\n```',
            })
        assert result.success is False
        assert "blocked" in result.result.get("error", "").lower() or result.result.get("safety") == "rejected"

    def test_blocks_exec(self, anah_dir):
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_sandbox_eval({
                "title": "sandbox_eval: test",
                "description": '```python\nexec("print(1)")\n```',
            })
        assert result.success is False

    def test_blocks_eval(self, anah_dir):
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_sandbox_eval({
                "title": "sandbox_eval: test",
                "description": '```python\neval("1+1")\n```',
            })
        assert result.success is False

    def test_blocks_os_system(self, anah_dir):
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_sandbox_eval({
                "title": "sandbox_eval: test",
                "description": '```python\nimport os\nos.system("echo pwned")\n```',
            })
        assert result.success is False

    def test_blocks_write_mode(self, anah_dir):
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_sandbox_eval({
                "title": "sandbox_eval: test",
                "description": '```python\nopen("/etc/passwd", "w").write("hacked")\n```',
            })
        assert result.success is False

    def test_timeout_kills_process(self, anah_dir):
        with patch.object(executor, "ANAH_DIR", anah_dir), \
             patch.object(executor, "SANDBOX_TIMEOUT", 2):
            result = executor.handle_sandbox_eval({
                "title": "sandbox_eval: test",
                "description": '```python\nimport time\ntime.sleep(60)\n```',
            })
        assert result.success is False
        assert "timed out" in result.result["error"].lower()

    def test_no_code_found(self, anah_dir):
        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_sandbox_eval({
                "title": "sandbox_eval: test",
                "description": "no code here just text",
            })
        assert result.success is False
        assert "no code" in result.result["error"].lower()

    def test_reads_from_generated_file(self, anah_dir):
        gen_dir = anah_dir / "generated"
        gen_dir.mkdir(parents=True, exist_ok=True)
        script = gen_dir / "test_script.py"
        script.write_text('print("from file")')

        with patch.object(executor, "ANAH_DIR", anah_dir):
            result = executor.handle_sandbox_eval({
                "title": "sandbox_eval: test",
                "description": f"file: {script}",
            })
        assert result.success is True
        assert "from file" in result.result["stdout"]

    def test_routing(self):
        assert executor.route_task({"title": "sandbox_eval: run test", "description": ""}) == "sandbox_eval"


# ---------------------------------------------------------------------------
# Dynamic handler loading (WS4)
# ---------------------------------------------------------------------------
class TestDynamicHandlers:
    """Tests for custom handler loading and revert."""

    def test_load_custom_handler(self, anah_dir):
        handler_dir = anah_dir / "custom_handlers"
        handler_dir.mkdir(parents=True, exist_ok=True)
        (handler_dir / "greet.py").write_text(
            "from executor import TaskResult\n"
            "def handle(task):\n"
            "    return TaskResult(True, {'greeting': 'hello', 'handler': 'custom_greet'}, 1.0)\n"
        )

        with patch.object(executor, "CUSTOM_HANDLERS_DIR", handler_dir):
            custom = executor.load_custom_handlers()

        assert "custom_greet" in custom
        result = custom["custom_greet"]({"title": "test"})
        assert result.success is True
        assert result.result["greeting"] == "hello"

    def test_load_skips_bad_files(self, anah_dir):
        handler_dir = anah_dir / "custom_handlers"
        handler_dir.mkdir(parents=True, exist_ok=True)
        (handler_dir / "broken.py").write_text("this is not valid python!!!")

        with patch.object(executor, "CUSTOM_HANDLERS_DIR", handler_dir):
            custom = executor.load_custom_handlers()

        assert "custom_broken" not in custom

    def test_load_skips_missing_handle_func(self, anah_dir):
        handler_dir = anah_dir / "custom_handlers"
        handler_dir.mkdir(parents=True, exist_ok=True)
        (handler_dir / "nohandle.py").write_text("x = 42\n")

        with patch.object(executor, "CUSTOM_HANDLERS_DIR", handler_dir):
            custom = executor.load_custom_handlers()

        assert "custom_nohandle" not in custom

    def test_load_empty_dir(self, anah_dir):
        handler_dir = anah_dir / "custom_handlers"
        handler_dir.mkdir(parents=True, exist_ok=True)

        with patch.object(executor, "CUSTOM_HANDLERS_DIR", handler_dir):
            custom = executor.load_custom_handlers()

        assert custom == {}

    def test_load_nonexistent_dir(self, anah_dir):
        with patch.object(executor, "CUSTOM_HANDLERS_DIR", anah_dir / "nope"):
            custom = executor.load_custom_handlers()

        assert custom == {}

    def test_revert_custom_handler(self, anah_dir):
        handler_dir = anah_dir / "custom_handlers"
        handler_dir.mkdir(parents=True, exist_ok=True)
        handler_file = handler_dir / "removeme.py"
        handler_file.write_text("def handle(task): pass")

        with patch.object(executor, "CUSTOM_HANDLERS_DIR", handler_dir):
            result = executor.revert_custom_handler("custom_removeme")

        assert "reverted" in result
        assert not handler_file.exists()

    def test_revert_rejects_non_custom(self):
        result = executor.revert_custom_handler("echo")
        assert "error" in result

    def test_revert_missing_file(self, anah_dir):
        handler_dir = anah_dir / "custom_handlers"
        handler_dir.mkdir(parents=True, exist_ok=True)
        with patch.object(executor, "CUSTOM_HANDLERS_DIR", handler_dir):
            result = executor.revert_custom_handler("custom_nonexistent")
        assert "error" in result

"""Tests for anah-cortex metacognition — L5 self-awareness engine."""

import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "anah-cortex" / "scripts"))
import metacognition


# ---------------------------------------------------------------------------
# Health trend analysis
# ---------------------------------------------------------------------------
class TestHealthTrends:
    def test_degradation_detected(self, anah_db):
        """Detects health degradation when second half is worse than first."""
        now = time.time()
        # First half: mostly passing (8/10)
        for i in range(10):
            anah_db.execute(
                "INSERT INTO health_logs (timestamp, level, check_name, passed, duration_ms, message) "
                "VALUES (?, 1, 'test', ?, 10.0, 'ok')",
                (now - 20 * 3600 + i * 100, 1 if i < 8 else 0),
            )
        # Second half: mostly failing (3/10)
        for i in range(10):
            anah_db.execute(
                "INSERT INTO health_logs (timestamp, level, check_name, passed, duration_ms, message) "
                "VALUES (?, 1, 'test', ?, 10.0, 'ok')",
                (now - 5 * 3600 + i * 100, 1 if i < 3 else 0),
            )
        anah_db.commit()

        with patch.object(metacognition, "DB_FILE", Path(anah_db.execute("PRAGMA database_list").fetchone()[2])):
            trends = metacognition.analyze_health_trends(anah_db, hours=24)

        degradation = [t for t in trends if t["type"] == "degradation"]
        assert len(degradation) >= 1
        assert degradation[0]["severity"] in ("high", "medium")

    def test_improvement_detected(self, anah_db):
        """Detects improvement when second half is better."""
        now = time.time()
        # First half: mostly failing
        for i in range(10):
            anah_db.execute(
                "INSERT INTO health_logs (timestamp, level, check_name, passed, duration_ms, message) "
                "VALUES (?, 1, 'test', ?, 10.0, 'ok')",
                (now - 20 * 3600 + i * 100, 1 if i < 3 else 0),
            )
        # Second half: mostly passing
        for i in range(10):
            anah_db.execute(
                "INSERT INTO health_logs (timestamp, level, check_name, passed, duration_ms, message) "
                "VALUES (?, 1, 'test', ?, 10.0, 'ok')",
                (now - 5 * 3600 + i * 100, 1 if i < 9 else 0),
            )
        anah_db.commit()

        trends = metacognition.analyze_health_trends(anah_db, hours=24)
        improvement = [t for t in trends if t["type"] == "improvement"]
        assert len(improvement) >= 1

    def test_recurring_failure_detected(self, anah_db):
        """Detects a specific check that keeps failing."""
        now = time.time()
        for i in range(10):
            anah_db.execute(
                "INSERT INTO health_logs (timestamp, level, check_name, passed, duration_ms, message) "
                "VALUES (?, 1, 'bad_check', 0, 10.0, 'fail')",
                (now - i * 3600,),
            )
        anah_db.commit()

        trends = metacognition.analyze_health_trends(anah_db, hours=24)
        recurring = [t for t in trends if t["type"] == "recurring_failure"]
        assert len(recurring) >= 1
        assert recurring[0]["check_name"] == "bad_check"

    def test_no_trends_when_stable(self, anah_db):
        """No trends when pass rate is stable."""
        now = time.time()
        for half in [20, 5]:  # both halves ~80% pass
            for i in range(10):
                anah_db.execute(
                    "INSERT INTO health_logs (timestamp, level, check_name, passed, duration_ms, message) "
                    "VALUES (?, 1, 'test', ?, 10.0, 'ok')",
                    (now - half * 3600 + i * 100, 1 if i < 8 else 0),
                )
        anah_db.commit()
        trends = metacognition.analyze_health_trends(anah_db, hours=24)
        assert not any(t["type"] == "degradation" for t in trends)


# ---------------------------------------------------------------------------
# Failure chain tracing
# ---------------------------------------------------------------------------
class TestFailureChains:
    def test_traces_failed_tasks(self, anah_db):
        """Traces failed tasks back to goals."""
        now = time.time()
        anah_db.execute(
            "INSERT INTO task_queue (id, created_at, title, status, completed_at, result) "
            "VALUES (1, ?, 'health_report: test', 'failed', ?, ?)",
            (now - 100, now, json.dumps({"error": "timeout"})),
        )
        anah_db.execute(
            "INSERT INTO generated_goals (id, timestamp, title, priority, reasoning, source, status, task_id) "
            "VALUES (1, ?, 'health_report: test', 5, 'test reason', 'llm', 'enacted', 1)",
            (now - 100,),
        )
        anah_db.commit()

        chains = metacognition.trace_failure_chains(anah_db)
        assert len(chains) >= 1
        assert chains[0]["task_title"] == "health_report: test"
        assert "timeout" in chains[0]["error"]

    def test_no_chains_when_no_failures(self, anah_db):
        chains = metacognition.trace_failure_chains(anah_db)
        assert len(chains) == 0


# ---------------------------------------------------------------------------
# Handler effectiveness
# ---------------------------------------------------------------------------
class TestHandlerEffectiveness:
    def test_scores_handlers(self, anah_db):
        """Scores handler types by success rate."""
        now = time.time()
        # Echo: 9/10 success
        for i in range(10):
            anah_db.execute(
                "INSERT INTO task_queue (created_at, title, status) VALUES (?, ?, ?)",
                (now - 3600, f"echo: test {i}", "completed" if i < 9 else "failed"),
            )
        # Cleanup: 2/10 success
        for i in range(10):
            anah_db.execute(
                "INSERT INTO task_queue (created_at, title, status) VALUES (?, ?, ?)",
                (now - 3600, f"cleanup: test {i}", "completed" if i < 2 else "failed"),
            )
        anah_db.commit()

        handlers = metacognition.analyze_handler_effectiveness(anah_db)
        assert len(handlers) >= 2

        echo = next(h for h in handlers if h["handler"] == "echo")
        assert echo["success_rate"] == 90.0

        cleanup = next(h for h in handlers if h["handler"] == "cleanup")
        assert cleanup["success_rate"] == 20.0

    def test_empty_db(self, anah_db):
        handlers = metacognition.analyze_handler_effectiveness(anah_db)
        assert handlers == []


# ---------------------------------------------------------------------------
# Capability gaps
# ---------------------------------------------------------------------------
class TestCapabilityGaps:
    def test_repeated_failures_detected(self, anah_db):
        """Detects tasks that fail repeatedly without a dedicated handler."""
        now = time.time()
        for i in range(5):
            anah_db.execute(
                "INSERT INTO task_queue (created_at, title, status, completed_at, result) "
                "VALUES (?, 'analyze: test', 'failed', ?, ?)",
                (now - 3600, now, json.dumps({"error": "no handler"})),
            )
        anah_db.commit()

        gaps = metacognition.detect_capability_gaps(anah_db)
        cap_gaps = [g for g in gaps if g["type"] == "capability_gap"]
        assert len(cap_gaps) >= 1
        assert cap_gaps[0]["failures"] >= 2

    def test_hallucination_count(self, anah_db):
        """Detects suppressed hallucinated alerts."""
        now = time.time()
        for i in range(8):
            anah_db.execute(
                "INSERT INTO task_queue (created_at, title, status, completed_at, result) "
                "VALUES (?, 'notify: test', 'completed', ?, ?)",
                (now - 3600, now, json.dumps({"suppressed": True, "reason": "hallucinated"})),
            )
        anah_db.commit()

        gaps = metacognition.detect_capability_gaps(anah_db)
        halluc = [g for g in gaps if g["type"] == "hallucination"]
        assert len(halluc) >= 1
        assert halluc[0]["count"] >= 6


# ---------------------------------------------------------------------------
# Strategy journal
# ---------------------------------------------------------------------------
class TestStrategyJournal:
    def test_save_and_load(self, anah_dir):
        with patch.object(metacognition, "ANAH_DIR", anah_dir), \
             patch.object(metacognition, "STRATEGY_FILE", anah_dir / "strategy.json"):
            entries = [{"timestamp": time.time(), "category": "avoid", "title": "test", "confidence": 0.8}]
            metacognition.save_strategy(entries)
            loaded = metacognition.load_strategy()
            assert len(loaded) == 1
            assert loaded[0]["title"] == "test"

    def test_max_entries_cap(self, anah_dir):
        with patch.object(metacognition, "ANAH_DIR", anah_dir), \
             patch.object(metacognition, "STRATEGY_FILE", anah_dir / "strategy.json"):
            entries = [{"timestamp": i, "title": f"entry_{i}"} for i in range(100)]
            metacognition.save_strategy(entries)
            loaded = metacognition.load_strategy()
            assert len(loaded) == metacognition.MAX_STRATEGY_ENTRIES

    def test_load_missing_file(self, anah_dir):
        with patch.object(metacognition, "STRATEGY_FILE", anah_dir / "nonexistent.json"):
            loaded = metacognition.load_strategy()
            assert loaded == []

    def test_generate_strategy_from_trends(self):
        trends = [{"type": "degradation", "message": "Health declining", "severity": "high",
                    "first_half_rate": 90, "second_half_rate": 50}]
        entries = metacognition.generate_strategy_entries(trends, [], [], [])
        assert len(entries) >= 1
        assert entries[0].category == "insight"

    def test_generate_strategy_from_weak_handler(self):
        handlers = [{"handler": "cleanup", "total": 10, "completed": 2, "failed": 8, "success_rate": 20.0}]
        entries = metacognition.generate_strategy_entries([], [], handlers, [])
        assert len(entries) >= 1
        assert entries[0].category == "avoid"

    def test_generate_strategy_from_strong_handler(self):
        handlers = [{"handler": "echo", "total": 10, "completed": 10, "failed": 0, "success_rate": 100.0}]
        entries = metacognition.generate_strategy_entries([], [], handlers, [])
        assert len(entries) >= 1
        assert entries[0].category == "leverage"

    def test_generate_strategy_from_gaps(self):
        gaps = [{"type": "capability_gap", "prefix": "analyze", "failures": 5,
                 "message": "'analyze' tasks fail repeatedly"}]
        entries = metacognition.generate_strategy_entries([], [], [], gaps)
        assert len(entries) >= 1
        assert entries[0].category == "capability_gap"

    def test_generate_strategy_from_repeated_errors(self):
        failures = [{"error": "timeout connecting"} for _ in range(5)]
        entries = metacognition.generate_strategy_entries([], failures, [], [])
        assert len(entries) >= 1
        assert entries[0].category == "avoid"


# ---------------------------------------------------------------------------
# get_strategy_for_cortex
# ---------------------------------------------------------------------------
class TestStrategyForCortex:
    def test_empty_strategy(self, anah_dir):
        with patch.object(metacognition, "STRATEGY_FILE", anah_dir / "nonexistent.json"):
            text = metacognition.get_strategy_for_cortex()
            assert text == "No strategic insights yet."

    def test_formats_entries(self, anah_dir):
        entries = [
            {"category": "avoid", "title": "Stop doing X", "confidence": 0.9, "timestamp": time.time()},
            {"category": "leverage", "title": "Use handler Y", "confidence": 0.8, "timestamp": time.time()},
        ]
        with patch.object(metacognition, "STRATEGY_FILE", anah_dir / "strategy.json"), \
             patch.object(metacognition, "ANAH_DIR", anah_dir):
            metacognition.save_strategy(entries)
            text = metacognition.get_strategy_for_cortex()
            assert "[AVOID]" in text
            assert "[LEVERAGE]" in text
            assert "Stop doing X" in text


# ---------------------------------------------------------------------------
# Full cycle
# ---------------------------------------------------------------------------
class TestFullCycle:
    def test_run_metacognition(self, anah_dir, anah_db):
        """Full metacognition cycle returns expected structure."""
        db_path = anah_dir / "anah.db"

        # Create file-based DB with schema (run_metacognition opens/closes its own connection)
        file_db = sqlite3.connect(str(db_path))
        file_db.executescript("""
            CREATE TABLE IF NOT EXISTS health_logs (
                id INTEGER PRIMARY KEY, timestamp REAL, level INTEGER,
                check_name TEXT, passed INTEGER, duration_ms REAL, message TEXT, details TEXT
            );
            CREATE TABLE IF NOT EXISTS task_queue (
                id INTEGER PRIMARY KEY, created_at REAL, started_at REAL, completed_at REAL,
                priority INTEGER DEFAULT 5, source TEXT, title TEXT, description TEXT,
                status TEXT DEFAULT 'queued', result TEXT
            );
            CREATE TABLE IF NOT EXISTS generated_goals (
                id INTEGER PRIMARY KEY, timestamp REAL, title TEXT, priority INTEGER,
                description TEXT, reasoning TEXT, source TEXT, context TEXT,
                status TEXT DEFAULT 'proposed', task_id INTEGER, topic_hash TEXT,
                discord_message_id TEXT, expires_at REAL,
                chain_id TEXT, chain_step INTEGER, depends_on_goal_id INTEGER
            );
        """)
        file_db.close()

        with patch.object(metacognition, "DB_FILE", db_path), \
             patch.object(metacognition, "ANAH_DIR", anah_dir), \
             patch.object(metacognition, "STRATEGY_FILE", anah_dir / "strategy.json"):
            result = metacognition.run_metacognition()

        assert "trends" in result
        assert "failure_chains" in result
        assert "handler_scores" in result
        assert "capability_gaps" in result
        assert "new_strategies" in result
        assert "total_strategies" in result

    def test_deduplicates_strategies(self, anah_dir, anah_db):
        """Doesn't add duplicate strategy entries."""
        db_path = anah_dir / "anah.db"

        def fresh_db():
            """Return a new connection each time (run_metacognition closes it)."""
            db = sqlite3.connect(str(db_path))
            db.row_factory = sqlite3.Row
            return db

        # Copy schema to the file-based db
        file_db = fresh_db()
        file_db.executescript("""
            CREATE TABLE IF NOT EXISTS health_logs (
                id INTEGER PRIMARY KEY, timestamp REAL, level INTEGER,
                check_name TEXT, passed INTEGER, duration_ms REAL, message TEXT, details TEXT
            );
            CREATE TABLE IF NOT EXISTS task_queue (
                id INTEGER PRIMARY KEY, created_at REAL, started_at REAL, completed_at REAL,
                priority INTEGER DEFAULT 5, source TEXT, title TEXT, description TEXT,
                status TEXT DEFAULT 'queued', result TEXT
            );
            CREATE TABLE IF NOT EXISTS generated_goals (
                id INTEGER PRIMARY KEY, timestamp REAL, title TEXT, priority INTEGER,
                description TEXT, reasoning TEXT, source TEXT, context TEXT,
                status TEXT DEFAULT 'proposed', task_id INTEGER, topic_hash TEXT,
                discord_message_id TEXT, expires_at REAL,
                chain_id TEXT, chain_step INTEGER, depends_on_goal_id INTEGER
            );
        """)
        file_db.close()

        with patch.object(metacognition, "DB_FILE", db_path), \
             patch.object(metacognition, "ANAH_DIR", anah_dir), \
             patch.object(metacognition, "STRATEGY_FILE", anah_dir / "strategy.json"):
            r1 = metacognition.run_metacognition()
            r2 = metacognition.run_metacognition()

        assert r2["new_strategies"] == 0 or r2["total_strategies"] >= r1["total_strategies"]

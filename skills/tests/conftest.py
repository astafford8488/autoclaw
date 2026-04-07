"""Shared fixtures for ANAH skill tests."""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Add all skill script directories to path
SKILLS_ROOT = Path(__file__).resolve().parent.parent
for skill_dir in SKILLS_ROOT.glob("anah-*/scripts"):
    if str(skill_dir) not in sys.path:
        sys.path.insert(0, str(skill_dir))


@pytest.fixture
def anah_dir(tmp_path):
    """Create a temporary ~/.anah directory for test isolation."""
    anah = tmp_path / ".anah"
    anah.mkdir()
    (anah / "backups").mkdir()
    (anah / "skills").mkdir()
    (anah / "trajectories").mkdir()

    # Minimal config
    config = {
        "check_intervals": {"l1": 30, "l2": 300, "l3": 900},
        "thresholds": {"cpu_percent": 90, "ram_percent": 85, "disk_percent": 95},
        "integrations": [],
    }
    (anah / "config.json").write_text(json.dumps(config))

    # Empty state
    (anah / "state.json").write_text(json.dumps({"levels": {}, "gating": {}}))

    return anah


@pytest.fixture
def anah_db(anah_dir):
    """Create a test database with the ANAH schema."""
    db_path = anah_dir / "anah.db"
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS health_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL, level INTEGER, check_name TEXT,
            passed INTEGER, duration_ms REAL, message TEXT, details TEXT
        );
        CREATE TABLE IF NOT EXISTS task_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL, started_at REAL, completed_at REAL,
            priority INTEGER DEFAULT 5, source TEXT DEFAULT 'manual',
            title TEXT, description TEXT, status TEXT DEFAULT 'queued',
            result TEXT
        );
        CREATE TABLE IF NOT EXISTS generated_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL, title TEXT, priority INTEGER,
            description TEXT, reasoning TEXT, source TEXT,
            context TEXT, status TEXT DEFAULT 'proposed', task_id INTEGER,
            topic_hash TEXT, discord_message_id TEXT, expires_at REAL
        );
        CREATE TABLE IF NOT EXISTS agent_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL, action_type TEXT, description TEXT,
            result TEXT, confidence REAL
        );
    """)
    db.commit()
    yield db
    db.close()


@pytest.fixture
def patch_anah_dir(anah_dir):
    """Patch ANAH_DIR across all skill modules to use the temp directory."""
    patches = []
    module_names = ["brainstem", "cerebellum", "cortex", "hippocampus", "memory"]
    for mod_name in module_names:
        try:
            mod = sys.modules.get(mod_name)
            if mod and hasattr(mod, "ANAH_DIR"):
                p = patch.object(mod, "ANAH_DIR", anah_dir)
                p.start()
                patches.append(p)
                # Also patch derived paths
                for attr in ("DB_FILE", "STATE_FILE", "MEMORY_FILE", "PROFILE_FILE",
                             "SKILLS_DIR", "LEARNING_LOG", "TRAJECTORIES_DIR"):
                    if hasattr(mod, attr):
                        original = getattr(mod, attr)
                        new_path = anah_dir / original.name
                        p2 = patch.object(mod, attr, new_path)
                        p2.start()
                        patches.append(p2)
        except Exception:
            pass
    yield anah_dir
    for p in patches:
        p.stop()


@pytest.fixture
def sample_brainstem_results():
    """Sample brainstem output for testing downstream consumers."""
    return {
        "results": [
            {"name": "network_connectivity", "level": 1, "passed": True,
             "duration_ms": 50.0, "message": "DNS and network reachable", "details": None},
            {"name": "filesystem_access", "level": 1, "passed": True,
             "duration_ms": 1.0, "message": "Read/write OK", "details": None},
            {"name": "compute_resources", "level": 1, "passed": True,
             "duration_ms": 500.0, "message": "CPU 5%, RAM 35%, Disk 37%",
             "details": {"cpu": 5.0, "ram": 35.0, "disk": 37.0}},
            {"name": "wifi_interface", "level": 1, "passed": True,
             "duration_ms": 10.0, "message": "Active interfaces: Wi-Fi", "details": None},
            {"name": "config_integrity", "level": 2, "passed": True,
             "duration_ms": 1.0, "message": "Config integrity OK",
             "details": {"checksum": "abc123"}},
            {"name": "db_integrity", "level": 2, "passed": True,
             "duration_ms": 2.0, "message": "Database integrity OK", "details": None},
            {"name": "backup_recency", "level": 2, "passed": True,
             "duration_ms": 0.5, "message": "Backup age: 60s", "details": None},
            {"name": "anthropic_api", "level": 3, "passed": True,
             "duration_ms": 200.0, "message": "Anthropic API responded: 401", "details": None},
        ],
        "gating": {"l1_healthy": True},
        "summary": {"total": 8, "passed": 8, "failed": 0, "health_score": 100.0},
    }


@pytest.fixture
def sample_brainstem_l1_failure():
    """Brainstem output where L1 network check fails."""
    return {
        "results": [
            {"name": "network_connectivity", "level": 1, "passed": False,
             "duration_ms": 5000.0, "message": "DNS resolution failed", "details": None},
            {"name": "filesystem_access", "level": 1, "passed": True,
             "duration_ms": 1.0, "message": "Read/write OK", "details": None},
            {"name": "compute_resources", "level": 1, "passed": True,
             "duration_ms": 500.0, "message": "CPU 5%, RAM 35%, Disk 37%",
             "details": {"cpu": 5.0, "ram": 35.0, "disk": 37.0}},
            {"name": "wifi_interface", "level": 1, "passed": False,
             "duration_ms": 10.0, "message": "No active interfaces", "details": None},
        ],
        "gating": {"l1_healthy": False},
        "summary": {"total": 4, "passed": 2, "failed": 2, "health_score": 50.0},
    }

#!/usr/bin/env python3
"""ANAH Cerebellum — L4 performance monitoring and pattern analysis.

Reads brainstem state + task history, detects patterns, produces context
summaries for the cortex to consume during goal generation.
"""

import json
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path

ANAH_DIR = Path.home() / ".anah"
STATE_FILE = ANAH_DIR / "state.json"
DB_FILE = ANAH_DIR / "anah.db"

# ---------------------------------------------------------------------------
# Database schema (auto-created if missing)
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS health_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL NOT NULL,
    level       INTEGER NOT NULL,
    check_name  TEXT NOT NULL,
    passed      INTEGER NOT NULL,
    duration_ms REAL,
    message     TEXT,
    details     TEXT
);

CREATE TABLE IF NOT EXISTS task_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    started_at  REAL,
    completed_at REAL,
    result      TEXT
);

CREATE TABLE IF NOT EXISTS generated_goals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL NOT NULL,
    title       TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    reasoning   TEXT,
    source      TEXT NOT NULL,
    task_id     INTEGER,
    context     TEXT,
    status      TEXT NOT NULL DEFAULT 'proposed'
);

CREATE TABLE IF NOT EXISTS agent_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL NOT NULL,
    level       INTEGER,
    action_type TEXT NOT NULL,
    description TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'started',
    duration_ms REAL,
    details     TEXT
);

CREATE INDEX IF NOT EXISTS idx_health_logs_ts ON health_logs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_task_queue_status ON task_queue(status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_generated_goals_ts ON generated_goals(timestamp DESC);
"""


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_FILE))
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


# ---------------------------------------------------------------------------
# Pattern detectors
# ---------------------------------------------------------------------------
@dataclass
class Pattern:
    title: str
    category: str  # recurring_failure, performance_trend, idle_opportunity, maintenance, anomaly
    severity: str  # critical, warning, info
    description: str
    suggested_action: str | None = None


def detect_recurring_failures(db: sqlite3.Connection) -> list[Pattern]:
    """Find checks that failed multiple times in the last hour."""
    patterns = []
    cutoff = time.time() - 3600
    cursor = db.execute(
        """SELECT check_name, level, COUNT(*) as fail_count
           FROM health_logs WHERE passed = 0 AND timestamp > ?
           GROUP BY check_name HAVING fail_count >= 3
           ORDER BY fail_count DESC""",
        (cutoff,),
    )
    for row in cursor:
        severity = "critical" if row["fail_count"] >= 5 else "warning"
        patterns.append(Pattern(
            title=f"Recurring failure: {row['check_name']}",
            category="recurring_failure",
            severity=severity,
            description=f"L{row['level']} check '{row['check_name']}' failed {row['fail_count']} times in the last hour",
            suggested_action=f"self_diagnostic: investigate {row['check_name']} recurring failures",
        ))
    return patterns


def detect_performance_trends(db: sqlite3.Connection) -> list[Pattern]:
    """Detect degrading task performance over recent windows."""
    patterns = []
    now = time.time()

    # Compare last-hour completion rate vs previous hour
    for window_name, start, end in [
        ("last_hour", now - 3600, now),
        ("prev_hour", now - 7200, now - 3600),
    ]:
        cursor = db.execute(
            "SELECT COUNT(*) as total, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed "
            "FROM task_queue WHERE completed_at BETWEEN ? AND ?",
            (start, end),
        )
        row = cursor.fetchone()
        if window_name == "last_hour":
            recent_rate = (row["completed"] / row["total"] * 100) if row["total"] > 0 else 100
            recent_total = row["total"]
        else:
            prev_rate = (row["completed"] / row["total"] * 100) if row["total"] > 0 else 100

    if recent_total > 0 and recent_rate < prev_rate - 10:
        patterns.append(Pattern(
            title="Declining task completion rate",
            category="performance_trend",
            severity="warning",
            description=f"Completion rate dropped from {prev_rate:.0f}% to {recent_rate:.0f}% in the last hour",
            suggested_action="health_report: performance degradation analysis",
        ))
    return patterns


def detect_idle_opportunity(db: sqlite3.Connection, state: dict) -> list[Pattern]:
    """Detect when the system is healthy and idle — good time for self-improvement."""
    patterns = []
    cursor = db.execute(
        "SELECT COUNT(*) as cnt FROM task_queue WHERE status IN ('queued', 'running')"
    )
    active = cursor.fetchone()["cnt"]

    health_score = 0
    gating = state.get("gating", {})
    if gating.get("l1_healthy", False):
        levels = state.get("levels", {})
        healthy_count = sum(1 for l in levels.values() if isinstance(l, dict) and l.get("status") == "healthy")
        total = len(levels) or 1
        health_score = healthy_count / total * 100

    if active == 0 and health_score >= 80:
        patterns.append(Pattern(
            title="System idle and healthy",
            category="idle_opportunity",
            severity="info",
            description=f"Queue empty, health score {health_score:.0f}% — good time for goal generation",
        ))
    return patterns


def detect_maintenance_needs(db: sqlite3.Connection) -> list[Pattern]:
    """Check for data that needs cleanup."""
    patterns = []
    cutoff_7d = time.time() - 86400 * 7

    cursor = db.execute("SELECT COUNT(*) as cnt FROM health_logs WHERE timestamp < ?", (cutoff_7d,))
    old_logs = cursor.fetchone()["cnt"]
    if old_logs > 1000:
        patterns.append(Pattern(
            title="Old health logs accumulating",
            category="maintenance",
            severity="info",
            description=f"{old_logs} health logs older than 7 days",
            suggested_action="cleanup: purge old health logs and task history",
        ))
    return patterns


def detect_check_anomalies(db: sqlite3.Connection) -> list[Pattern]:
    """Detect unusual check durations."""
    patterns = []
    cursor = db.execute(
        """SELECT check_name, AVG(duration_ms) as avg_ms, MAX(duration_ms) as max_ms,
                  COUNT(*) as cnt
           FROM health_logs WHERE timestamp > ? GROUP BY check_name""",
        (time.time() - 3600,),
    )
    for row in cursor:
        if row["cnt"] >= 3 and row["max_ms"] > row["avg_ms"] * 3 and row["avg_ms"] > 100:
            patterns.append(Pattern(
                title=f"Anomalous duration: {row['check_name']}",
                category="anomaly",
                severity="warning",
                description=f"Check '{row['check_name']}' max {row['max_ms']:.0f}ms vs avg {row['avg_ms']:.0f}ms",
            ))
    return patterns


# ---------------------------------------------------------------------------
# Context summary for cortex consumption
# ---------------------------------------------------------------------------
def build_context(db: sqlite3.Connection, state: dict) -> dict:
    """Build full system context for LLM consumption by the cortex."""
    # Task stats
    cursor = db.execute(
        """SELECT status, COUNT(*) as cnt FROM task_queue GROUP BY status"""
    )
    queue = {"queued": 0, "running": 0, "completed": 0, "failed": 0, "pending_approval": 0}
    for row in cursor:
        queue[row["status"]] = row["cnt"]

    # Health score from state
    levels = state.get("levels", {})
    healthy = sum(1 for l in levels.values() if isinstance(l, dict) and l.get("status") == "healthy")
    total = max(len(levels), 1)
    health_score = round(healthy / total * 100, 1)

    # Recent failures
    cursor = db.execute(
        "SELECT check_name, message FROM health_logs WHERE passed = 0 ORDER BY timestamp DESC LIMIT 5"
    )
    recent_failures = [{"check": r["check_name"], "message": r["message"]} for r in cursor]

    return {
        "health_score": health_score,
        "active_levels": total,
        "levels_healthy": healthy,
        "queue": queue,
        "recent_failures": recent_failures,
        "gating": state.get("gating", {}),
        "timestamp": time.time(),
    }


def analyze(db: sqlite3.Connection, state: dict) -> list[Pattern]:
    """Run all pattern detectors."""
    patterns = []
    patterns.extend(detect_recurring_failures(db))
    patterns.extend(detect_performance_trends(db))
    patterns.extend(detect_idle_opportunity(db, state))
    patterns.extend(detect_maintenance_needs(db))
    patterns.extend(detect_check_anomalies(db))
    return patterns


# ---------------------------------------------------------------------------
# Log brainstem results to DB
# ---------------------------------------------------------------------------
def ingest_brainstem_results(db: sqlite3.Connection, results: list[dict]):
    """Store brainstem check results in the health_logs table."""
    now = time.time()
    for r in results:
        db.execute(
            "INSERT INTO health_logs (timestamp, level, check_name, passed, duration_ms, message, details) VALUES (?,?,?,?,?,?,?)",
            (now, r["level"], r["name"], int(r["passed"]), r["duration_ms"], r["message"],
             json.dumps(r.get("details")) if r.get("details") else None),
        )
    db.commit()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ANAH Cerebellum — L4 performance & patterns")
    parser.add_argument("--metrics", action="store_true", help="Show task metrics context")
    parser.add_argument("--patterns", action="store_true", help="Run pattern detection")
    parser.add_argument("--all", "-a", action="store_true", help="Full context + patterns")
    parser.add_argument("--ingest", type=str, help="Ingest brainstem JSON results from file or stdin")
    args = parser.parse_args()

    ANAH_DIR.mkdir(exist_ok=True)
    db = get_db()
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"levels": {}, "gating": {}}

    if args.ingest:
        if args.ingest == "-":
            data = json.load(sys.stdin)
        else:
            data = json.loads(Path(args.ingest).read_text())
        ingest_brainstem_results(db, data.get("results", []))
        print(json.dumps({"ingested": len(data.get("results", []))}))
    elif args.patterns or args.all:
        patterns = analyze(db, state)
        context = build_context(db, state) if args.all else {}
        output = {
            "patterns": [asdict(p) for p in patterns],
            "context": context,
        }
        print(json.dumps(output, indent=2))
    elif args.metrics:
        context = build_context(db, state)
        print(json.dumps(context, indent=2))
    else:
        context = build_context(db, state)
        print(json.dumps(context, indent=2))

    db.close()

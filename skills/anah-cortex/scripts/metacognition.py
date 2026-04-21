#!/usr/bin/env python3
"""ANAH Metacognition — L5 self-awareness and strategic reasoning.

Runs after hippocampus in each cycle to analyze ANAH's own performance,
detect patterns in failures, and write strategy recommendations that
the cortex analysis phase reads.

Capabilities:
- Trend analysis: health degradation over hours/days
- Failure chain tracing: goal → task → result → root cause
- Handler effectiveness scoring: which handlers are worth using
- Strategy journal: persistent recommendations for cortex
"""

import json
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path

ANAH_DIR = Path.home() / ".anah"
DB_FILE = ANAH_DIR / "anah.db"
STRATEGY_FILE = ANAH_DIR / "strategy.json"
MAX_STRATEGY_ENTRIES = 50


@dataclass
class StrategyEntry:
    timestamp: float
    category: str  # "avoid", "leverage", "insight", "capability_gap"
    title: str
    evidence: str
    confidence: float  # 0.0–1.0


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_FILE))
    db.row_factory = sqlite3.Row
    return db


# ---------------------------------------------------------------------------
# Trend analysis
# ---------------------------------------------------------------------------
def analyze_health_trends(db: sqlite3.Connection, hours: int = 24) -> list[dict]:
    """Detect health degradation trends over time windows."""
    cutoff = time.time() - (hours * 3600)
    midpoint = time.time() - (hours * 1800)  # halfway

    # Compare first half vs second half pass rates
    first_half = db.execute(
        "SELECT COUNT(*) as total, SUM(CASE WHEN passed = 1 THEN 1 ELSE 0 END) as passed "
        "FROM health_logs WHERE timestamp > ? AND timestamp <= ?",
        (cutoff, midpoint),
    ).fetchone()

    second_half = db.execute(
        "SELECT COUNT(*) as total, SUM(CASE WHEN passed = 1 THEN 1 ELSE 0 END) as passed "
        "FROM health_logs WHERE timestamp > ?",
        (midpoint,),
    ).fetchone()

    trends = []

    if first_half["total"] > 0 and second_half["total"] > 0:
        rate_first = first_half["passed"] / first_half["total"] * 100
        rate_second = second_half["passed"] / second_half["total"] * 100
        delta = rate_second - rate_first

        if delta < -10:
            trends.append({
                "type": "degradation",
                "message": f"Health declining: {rate_first:.0f}% → {rate_second:.0f}% ({delta:+.0f}%)",
                "severity": "high" if delta < -20 else "medium",
                "first_half_rate": round(rate_first, 1),
                "second_half_rate": round(rate_second, 1),
            })
        elif delta > 10:
            trends.append({
                "type": "improvement",
                "message": f"Health improving: {rate_first:.0f}% → {rate_second:.0f}% ({delta:+.0f}%)",
                "severity": "info",
                "first_half_rate": round(rate_first, 1),
                "second_half_rate": round(rate_second, 1),
            })

    # Check-level degradation: which specific checks are failing more?
    failing_checks = db.execute(
        """SELECT check_name, COUNT(*) as total,
           SUM(CASE WHEN passed = 0 THEN 1 ELSE 0 END) as failures
           FROM health_logs WHERE timestamp > ?
           GROUP BY check_name HAVING failures > 0
           ORDER BY failures DESC LIMIT 5""",
        (cutoff,),
    ).fetchall()

    for row in failing_checks:
        r = dict(row)
        fail_rate = r["failures"] / r["total"] * 100
        if fail_rate > 30:
            trends.append({
                "type": "recurring_failure",
                "check_name": r["check_name"],
                "message": f"{r['check_name']} failing {fail_rate:.0f}% of the time ({r['failures']}/{r['total']})",
                "severity": "high" if fail_rate > 60 else "medium",
                "fail_rate": round(fail_rate, 1),
            })

    return trends


# ---------------------------------------------------------------------------
# Failure chain tracing
# ---------------------------------------------------------------------------
def trace_failure_chains(db: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Trace failed tasks back to their generating goals to find root causes."""
    failed = db.execute(
        """SELECT t.id as task_id, t.title, t.result, t.completed_at,
           g.id as goal_id, g.reasoning, g.source, g.chain_id
           FROM task_queue t
           LEFT JOIN generated_goals g ON g.task_id = t.id
           WHERE t.status = 'failed' AND t.completed_at > ?
           ORDER BY t.completed_at DESC LIMIT ?""",
        (time.time() - 86400, limit),
    ).fetchall()

    chains = []
    for row in failed:
        r = dict(row)
        error = ""
        try:
            result = json.loads(r.get("result") or "{}")
            error = result.get("error", str(result))
        except (json.JSONDecodeError, TypeError):
            error = str(r.get("result", ""))

        chains.append({
            "task_id": r["task_id"],
            "task_title": r["title"],
            "error": error[:200],
            "goal_id": r.get("goal_id"),
            "goal_reasoning": (r.get("reasoning") or "")[:200],
            "source": r.get("source", "unknown"),
            "chain_id": r.get("chain_id"),
        })

    return chains


# ---------------------------------------------------------------------------
# Handler effectiveness
# ---------------------------------------------------------------------------
def analyze_handler_effectiveness(db: sqlite3.Connection) -> list[dict]:
    """Score handler types by success rate, avg duration, and volume."""
    rows = db.execute(
        """SELECT
           CASE
             WHEN title LIKE 'health_report:%' THEN 'health_report'
             WHEN title LIKE 'self_diagnostic:%' THEN 'self_diagnostic'
             WHEN title LIKE 'cleanup:%' THEN 'cleanup'
             WHEN title LIKE 'echo:%' THEN 'echo'
             WHEN title LIKE 'mcp:%' THEN 'mcp_tool'
             WHEN title LIKE 'notify:%' THEN 'notify'
             ELSE 'other'
           END as handler,
           COUNT(*) as total,
           SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
           SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
         FROM task_queue
         WHERE created_at > ?
         GROUP BY handler
         ORDER BY total DESC""",
        (time.time() - 7 * 86400,),
    ).fetchall()

    handlers = []
    for row in rows:
        r = dict(row)
        total = r["total"]
        success_rate = r["completed"] / total * 100 if total > 0 else 0
        handlers.append({
            "handler": r["handler"],
            "total": total,
            "completed": r["completed"],
            "failed": r["failed"],
            "success_rate": round(success_rate, 1),
        })

    return handlers


# ---------------------------------------------------------------------------
# Capability gap detection
# ---------------------------------------------------------------------------
def detect_capability_gaps(db: sqlite3.Connection) -> list[dict]:
    """Identify tasks that consistently fail or get routed to ollama (generic handler)."""
    gaps = []

    # Tasks that fall to 'other'/ollama handler with low success
    ollama_failures = db.execute(
        """SELECT title, result FROM task_queue
           WHERE status = 'failed' AND created_at > ?
           AND title NOT LIKE 'health_report:%'
           AND title NOT LIKE 'self_diagnostic:%'
           AND title NOT LIKE 'cleanup:%'
           AND title NOT LIKE 'echo:%'
           AND title NOT LIKE 'mcp:%'
           AND title NOT LIKE 'notify:%'
           ORDER BY completed_at DESC LIMIT 10""",
        (time.time() - 7 * 86400,),
    ).fetchall()

    # Group by similar title prefixes
    prefix_failures: dict[str, int] = {}
    for row in ollama_failures:
        title = dict(row)["title"]
        prefix = title.split(":")[0].strip().lower() if ":" in title else title.split()[0].lower()
        prefix_failures[prefix] = prefix_failures.get(prefix, 0) + 1

    for prefix, count in prefix_failures.items():
        if count >= 2:
            gaps.append({
                "type": "capability_gap",
                "prefix": prefix,
                "failures": count,
                "message": f"'{prefix}' tasks fail repeatedly ({count}x) — may need a dedicated handler",
            })

    # Suppressed notifications (hallucinated alerts)
    suppressed = db.execute(
        "SELECT COUNT(*) FROM task_queue WHERE status = 'completed' AND result LIKE '%suppressed%true%' AND completed_at > ?",
        (time.time() - 7 * 86400,),
    ).fetchone()[0]
    if suppressed > 5:
        gaps.append({
            "type": "hallucination",
            "message": f"{suppressed} hallucinated alerts suppressed this week — LLM needs better grounding",
            "count": suppressed,
        })

    return gaps


# ---------------------------------------------------------------------------
# Strategy journal
# ---------------------------------------------------------------------------
def load_strategy() -> list[dict]:
    """Load existing strategy entries."""
    if STRATEGY_FILE.exists():
        try:
            return json.loads(STRATEGY_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_strategy(entries: list[dict]):
    """Save strategy entries (capped at MAX_STRATEGY_ENTRIES)."""
    ANAH_DIR.mkdir(parents=True, exist_ok=True)
    # Keep only the most recent entries
    entries = entries[-MAX_STRATEGY_ENTRIES:]
    STRATEGY_FILE.write_text(json.dumps(entries, indent=2, default=str))


def generate_strategy_entries(
    trends: list[dict],
    failures: list[dict],
    handlers: list[dict],
    gaps: list[dict],
) -> list[StrategyEntry]:
    """Synthesize analysis into actionable strategy entries."""
    entries = []
    now = time.time()

    # From trends
    for t in trends:
        if t["type"] == "degradation":
            entries.append(StrategyEntry(
                timestamp=now, category="insight",
                title=f"Health degrading: {t['message']}",
                evidence=f"Pass rate dropped from {t.get('first_half_rate')}% to {t.get('second_half_rate')}%",
                confidence=0.8,
            ))
        elif t["type"] == "recurring_failure":
            entries.append(StrategyEntry(
                timestamp=now, category="avoid",
                title=f"Recurring failure: {t['check_name']}",
                evidence=t["message"],
                confidence=0.7 if t.get("fail_rate", 0) > 50 else 0.5,
            ))

    # From handler effectiveness
    for h in handlers:
        if h["success_rate"] < 30 and h["total"] >= 5:
            entries.append(StrategyEntry(
                timestamp=now, category="avoid",
                title=f"Stop generating '{h['handler']}' tasks — {h['success_rate']}% success",
                evidence=f"{h['failed']}/{h['total']} failed in last 7 days",
                confidence=0.9,
            ))
        elif h["success_rate"] >= 90 and h["total"] >= 5:
            entries.append(StrategyEntry(
                timestamp=now, category="leverage",
                title=f"'{h['handler']}' is highly effective — {h['success_rate']}% success",
                evidence=f"{h['completed']}/{h['total']} succeeded in last 7 days",
                confidence=0.8,
            ))

    # From capability gaps
    for g in gaps:
        entries.append(StrategyEntry(
            timestamp=now, category="capability_gap",
            title=g["message"],
            evidence=f"Detected via {g['type']} analysis",
            confidence=0.6,
        ))

    # From failure chains — find common error patterns
    error_counts: dict[str, int] = {}
    for f in failures:
        error = f.get("error", "")[:50]
        error_counts[error] = error_counts.get(error, 0) + 1
    for error, count in error_counts.items():
        if count >= 3:
            entries.append(StrategyEntry(
                timestamp=now, category="avoid",
                title=f"Repeated failure pattern: {error}",
                evidence=f"Seen {count} times in last 24h",
                confidence=0.7,
            ))

    return entries


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------
def run_metacognition() -> dict:
    """Full metacognition cycle. Called after hippocampus in orchestrator."""
    db = get_db()

    trends = analyze_health_trends(db)
    failures = trace_failure_chains(db)
    handlers = analyze_handler_effectiveness(db)
    gaps = detect_capability_gaps(db)

    db.close()

    # Generate strategy entries
    new_entries = generate_strategy_entries(trends, failures, handlers, gaps)

    # Merge with existing strategy (deduplicate by title)
    existing = load_strategy()
    existing_titles = {e.get("title") for e in existing}
    merged = existing.copy()
    added = 0
    for entry in new_entries:
        if entry.title not in existing_titles:
            merged.append(asdict(entry))
            existing_titles.add(entry.title)
            added += 1

    save_strategy(merged)

    return {
        "trends": len(trends),
        "failure_chains": len(failures),
        "handler_scores": len(handlers),
        "capability_gaps": len(gaps),
        "new_strategies": added,
        "total_strategies": len(merged),
        "details": {
            "trends": trends,
            "gaps": gaps,
            "top_failures": failures[:5],
            "weak_handlers": [h for h in handlers if h["success_rate"] < 50],
        },
    }


def get_strategy_for_cortex() -> str:
    """Format strategy entries for injection into cortex generation prompt."""
    entries = load_strategy()
    if not entries:
        return "No strategic insights yet."

    # Sort by confidence, take top 10 recent
    recent = sorted(entries, key=lambda e: (e.get("confidence", 0), e.get("timestamp", 0)), reverse=True)[:10]

    lines = []
    for e in recent:
        cat = e.get("category", "insight").upper()
        lines.append(f"- [{cat}] {e.get('title', '')} (confidence: {e.get('confidence', 0):.0%})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ANAH Metacognition — self-awareness engine")
    parser.add_argument("--run", "-r", action="store_true", help="Run metacognition cycle")
    parser.add_argument("--strategy", "-s", action="store_true", help="Show current strategy")
    parser.add_argument("--trends", action="store_true", help="Show health trends")
    parser.add_argument("--failures", action="store_true", help="Show failure chains")
    parser.add_argument("--gaps", action="store_true", help="Show capability gaps")
    args = parser.parse_args()

    if args.run:
        result = run_metacognition()
        print(json.dumps(result, indent=2, default=str))
    elif args.strategy:
        entries = load_strategy()
        print(json.dumps(entries, indent=2, default=str))
    elif args.trends:
        db = get_db()
        print(json.dumps(analyze_health_trends(db), indent=2))
        db.close()
    elif args.failures:
        db = get_db()
        print(json.dumps(trace_failure_chains(db), indent=2, default=str))
        db.close()
    elif args.gaps:
        db = get_db()
        print(json.dumps(detect_capability_gaps(db), indent=2))
        db.close()
    else:
        parser.print_help()

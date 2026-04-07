#!/usr/bin/env python3
"""ANAH Executor — Motor cortex. Dequeues and executes tasks.

Routes tasks to built-in handlers or Ollama for general-purpose execution.
Records results back to the database, closing the autonomous loop.
"""

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ANAH_DIR = Path.home() / ".anah"
DB_FILE = ANAH_DIR / "anah.db"

# Sibling skill scripts
SKILLS_DIR = Path(__file__).resolve().parent.parent.parent
BRAINSTEM_DIR = SKILLS_DIR / "anah-brainstem" / "scripts"
CEREBELLUM_DIR = SKILLS_DIR / "anah-cerebellum" / "scripts"
MEMORY_DIR = SKILLS_DIR / "anah-memory" / "scripts"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")

MAX_TASK_TIMEOUT = 120  # seconds


@dataclass
class TaskResult:
    success: bool
    result: dict
    duration_ms: float


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_FILE))
    db.row_factory = sqlite3.Row
    return db


def dequeue_task(db: sqlite3.Connection) -> dict | None:
    """Atomically dequeue the highest-priority queued task."""
    row = db.execute(
        "SELECT * FROM task_queue WHERE status = 'queued' ORDER BY priority DESC, created_at ASC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    task = dict(row)
    db.execute(
        "UPDATE task_queue SET status = 'running', started_at = ? WHERE id = ?",
        (time.time(), task["id"]),
    )
    db.commit()
    return task


def complete_task(db: sqlite3.Connection, task_id: int, result: dict):
    """Mark a task as completed with its result."""
    db.execute(
        "UPDATE task_queue SET status = 'completed', completed_at = ?, result = ? WHERE id = ?",
        (time.time(), json.dumps(result), task_id),
    )
    db.commit()


def fail_task(db: sqlite3.Connection, task_id: int, error: str):
    """Mark a task as failed."""
    db.execute(
        "UPDATE task_queue SET status = 'failed', completed_at = ?, result = ? WHERE id = ?",
        (time.time(), json.dumps({"error": error}), task_id),
    )
    db.commit()


def queue_status(db: sqlite3.Connection) -> dict:
    """Get current queue statistics."""
    counts = {}
    for row in db.execute("SELECT status, COUNT(*) as cnt FROM task_queue GROUP BY status"):
        counts[row["status"]] = row["cnt"]
    next_task = db.execute(
        "SELECT id, title, priority FROM task_queue WHERE status = 'queued' ORDER BY priority DESC, created_at ASC LIMIT 1"
    ).fetchone()
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "next": dict(next_task) if next_task else None,
    }


# ---------------------------------------------------------------------------
# Handler routing
# ---------------------------------------------------------------------------
def route_task(task: dict) -> str:
    """Determine which handler should process this task."""
    title = task["title"].lower()

    if title.startswith("health_report:") or title.startswith("system health report"):
        return "health_report"
    if title.startswith("self_diagnostic:") or "diagnostic" in title:
        return "self_diagnostic"
    if title.startswith("cleanup:") or "cleanup" in title or "prune" in title:
        return "cleanup"
    if title.startswith("echo:"):
        return "echo"

    # General-purpose tasks go to Ollama
    return "ollama"


# ---------------------------------------------------------------------------
# Built-in handlers
# ---------------------------------------------------------------------------
def handle_echo(task: dict) -> TaskResult:
    """Simple echo handler for testing."""
    t0 = time.time()
    return TaskResult(
        success=True,
        result={"echo": task.get("description", task["title"]), "handler": "echo"},
        duration_ms=(time.time() - t0) * 1000,
    )


def handle_health_report(task: dict) -> TaskResult:
    """Run brainstem + cerebellum and produce a health report."""
    import asyncio
    t0 = time.time()
    try:
        sys.path.insert(0, str(BRAINSTEM_DIR))
        sys.path.insert(0, str(CEREBELLUM_DIR))
        import brainstem
        import cerebellum

        # Run brainstem
        bs_result = asyncio.run(brainstem.run_checks())

        # Ingest into cerebellum
        db = cerebellum.get_db()
        cerebellum.ingest_brainstem_results(db, bs_result["results"])
        state_file = ANAH_DIR / "state.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else {"levels": {}, "gating": {}}
        context = cerebellum.build_context(db, state)
        patterns = cerebellum.analyze(db, state)
        db.close()

        report = {
            "handler": "health_report",
            "health_score": bs_result["summary"]["health_score"],
            "checks_passed": bs_result["summary"]["passed"],
            "checks_failed": bs_result["summary"]["failed"],
            "l1_healthy": bs_result["gating"]["l1_healthy"],
            "patterns_detected": len(patterns),
            "pattern_summaries": [
                {"title": p.title, "category": p.category, "severity": p.severity}
                for p in patterns[:5]
            ],
            "queue": context.get("queue", {}),
        }
        return TaskResult(True, report, (time.time() - t0) * 1000)
    except Exception as e:
        return TaskResult(False, {"error": str(e), "handler": "health_report"}, (time.time() - t0) * 1000)


def handle_self_diagnostic(task: dict) -> TaskResult:
    """Full system diagnostic — checks all organelles and reports status."""
    import asyncio
    t0 = time.time()
    try:
        sys.path.insert(0, str(BRAINSTEM_DIR))
        sys.path.insert(0, str(MEMORY_DIR))
        import brainstem
        import memory

        bs_result = asyncio.run(brainstem.run_checks())
        mem_status = memory.memory_status()

        diagnostic = {
            "handler": "self_diagnostic",
            "brainstem": {
                "health_score": bs_result["summary"]["health_score"],
                "l1_healthy": bs_result["gating"]["l1_healthy"],
                "checks": bs_result["summary"],
            },
            "memory": mem_status,
            "recommendations": [],
        }

        # Generate recommendations
        if bs_result["summary"]["failed"] > 0:
            failed = [r for r in bs_result["results"] if not r["passed"]]
            for f in failed:
                diagnostic["recommendations"].append(
                    f"Investigate failing check: {f['name']} ({f['message']})")

        mem_util = mem_status["memory"]["chars"] / mem_status["memory"]["limit"]
        if mem_util > 0.8:
            diagnostic["recommendations"].append(
                f"Memory at {mem_util:.0%} — consolidation recommended")

        if not diagnostic["recommendations"]:
            diagnostic["recommendations"].append("All systems nominal. No action needed.")

        return TaskResult(True, diagnostic, (time.time() - t0) * 1000)
    except Exception as e:
        return TaskResult(False, {"error": str(e), "handler": "self_diagnostic"}, (time.time() - t0) * 1000)


def handle_cleanup(task: dict) -> TaskResult:
    """Prune old data — logs, trajectories, backups."""
    t0 = time.time()
    try:
        sys.path.insert(0, str(MEMORY_DIR))
        import memory

        cleaned = {"handler": "cleanup"}

        # Prune trajectories
        traj_result = memory.prune_trajectories(keep=100)
        cleaned["trajectories_pruned"] = traj_result.get("pruned", 0)

        # Prune old health logs (keep last 7 days)
        db = get_db()
        cutoff = time.time() - (7 * 86400)
        cursor = db.execute("DELETE FROM health_logs WHERE timestamp < ?", (cutoff,))
        cleaned["health_logs_pruned"] = cursor.rowcount
        db.commit()

        # Prune old completed/failed tasks (keep last 30 days)
        cutoff_tasks = time.time() - (30 * 86400)
        cursor = db.execute(
            "DELETE FROM task_queue WHERE status IN ('completed', 'failed') AND completed_at < ?",
            (cutoff_tasks,))
        cleaned["tasks_pruned"] = cursor.rowcount
        db.commit()

        # Prune old backups (keep last 10)
        backup_dir = ANAH_DIR / "backups"
        if backup_dir.exists():
            backups = sorted(backup_dir.glob("*.db"), key=lambda f: f.stat().st_mtime, reverse=True)
            pruned_backups = 0
            for b in backups[10:]:
                b.unlink()
                pruned_backups += 1
            cleaned["backups_pruned"] = pruned_backups

        db.close()
        return TaskResult(True, cleaned, (time.time() - t0) * 1000)
    except Exception as e:
        return TaskResult(False, {"error": str(e), "handler": "cleanup"}, (time.time() - t0) * 1000)


def handle_ollama(task: dict) -> TaskResult:
    """Send task to Ollama for general-purpose execution."""
    t0 = time.time()
    import urllib.request

    prompt = f"""You are ANAH's task executor. Complete the following task and return a JSON result.

Task: {task['title']}
{f"Details: {task['description']}" if task.get('description') else ""}

Respond with a JSON object containing:
- "status": "completed" or "needs_followup"
- "summary": brief description of what was done
- "findings": array of key findings or actions taken
- "recommendations": array of suggested follow-up actions (if any)

JSON only, no markdown fences."""

    try:
        body = json.dumps({
            "model": OLLAMA_MODEL,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=body,
            headers={"content-type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=MAX_TASK_TIMEOUT)
        data = json.loads(resp.read())
        content = data["message"]["content"]

        # Try to parse as JSON
        try:
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
            result = json.loads(content.strip())
        except (json.JSONDecodeError, IndexError):
            result = {"summary": content.strip(), "handler": "ollama", "parsed": False}

        result["handler"] = "ollama"
        result["model"] = OLLAMA_MODEL
        return TaskResult(True, result, (time.time() - t0) * 1000)
    except Exception as e:
        return TaskResult(False, {"error": str(e), "handler": "ollama"}, (time.time() - t0) * 1000)


# Handler registry
HANDLERS = {
    "echo": handle_echo,
    "health_report": handle_health_report,
    "self_diagnostic": handle_self_diagnostic,
    "cleanup": handle_cleanup,
    "ollama": handle_ollama,
}


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def execute_next(db: sqlite3.Connection) -> dict | None:
    """Dequeue and execute the next task."""
    task = dequeue_task(db)
    if not task:
        return None

    handler_name = route_task(task)
    handler = HANDLERS.get(handler_name, handle_ollama)

    print(f"[executor] Task #{task['id']}: {task['title']} → {handler_name}", file=sys.stderr)

    try:
        result = handler(task)
        if result.success:
            complete_task(db, task["id"], result.result)
            print(f"[executor] Task #{task['id']} completed ({result.duration_ms:.0f}ms)", file=sys.stderr)
        else:
            fail_task(db, task["id"], json.dumps(result.result))
            print(f"[executor] Task #{task['id']} failed ({result.duration_ms:.0f}ms)", file=sys.stderr)
    except Exception as e:
        fail_task(db, task["id"], str(e))
        print(f"[executor] Task #{task['id']} crashed: {e}", file=sys.stderr)
        result = TaskResult(False, {"error": str(e)}, 0)

    return {
        "task_id": task["id"],
        "title": task["title"],
        "handler": handler_name,
        "success": result.success,
        "duration_ms": result.duration_ms,
        "result": result.result,
    }


def run_queue(db: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Process up to `limit` tasks from the queue."""
    results = []
    for _ in range(limit):
        outcome = execute_next(db)
        if outcome is None:
            break  # Queue empty
        results.append(outcome)
    return results


def drain_queue(db: sqlite3.Connection) -> list[dict]:
    """Process all queued tasks until the queue is empty."""
    return run_queue(db, limit=1000)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    # Load .env
    env_file = ANAH_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

    parser = argparse.ArgumentParser(description="ANAH Executor — task processing")
    parser.add_argument("--next", "-n", action="store_true", help="Execute next queued task")
    parser.add_argument("--run", "-r", action="store_true", help="Process queued tasks")
    parser.add_argument("--drain", action="store_true", help="Process all queued tasks")
    parser.add_argument("--limit", type=int, default=10, help="Max tasks to process (with --run)")
    parser.add_argument("--status", "-s", action="store_true", help="Show queue status")
    args = parser.parse_args()

    ANAH_DIR.mkdir(exist_ok=True)
    db = get_db()

    if args.status:
        print(json.dumps(queue_status(db), indent=2))
    elif args.next:
        result = execute_next(db)
        if result:
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps({"message": "Queue empty"}))
    elif args.run:
        results = run_queue(db, args.limit)
        print(json.dumps({"processed": len(results), "results": results}, indent=2))
    elif args.drain:
        results = drain_queue(db)
        print(json.dumps({"drained": len(results), "results": results}, indent=2))
    else:
        parser.print_help()

    db.close()

#!/usr/bin/env python3
"""ANAH Memory — Bounded experience store and trajectory export.

Manages constrained memory files and exports task execution traces
in ShareGPT format for future RL training.
"""

import json
import sqlite3
import time
from pathlib import Path

ANAH_DIR = Path.home() / ".anah"
DB_FILE = ANAH_DIR / "anah.db"
MEMORY_FILE = ANAH_DIR / "MEMORY.md"
PROFILE_FILE = ANAH_DIR / "SYSTEM_PROFILE.md"
TRAJECTORIES_DIR = ANAH_DIR / "trajectories"

# Bounded memory limits (characters)
MEMORY_LIMIT = 2200
PROFILE_LIMIT = 1375


# ---------------------------------------------------------------------------
# Bounded memory
# ---------------------------------------------------------------------------
def read_memory() -> str:
    if MEMORY_FILE.exists():
        return MEMORY_FILE.read_text()
    return ""


def write_memory(content: str) -> dict:
    """Write memory, enforcing the character limit."""
    if len(content) > MEMORY_LIMIT:
        return {"error": f"Content exceeds limit ({len(content)} > {MEMORY_LIMIT}). Consolidate first."}
    MEMORY_FILE.write_text(content)
    return {"written": len(content), "remaining": MEMORY_LIMIT - len(content)}


def read_profile() -> str:
    if PROFILE_FILE.exists():
        return PROFILE_FILE.read_text()
    return ""


def write_profile(content: str) -> dict:
    if len(content) > PROFILE_LIMIT:
        return {"error": f"Content exceeds limit ({len(content)} > {PROFILE_LIMIT}). Consolidate first."}
    PROFILE_FILE.write_text(content)
    return {"written": len(content), "remaining": PROFILE_LIMIT - len(content)}


def memory_status() -> dict:
    mem = read_memory()
    prof = read_profile()
    return {
        "memory": {
            "chars": len(mem),
            "limit": MEMORY_LIMIT,
            "remaining": MEMORY_LIMIT - len(mem),
            "utilization": f"{len(mem)/MEMORY_LIMIT*100:.1f}%",
        },
        "profile": {
            "chars": len(prof),
            "limit": PROFILE_LIMIT,
            "remaining": PROFILE_LIMIT - len(prof),
            "utilization": f"{len(prof)/PROFILE_LIMIT*100:.1f}%",
        },
        "trajectories": {
            "count": len(list(TRAJECTORIES_DIR.glob("*.json"))) if TRAJECTORIES_DIR.exists() else 0,
        },
    }


def consolidate_memory() -> dict:
    """Summarize and compress memory to fit within limits.

    For now, this truncates to fit. In production, this would use
    an LLM to intelligently summarize, preserving the most important
    information and discarding redundant entries.
    """
    mem = read_memory()
    if len(mem) <= MEMORY_LIMIT:
        return {"status": "within_limits", "chars": len(mem)}

    # Simple consolidation: keep last N chars that fit
    # A real implementation would use LLM summarization
    lines = mem.strip().split("\n")
    consolidated = []
    total = 0
    for line in reversed(lines):
        if total + len(line) + 1 > MEMORY_LIMIT - 50:  # Leave 50 char buffer
            break
        consolidated.insert(0, line)
        total += len(line) + 1

    header = "# ANAH Memory (consolidated)\n\n"
    result = header + "\n".join(consolidated)
    write_memory(result)
    return {"status": "consolidated", "before": len(mem), "after": len(result)}


# ---------------------------------------------------------------------------
# Trajectory export
# ---------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_FILE))
    db.row_factory = sqlite3.Row
    return db


def export_task_trajectory(db: sqlite3.Connection, task_id: int) -> dict | None:
    """Export a single task as a ShareGPT-format trajectory."""
    cursor = db.execute("SELECT * FROM task_queue WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    if not row:
        return None

    task = dict(row)
    result = task.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            result = {"raw": result}

    # Get associated goal if l5_generated
    goal_context = None
    if task["source"] == "l5_generated":
        cursor = db.execute(
            "SELECT * FROM generated_goals WHERE task_id = ?", (task_id,)
        )
        goal_row = cursor.fetchone()
        if goal_row:
            goal = dict(goal_row)
            goal_context = goal.get("context")
            if isinstance(goal_context, str):
                try:
                    goal_context = json.loads(goal_context)
                except Exception:
                    goal_context = None

    # Build ShareGPT format
    conversations = []

    # System message: hierarchy context
    system_msg = "You are an autonomous task executor in the ANAH hierarchy system. "
    if goal_context:
        system_msg += f"System health: {goal_context.get('health_score', 'unknown')}%. "
        queue = goal_context.get("queue", {})
        system_msg += f"Queue: {queue.get('completed', 0)} completed, {queue.get('failed', 0)} failed."
    conversations.append({"from": "system", "value": system_msg})

    # Human message: the task
    human_msg = f"Task: {task['title']}"
    if task.get("description"):
        human_msg += f"\n\nDetails: {task['description']}"
    conversations.append({"from": "human", "value": human_msg})

    # Assistant message: the result
    if result:
        gpt_msg = json.dumps(result, indent=2) if isinstance(result, dict) else str(result)
    else:
        gpt_msg = f"Task completed (status: {task['status']})"
    conversations.append({"from": "gpt", "value": gpt_msg})

    duration = None
    if task.get("completed_at") and task.get("started_at"):
        duration = (task["completed_at"] - task["started_at"]) * 1000

    return {
        "conversations": conversations,
        "metadata": {
            "task_id": task_id,
            "title": task["title"],
            "source": task["source"],
            "outcome": task["status"],
            "priority": task["priority"],
            "duration_ms": duration,
            "timestamp": task.get("completed_at") or task["created_at"],
        },
    }


def export_trajectories(since_hours: float = 24, limit: int = 500) -> list[dict]:
    """Export recent task trajectories."""
    db = get_db()
    cutoff = time.time() - (since_hours * 3600)
    cursor = db.execute(
        "SELECT id FROM task_queue WHERE status = 'completed' AND completed_at > ? ORDER BY completed_at DESC LIMIT ?",
        (cutoff, limit),
    )
    trajectories = []
    for row in cursor:
        traj = export_task_trajectory(db, row["id"])
        if traj:
            trajectories.append(traj)
    db.close()
    return trajectories


def save_trajectories(trajectories: list[dict], filename: str | None = None):
    """Save trajectories to disk."""
    TRAJECTORIES_DIR.mkdir(parents=True, exist_ok=True)
    if not filename:
        filename = f"trajectories_{int(time.time())}.json"
    # Sanitize filename — strip path components to prevent traversal
    safe_name = Path(filename).name
    if not safe_name:
        safe_name = f"trajectories_{int(time.time())}.json"
    path = TRAJECTORIES_DIR / safe_name
    path.write_text(json.dumps(trajectories, indent=2))
    return str(path)


def prune_trajectories(keep: int = 1000):
    """Remove old trajectory files, keeping the most recent."""
    if not TRAJECTORIES_DIR.exists():
        return {"pruned": 0}
    files = sorted(TRAJECTORIES_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    pruned = 0
    for f in files[keep:]:
        f.unlink()
        pruned += 1
    return {"pruned": pruned, "remaining": len(files) - pruned}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ANAH Memory — bounded store & trajectory export")
    parser.add_argument("--status", "-s", action="store_true", help="Memory status")
    parser.add_argument("--consolidate", action="store_true", help="Consolidate memory to fit limits")
    parser.add_argument("--export", action="store_true", help="Export trajectories")
    parser.add_argument("--since", type=float, default=24, help="Hours of history to export")
    parser.add_argument("--format", type=str, default="sharegpt", choices=["sharegpt"], help="Export format")
    parser.add_argument("--prune", action="store_true", help="Prune old trajectory files")
    parser.add_argument("--keep", type=int, default=1000, help="Files to keep when pruning")
    args = parser.parse_args()

    ANAH_DIR.mkdir(exist_ok=True)

    if args.status:
        print(json.dumps(memory_status(), indent=2))
    elif args.consolidate:
        print(json.dumps(consolidate_memory(), indent=2))
    elif args.export:
        trajectories = export_trajectories(args.since)
        if trajectories:
            path = save_trajectories(trajectories)
            print(json.dumps({"exported": len(trajectories), "path": path}, indent=2))
        else:
            print(json.dumps({"exported": 0, "message": "No completed tasks in time window"}))
    elif args.prune:
        print(json.dumps(prune_trajectories(args.keep), indent=2))
    else:
        print(json.dumps(memory_status(), indent=2))

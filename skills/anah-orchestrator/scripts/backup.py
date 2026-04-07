#!/usr/bin/env python3
"""ANAH Backup — Database backup, restore, integrity checking, and data pruning.

Manages SQLite database backups with configurable retention,
integrity verification, automatic restore on corruption,
and data pruning for old records.
"""

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

ANAH_DIR = Path.home() / ".anah"
DB_FILE = ANAH_DIR / "anah.db"
BACKUPS_DIR = ANAH_DIR / "backups"

# Retention defaults (seconds)
HEALTH_LOG_RETENTION = 7 * 86400       # 7 days
COMPLETED_TASK_RETENTION = 30 * 86400  # 30 days
TRAJECTORY_RETENTION = 90 * 86400      # 90 days
DISMISSED_GOAL_RETENTION = 14 * 86400  # 14 days

# Backup retention
MAX_DAILY_BACKUPS = 7
MAX_WEEKLY_BACKUPS = 4


def create_backup(tag: str = "") -> dict:
    """Create a timestamped backup of the database."""
    if not DB_FILE.exists():
        return {"error": "No database found to backup"}

    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    suffix = f"-{tag}" if tag else ""
    backup_name = f"anah-{ts}{suffix}.db"
    backup_path = BACKUPS_DIR / backup_name

    # Use SQLite online backup API for consistency
    try:
        src = sqlite3.connect(str(DB_FILE))
        dst = sqlite3.connect(str(backup_path))
        src.backup(dst)
        dst.close()
        src.close()

        size = backup_path.stat().st_size
        return {
            "path": str(backup_path),
            "size_bytes": size,
            "timestamp": ts,
            "tag": tag or None,
        }
    except Exception as e:
        return {"error": str(e)}


def list_backups() -> list[dict]:
    """List all available backups, newest first."""
    if not BACKUPS_DIR.exists():
        return []
    backups = []
    for f in sorted(BACKUPS_DIR.glob("anah-*.db"), reverse=True):
        backups.append({
            "name": f.name,
            "path": str(f),
            "size_bytes": f.stat().st_size,
            "modified": f.stat().st_mtime,
        })
    return backups


def restore_backup(backup_path: str | None = None) -> dict:
    """Restore database from a backup. Uses latest if no path specified."""
    if backup_path:
        src = Path(backup_path)
    else:
        backups = list_backups()
        if not backups:
            return {"error": "No backups available"}
        src = Path(backups[0]["path"])

    if not src.exists():
        return {"error": f"Backup not found: {src}"}

    # Verify backup integrity first
    try:
        check_db = sqlite3.connect(str(src))
        result = check_db.execute("PRAGMA integrity_check").fetchone()
        check_db.close()
        if result[0] != "ok":
            return {"error": f"Backup is also corrupt: {result[0]}"}
    except Exception as e:
        return {"error": f"Cannot read backup: {e}"}

    # Replace current DB
    try:
        if DB_FILE.exists():
            corrupt_path = DB_FILE.with_suffix(".db.corrupt")
            shutil.move(str(DB_FILE), str(corrupt_path))
        shutil.copy2(str(src), str(DB_FILE))
        return {
            "restored_from": str(src),
            "size_bytes": DB_FILE.stat().st_size,
        }
    except Exception as e:
        return {"error": str(e)}


def check_integrity() -> dict:
    """Check database integrity. Returns status and details."""
    if not DB_FILE.exists():
        return {"status": "missing", "message": "Database does not exist"}

    try:
        db = sqlite3.connect(str(DB_FILE))
        result = db.execute("PRAGMA integrity_check").fetchone()
        db.close()
        ok = result[0] == "ok"
        return {
            "status": "ok" if ok else "corrupt",
            "message": result[0],
            "size_bytes": DB_FILE.stat().st_size,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def prune_old_data(
    health_days: float | None = None,
    task_days: float | None = None,
    goal_days: float | None = None,
) -> dict:
    """Prune old records from the database. Returns counts of deleted rows."""
    if not DB_FILE.exists():
        return {"error": "No database"}

    health_cutoff = time.time() - (health_days or HEALTH_LOG_RETENTION / 86400) * 86400
    task_cutoff = time.time() - (task_days or COMPLETED_TASK_RETENTION / 86400) * 86400
    goal_cutoff = time.time() - (goal_days or DISMISSED_GOAL_RETENTION / 86400) * 86400

    db = sqlite3.connect(str(DB_FILE))
    results = {}

    # Prune old health logs
    cursor = db.execute("DELETE FROM health_logs WHERE timestamp < ?", (health_cutoff,))
    results["health_logs"] = cursor.rowcount

    # Prune old completed/failed tasks (keep queued/running)
    cursor = db.execute(
        "DELETE FROM task_queue WHERE status IN ('completed', 'failed') AND completed_at < ?",
        (task_cutoff,),
    )
    results["tasks"] = cursor.rowcount

    # Prune old dismissed goals
    cursor = db.execute(
        "DELETE FROM generated_goals WHERE status = 'dismissed' AND timestamp < ?",
        (goal_cutoff,),
    )
    results["goals"] = cursor.rowcount

    db.commit()
    db.close()
    return results


def prune_trajectories(days: float | None = None) -> dict:
    """Prune old trajectory files."""
    traj_dir = ANAH_DIR / "trajectories"
    if not traj_dir.exists():
        return {"deleted": 0}

    cutoff = time.time() - (days or TRAJECTORY_RETENTION / 86400) * 86400
    deleted = 0
    for f in traj_dir.glob("*.json"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            deleted += 1
    return {"deleted": deleted}


def rotate_backups() -> dict:
    """Rotate backups: keep last N daily + M weekly."""
    backups = list_backups()
    if not backups:
        return {"kept": 0, "deleted": 0}

    kept = []
    deleted = []
    seen_days = set()
    seen_weeks = set()
    daily_count = 0
    weekly_count = 0

    for b in backups:
        ts = b["modified"]
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        week = time.strftime("%Y-W%W", time.localtime(ts))

        if day not in seen_days and daily_count < MAX_DAILY_BACKUPS:
            seen_days.add(day)
            daily_count += 1
            kept.append(b["name"])
        elif week not in seen_weeks and weekly_count < MAX_WEEKLY_BACKUPS:
            seen_weeks.add(week)
            weekly_count += 1
            kept.append(b["name"])
        else:
            Path(b["path"]).unlink(missing_ok=True)
            deleted.append(b["name"])

    return {"kept": len(kept), "deleted": len(deleted)}


def run_maintenance() -> dict:
    """Full maintenance cycle: backup → prune data → prune trajectories → rotate backups."""
    results = {}

    # 1. Create backup before pruning
    results["backup"] = create_backup(tag="maintenance")

    # 2. Check integrity
    results["integrity"] = check_integrity()

    # 3. Prune old data
    results["pruned"] = prune_old_data()

    # 4. Prune old trajectories
    results["trajectories"] = prune_trajectories()

    # 5. Rotate old backups
    results["rotation"] = rotate_backups()

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ANAH Backup & Maintenance")
    parser.add_argument("--backup", action="store_true", help="Create a database backup")
    parser.add_argument("--restore", nargs="?", const="latest", help="Restore from backup (latest or path)")
    parser.add_argument("--check", action="store_true", help="Check database integrity")
    parser.add_argument("--prune", action="store_true", help="Prune old data")
    parser.add_argument("--list", action="store_true", help="List available backups")
    parser.add_argument("--maintain", action="store_true", help="Full maintenance cycle")
    parser.add_argument("--tag", type=str, default="", help="Tag for backup filename")
    args = parser.parse_args()

    ANAH_DIR.mkdir(exist_ok=True)

    if args.backup:
        print(json.dumps(create_backup(args.tag), indent=2))
    elif args.restore is not None:
        path = None if args.restore == "latest" else args.restore
        print(json.dumps(restore_backup(path), indent=2))
    elif args.check:
        print(json.dumps(check_integrity(), indent=2))
    elif args.prune:
        print(json.dumps(prune_old_data(), indent=2))
    elif args.list:
        print(json.dumps(list_backups(), indent=2))
    elif args.maintain:
        print(json.dumps(run_maintenance(), indent=2))
    else:
        parser.print_help()

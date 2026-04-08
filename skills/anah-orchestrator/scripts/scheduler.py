#!/usr/bin/env python3
"""ANAH Scheduler — Hardened heartbeat daemon.

Runs the orchestrator on configurable intervals. Includes:
- Structured log rotation (JSON lines, max 10MB per file, keep 5)
- PID file management (prevents duplicate daemons)
- Crash recovery (auto-restart on exception with backoff)
- Resource guard (skip cycle if system is under heavy load)
- Notification integration (writes critical alerts to notifications.json)
- Discord webhook dispatch (sends heartbeat summaries and alerts)

Usage:
    python scheduler.py                  # Default intervals
    python scheduler.py --fast           # Aggressive intervals for testing
    python scheduler.py --watchdog-only  # L1 checks only, every 30s
    python scheduler.py --daemon         # Background mode with log file
"""

import json
import os
import signal
import sys
import time
from pathlib import Path

ANAH_DIR = Path.home() / ".anah"
LOGS_DIR = ANAH_DIR / "logs"
PID_FILE = ANAH_DIR / "scheduler.pid"
SCRIPTS_DIR = Path(__file__).resolve().parent

# Import orchestrator and discord notifier
sys.path.insert(0, str(SCRIPTS_DIR))
import orchestrator

NOTIFY_DIR = Path(__file__).resolve().parent.parent.parent / "anah-notify" / "scripts"
sys.path.insert(0, str(NOTIFY_DIR))
try:
    import discord_webhook as discord_notify
except ImportError:
    discord_notify = None

import backup


# ---------------------------------------------------------------------------
# Interval presets (seconds)
# ---------------------------------------------------------------------------
PRESETS = {
    "default": {
        "heartbeat": 180,    # Full cycle every 3 min
        "watchdog": 30,      # L1 check every 30s
    },
    "fast": {
        "heartbeat": 60,     # Full cycle every 1 min (testing)
        "watchdog": 15,      # L1 check every 15s
    },
    "conservative": {
        "heartbeat": 300,    # Full cycle every 5 min
        "watchdog": 60,      # L1 check every 60s
    },
}

# Hardening constants
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB per log file
MAX_LOG_FILES = 5
MAX_CONSECUTIVE_FAILURES = 5
BACKOFF_BASE = 5  # seconds
BACKOFF_MAX = 300  # 5 min max backoff
CPU_GUARD_THRESHOLD = 95  # skip cycle if CPU > 95%
RAM_GUARD_THRESHOLD = 95  # skip cycle if RAM > 95%

running = True


def signal_handler(sig, frame):
    global running
    log_stderr("[scheduler] Shutting down gracefully...")
    running = False


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------
def ensure_log_dir():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def log_stderr(msg: str):
    """Print to stderr (for terminal visibility)."""
    print(msg, file=sys.stderr)


def log_structured(level: str, component: str, message: str, **extra):
    """Write a structured JSON log line to the log file and stderr."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "epoch": time.time(),
        "level": level,
        "component": component,
        "message": message,
        **extra,
    }
    line = json.dumps(entry, default=str)

    # Write to log file
    try:
        ensure_log_dir()
        log_file = LOGS_DIR / "scheduler.jsonl"
        with open(str(log_file), "a", encoding="utf-8") as f:
            f.write(line + "\n")
        rotate_logs(log_file)
    except Exception:
        pass  # Never crash on logging failure

    # Also print summary to stderr
    prefix = f"[{component}]"
    if level == "ERROR":
        prefix = f"[{component}] ERROR:"
    elif level == "WARN":
        prefix = f"[{component}] WARN:"
    log_stderr(f"{prefix} {message}")


def rotate_logs(log_file: Path):
    """Rotate log file if it exceeds MAX_LOG_SIZE."""
    try:
        if not log_file.exists() or log_file.stat().st_size < MAX_LOG_SIZE:
            return

        # Rotate: scheduler.jsonl → scheduler.1.jsonl → ... → scheduler.5.jsonl
        for i in range(MAX_LOG_FILES, 1, -1):
            older = LOGS_DIR / f"scheduler.{i}.jsonl"
            newer = LOGS_DIR / f"scheduler.{i-1}.jsonl"
            if newer.exists():
                if older.exists():
                    older.unlink()
                newer.rename(older)

        rotated = LOGS_DIR / "scheduler.1.jsonl"
        if rotated.exists():
            rotated.unlink()
        log_file.rename(rotated)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# PID file management
# ---------------------------------------------------------------------------
def write_pid():
    """Write PID file. Returns True if we acquired the lock."""
    ANAH_DIR.mkdir(parents=True, exist_ok=True)

    # Check for stale PID
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            # Check if process is still alive
            try:
                os.kill(old_pid, 0)  # Signal 0 = check existence
                log_structured("ERROR", "scheduler",
                               f"Another scheduler is running (PID {old_pid}). Exiting.",
                               stale_pid=old_pid)
                return False
            except (OSError, ProcessLookupError):
                # Process is dead — stale PID file
                log_structured("WARN", "scheduler",
                               f"Removing stale PID file (PID {old_pid} not running)",
                               stale_pid=old_pid)
        except ValueError:
            pass  # Corrupt PID file

    PID_FILE.write_text(str(os.getpid()))
    return True


def remove_pid():
    """Remove PID file on shutdown."""
    try:
        if PID_FILE.exists():
            current = PID_FILE.read_text().strip()
            if current == str(os.getpid()):
                PID_FILE.unlink()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Resource guard
# ---------------------------------------------------------------------------
def resource_guard() -> tuple[bool, str]:
    """Check if system resources allow running a cycle.
    Returns (ok, reason)."""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.1)
        ram = psutil.virtual_memory().percent
        if cpu > CPU_GUARD_THRESHOLD:
            return False, f"CPU at {cpu}% (threshold {CPU_GUARD_THRESHOLD}%)"
        if ram > RAM_GUARD_THRESHOLD:
            return False, f"RAM at {ram}% (threshold {RAM_GUARD_THRESHOLD}%)"
        return True, f"CPU {cpu}%, RAM {ram}%"
    except ImportError:
        return True, "psutil not available, skipping guard"


# ---------------------------------------------------------------------------
# Notification helper
# ---------------------------------------------------------------------------
def write_notification(level: str, title: str, message: str):
    """Write a notification to the JSONL notification file and dispatch to Discord."""
    try:
        notif_file = ANAH_DIR / "notifications.json"
        entry = {
            "timestamp": time.time(),
            "level": level,
            "title": title,
            "message": message,
            "source": "scheduler",
        }
        with open(str(notif_file), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass
    # Dispatch to Discord (best-effort, never blocks scheduler)
    try:
        if discord_notify:
            discord_notify.send_notification(level, title, message, source="scheduler")
    except Exception:
        pass


def discord_heartbeat_summary(result: dict):
    """Send a heartbeat summary to Discord (best-effort)."""
    if not discord_notify:
        return
    try:
        # Build summary dict compatible with discord module
        bs = result.get("brainstem", {})
        cx = result.get("cortex", {})
        ex = result.get("executor", {})
        hp = result.get("hippocampus", {})
        tj = result.get("trajectories", {})
        summary = {
            "gated": result.get("gated", False),
            "health_score": bs.get("health_score", 0),
            "goals_generated": cx.get("count", 0),
            "tasks_processed": ex.get("processed", 0),
            "tasks_succeeded": ex.get("succeeded", 0),
            "skills_extracted": hp.get("extracted", 0),
            "trajectories_exported": tj.get("exported", 0),
            "duration_ms": round(result.get("duration_ms", 0)),
            "summary": f"Health: {bs.get('health_score', 0):.1f}%",
        }
        discord_notify.send_heartbeat(summary)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main scheduler
# ---------------------------------------------------------------------------
def run_scheduler(preset: str = "default", watchdog_only: bool = False, generate: bool = False):
    """Main scheduler loop with crash recovery and resource guards."""
    global running
    intervals = PRESETS.get(preset, PRESETS["default"])

    log_structured("INFO", "scheduler", "ANAH Heartbeat Scheduler starting",
                   preset=preset, heartbeat_sec=intervals["heartbeat"],
                   watchdog_sec=intervals["watchdog"], generate=generate,
                   mode="watchdog-only" if watchdog_only else "full",
                   pid=os.getpid())

    orchestrator.load_env()

    # Startup: check DB integrity and restore if needed
    integrity = backup.check_integrity()
    if integrity["status"] == "corrupt":
        log_structured("ERROR", "startup", "Database corrupt — attempting restore",
                       integrity=integrity["message"])
        write_notification("critical", "Database Corruption Detected",
                           f"Integrity check failed: {integrity['message']}. Attempting restore.")
        restore_result = backup.restore_backup()
        if "error" in restore_result:
            log_structured("ERROR", "startup", f"Restore failed: {restore_result['error']}")
        else:
            log_structured("INFO", "startup", "Database restored from backup",
                           restored_from=restore_result.get("restored_from"))
            write_notification("info", "Database Restored",
                               f"Restored from: {restore_result.get('restored_from')}")
    elif integrity["status"] == "ok":
        log_structured("INFO", "startup", "Database integrity OK",
                       size_bytes=integrity.get("size_bytes"))

    last_heartbeat = 0
    last_watchdog = 0
    last_maintenance = 0
    cycle_count = 0
    consecutive_failures = 0
    total_cycles = 0
    total_failures = 0
    start_time = time.time()

    while running:
        now = time.time()

        # Watchdog check (more frequent)
        if now - last_watchdog >= intervals["watchdog"]:
            try:
                result = orchestrator.watchdog_cycle()
                healthy = result.get("l1_healthy", False)
                duration = result.get("duration_ms", 0)
                status = "OK" if healthy else "CRITICAL"
                log_structured("INFO" if healthy else "ERROR", "watchdog",
                               f"L1 {status} ({duration:.0f}ms)",
                               l1_healthy=healthy, duration_ms=duration)
                if not healthy:
                    write_notification("critical", "L1 Health Failure",
                                       "Brainstem L1 check failed — higher functions suspended")
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
            except Exception as e:
                log_structured("ERROR", "watchdog", f"Exception: {e}", error=str(e))
                consecutive_failures += 1

            # Check for expired goal approvals on each watchdog tick
            try:
                import cortex
                cdb = cortex.get_db()
                expired = cortex.check_expired_approvals(cdb)
                cdb.close()
                if expired["enacted"] or expired["dismissed"]:
                    log_structured("INFO", "approvals",
                                   f"Expired: {expired['enacted']} auto-enacted, {expired['dismissed']} auto-dismissed",
                                   **expired)
            except Exception:
                pass  # Cortex approval check is best-effort

            last_watchdog = now

        # Full heartbeat cycle (less frequent)
        if not watchdog_only and now - last_heartbeat >= intervals["heartbeat"]:
            # Resource guard
            ok, reason = resource_guard()
            if not ok:
                log_structured("WARN", "heartbeat", f"Skipping cycle — {reason}",
                               guard_reason=reason)
                last_heartbeat = now
                continue

            # Backoff on consecutive failures
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                backoff = min(BACKOFF_BASE * (2 ** (consecutive_failures - MAX_CONSECUTIVE_FAILURES)),
                              BACKOFF_MAX)
                log_structured("WARN", "heartbeat",
                               f"Backing off {backoff}s after {consecutive_failures} consecutive failures",
                               backoff_sec=backoff, consecutive_failures=consecutive_failures)
                write_notification("warning", "Scheduler Backoff",
                                   f"Backing off {backoff}s after {consecutive_failures} failures")
                time.sleep(min(backoff, 5))  # Sleep in small chunks
                last_heartbeat = now
                continue

            cycle_count += 1
            total_cycles += 1
            try:
                log_structured("INFO", "heartbeat", f"Cycle #{cycle_count} starting",
                               cycle=cycle_count)
                result = orchestrator.full_cycle(generate=generate, learn=True)
                duration = result.get("duration_ms", 0)
                bs = result.get("brainstem", {})
                cx = result.get("cortex", {})
                hp = result.get("hippocampus", {})
                ex = result.get("executor", {})

                log_structured("INFO", "heartbeat", f"Cycle #{cycle_count} complete ({duration:.0f}ms)",
                               cycle=cycle_count, duration_ms=duration,
                               health_score=bs.get("health_score"),
                               checks_passed=bs.get("passed"),
                               checks_total=bs.get("passed", 0) + bs.get("failed", 0),
                               goals=cx.get("count", 0),
                               skills_extracted=hp.get("extracted", 0),
                               tasks_processed=ex.get("processed", 0),
                               gated=result.get("gated", False))

                if result.get("gated"):
                    log_structured("WARN", "heartbeat", "L1 failure suspended higher functions",
                                   gated=True)
                    write_notification("warning", "Heartbeat Gated",
                                       "L1 failure suspended higher brain functions")

                # Send heartbeat summary to Discord
                discord_heartbeat_summary(result)

                consecutive_failures = 0
            except Exception as e:
                total_failures += 1
                consecutive_failures += 1
                log_structured("ERROR", "heartbeat", f"Cycle #{cycle_count} crashed: {e}",
                               cycle=cycle_count, error=str(e),
                               consecutive_failures=consecutive_failures)
                write_notification("critical", "Heartbeat Crash",
                                   f"Cycle #{cycle_count} crashed: {e}")
            last_heartbeat = now

        # Daily maintenance: backup, prune, rotate (every 24h)
        if now - last_maintenance >= 86400:
            try:
                log_structured("INFO", "maintenance", "Running daily maintenance")
                maint = backup.run_maintenance()
                pruned = maint.get("pruned", {})
                log_structured("INFO", "maintenance", "Maintenance complete",
                               health_logs_pruned=pruned.get("health_logs", 0),
                               tasks_pruned=pruned.get("tasks", 0),
                               goals_pruned=pruned.get("goals", 0),
                               backups_rotated=maint.get("rotation", {}).get("deleted", 0))
            except Exception as e:
                log_structured("ERROR", "maintenance", f"Maintenance failed: {e}")

            # Training: check if ready, compare, promote
            try:
                TRAINER_DIR = SCRIPTS_DIR.parent.parent / "anah-trainer" / "scripts"
                sys.path.insert(0, str(TRAINER_DIR))
                import trainer
                train_result = trainer.run_training_if_ready()
                if train_result.get("triggered"):
                    log_structured("INFO", "training", "Training triggered",
                                   examples=train_result.get("sft", {}).get("after_dedup", 0))
                    write_notification("info", "Training Complete",
                                       f"Trained on {train_result.get('sft', {}).get('after_dedup', 0)} examples")
                    # A/B comparison
                    eval_result = trainer.compare_models()
                    log_structured("INFO", "training", f"A/B eval: tuned {eval_result['tuned_wins']}/{eval_result['total']} wins",
                                   tuned_wins=eval_result["tuned_wins"])
                    if eval_result.get("tuned_better"):
                        promo = trainer.promote_model()
                        if promo.get("promoted"):
                            log_structured("INFO", "training", f"Model promoted to {promo['model']}")
                            write_notification("info", "Model Promoted",
                                               f"Tuned model promoted ({promo['eval_wins']}/5 wins)")
                else:
                    # Check for model reversion even when not training
                    rev = trainer.check_model_reversion()
                    if rev.get("reverted"):
                        log_structured("WARN", "training", f"Model reverted: {rev['reason']}")
                        write_notification("warning", "Model Reverted", rev["reason"])
            except Exception as e:
                log_structured("ERROR", "training", f"Training check failed: {e}")

            last_maintenance = now

        # Sleep in small increments so we can respond to Ctrl+C
        time.sleep(1)

    # Shutdown summary
    uptime = time.time() - start_time
    log_structured("INFO", "scheduler", "Stopped",
                   uptime_sec=round(uptime),
                   total_cycles=total_cycles,
                   total_failures=total_failures)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ANAH Scheduler — hardened heartbeat daemon")
    parser.add_argument("--preset", choices=["default", "fast", "conservative"], default="default",
                        help="Interval preset")
    parser.add_argument("--fast", action="store_true", help="Shortcut for --preset fast")
    parser.add_argument("--watchdog-only", action="store_true", help="Only run L1 watchdog checks")
    parser.add_argument("--generate", "-g", action="store_true",
                        help="Enable cortex goal generation (requires ANTHROPIC_API_KEY)")
    args = parser.parse_args()

    preset = "fast" if args.fast else args.preset

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # PID file — prevent duplicate daemons
    if not write_pid():
        sys.exit(1)

    try:
        run_scheduler(preset=preset, watchdog_only=args.watchdog_only, generate=args.generate)
    finally:
        remove_pid()

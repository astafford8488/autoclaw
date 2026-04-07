#!/usr/bin/env python3
"""ANAH Scheduler — Lightweight heartbeat daemon.

Runs the orchestrator on configurable intervals without requiring
the full Autoclaw TypeScript build. Designed as a bridge until
Autoclaw's cron system is wired up.

Usage:
    python scheduler.py                  # Default intervals
    python scheduler.py --fast           # Aggressive intervals for testing
    python scheduler.py --watchdog-only  # L1 checks only, every 30s
"""

import json
import signal
import sys
import time
import threading
from pathlib import Path

ANAH_DIR = Path.home() / ".anah"
SCRIPTS_DIR = Path(__file__).resolve().parent

# Import orchestrator
sys.path.insert(0, str(SCRIPTS_DIR))
import orchestrator


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

running = True


def signal_handler(sig, frame):
    global running
    print("\n[scheduler] Shutting down gracefully...", file=sys.stderr)
    running = False


def run_scheduler(preset: str = "default", watchdog_only: bool = False, generate: bool = False):
    """Main scheduler loop."""
    global running
    intervals = PRESETS.get(preset, PRESETS["default"])

    print(f"[scheduler] ANAH Heartbeat Scheduler starting", file=sys.stderr)
    print(f"[scheduler] Preset: {preset}", file=sys.stderr)
    print(f"[scheduler] Heartbeat interval: {intervals['heartbeat']}s", file=sys.stderr)
    print(f"[scheduler] Watchdog interval: {intervals['watchdog']}s", file=sys.stderr)
    print(f"[scheduler] Goal generation: {'enabled' if generate else 'disabled'}", file=sys.stderr)
    print(f"[scheduler] Mode: {'watchdog-only' if watchdog_only else 'full'}", file=sys.stderr)
    print(f"[scheduler] PID: {__import__('os').getpid()}", file=sys.stderr)
    print(f"[scheduler] Press Ctrl+C to stop\n", file=sys.stderr)

    orchestrator.load_env()

    last_heartbeat = 0
    last_watchdog = 0
    cycle_count = 0

    while running:
        now = time.time()

        # Watchdog check (more frequent)
        if now - last_watchdog >= intervals["watchdog"]:
            try:
                result = orchestrator.watchdog_cycle()
                healthy = result.get("l1_healthy", False)
                duration = result.get("duration_ms", 0)
                status = "OK" if healthy else "CRITICAL"
                print(f"[watchdog] L1 {status} ({duration:.0f}ms)", file=sys.stderr)
                if not healthy:
                    print(f"[watchdog] L1 FAILURE — higher functions suspended", file=sys.stderr)
            except Exception as e:
                print(f"[watchdog] Error: {e}", file=sys.stderr)
            last_watchdog = now

        # Full heartbeat cycle (less frequent)
        if not watchdog_only and now - last_heartbeat >= intervals["heartbeat"]:
            cycle_count += 1
            try:
                print(f"\n[heartbeat] Cycle #{cycle_count} starting...", file=sys.stderr)
                result = orchestrator.full_cycle(generate=generate, learn=True)
                duration = result.get("duration_ms", 0)
                bs = result.get("brainstem", {})
                cx = result.get("cortex", {})
                hp = result.get("hippocampus", {})

                print(f"[heartbeat] Cycle #{cycle_count} complete ({duration:.0f}ms)", file=sys.stderr)
                print(f"  Health: {bs.get('health_score', '?')}%  "
                      f"Passed: {bs.get('passed', '?')}/{bs.get('passed', 0) + bs.get('failed', 0)}  "
                      f"Goals: {cx.get('count', 0)}  "
                      f"Skills: {hp.get('extracted', 0)}", file=sys.stderr)

                if result.get("gated"):
                    print(f"  [GATED] L1 failure suspended higher functions", file=sys.stderr)
            except Exception as e:
                print(f"[heartbeat] Error in cycle #{cycle_count}: {e}", file=sys.stderr)
            last_heartbeat = now

        # Sleep in small increments so we can respond to Ctrl+C
        time.sleep(1)

    print("[scheduler] Stopped.", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ANAH Scheduler — lightweight heartbeat daemon")
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

    run_scheduler(preset=preset, watchdog_only=args.watchdog_only, generate=args.generate)

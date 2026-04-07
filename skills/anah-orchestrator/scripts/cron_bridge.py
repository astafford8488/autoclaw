#!/usr/bin/env python3
"""ANAH Cron Bridge — Interface between Autoclaw cron and ANAH orchestrator.

Provides JSON-RPC-style entry points that Autoclaw's cron system can invoke.
Each function returns structured JSON for delivery back to chat channels.

Designed to be called by Autoclaw isolated agent turns via:
    python cron_bridge.py heartbeat       # Full cycle
    python cron_bridge.py watchdog        # Quick L1 check
    python cron_bridge.py status          # Status overview
    python cron_bridge.py train           # Training pipeline
    python cron_bridge.py export          # Export trajectories

Or imported as a module by agent tool integrations.
"""

import json
import os
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ANAH_DIR = Path.home() / ".anah"

# Ensure orchestrator is importable
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def heartbeat(generate: bool = True, execute: bool = True, learn: bool = True) -> dict:
    """Run full ANAH heartbeat cycle, return structured summary."""
    import orchestrator

    orchestrator.load_env()
    ANAH_DIR.mkdir(exist_ok=True)

    try:
        result = orchestrator.full_cycle(
            generate=generate, execute=execute, learn=learn,
        )
        return format_heartbeat_summary(result)
    except Exception as e:
        return {"ok": False, "error": str(e), "command": "heartbeat"}


def watchdog() -> dict:
    """Quick L1 health check."""
    import orchestrator

    orchestrator.load_env()
    ANAH_DIR.mkdir(exist_ok=True)

    try:
        result = orchestrator.watchdog_cycle()
        healthy = result.get("l1_healthy", False)
        return {
            "ok": True,
            "command": "watchdog",
            "healthy": healthy,
            "checks": result.get("checks", {}),
            "duration_ms": round(result.get("duration_ms", 0)),
            "summary": "L1 healthy" if healthy else "L1 UNHEALTHY — check brainstem",
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "command": "watchdog"}


def status() -> dict:
    """Get ANAH status overview."""
    import orchestrator

    orchestrator.load_env()
    ANAH_DIR.mkdir(exist_ok=True)

    try:
        result = orchestrator.status_overview()
        return {"ok": True, "command": "status", **result}
    except Exception as e:
        return {"ok": False, "error": str(e), "command": "status"}


def train(model_name: str = "anah-tuned") -> dict:
    """Run training pipeline."""
    trainer_dir = Path(__file__).resolve().parent.parent.parent / "anah-trainer" / "scripts"
    if str(trainer_dir) not in sys.path:
        sys.path.insert(0, str(trainer_dir))

    try:
        import trainer
        result = trainer.run_training(model_name)
        return {"ok": True, "command": "train", **result}
    except Exception as e:
        return {"ok": False, "error": str(e), "command": "train"}


def export_trajectories() -> dict:
    """Export recent trajectories."""
    import orchestrator

    orchestrator.load_env()
    ANAH_DIR.mkdir(exist_ok=True)

    try:
        result = orchestrator.run_trajectory_export()
        return {"ok": True, "command": "export", **result}
    except Exception as e:
        return {"ok": False, "error": str(e), "command": "export"}


def format_heartbeat_summary(result: dict) -> dict:
    """Format full cycle result into a concise summary for delivery."""
    summary_parts = []
    bs = result.get("brainstem", {})
    health = bs.get("health_score", 0)
    summary_parts.append(f"Health: {health:.0%}")

    if result.get("gated"):
        summary_parts.append("GATED — L1 failure, higher functions skipped")
        return {
            "ok": True,
            "command": "heartbeat",
            "gated": True,
            "health_score": health,
            "summary": " | ".join(summary_parts),
            "duration_ms": round(result.get("duration_ms", 0)),
        }

    cortex = result.get("cortex", {})
    goals = cortex.get("count", 0)
    if goals:
        summary_parts.append(f"Goals: {goals}")

    executor = result.get("executor", {})
    tasks = executor.get("processed", 0)
    if tasks:
        summary_parts.append(f"Tasks: {executor.get('succeeded', 0)}/{tasks}")

    hippo = result.get("hippocampus", {})
    skills = hippo.get("extracted", 0)
    if skills:
        summary_parts.append(f"Skills: +{skills}")

    trajs = result.get("trajectories", {})
    exported = trajs.get("exported", 0)
    if exported:
        summary_parts.append(f"Trajectories: {exported}")

    return {
        "ok": True,
        "command": "heartbeat",
        "gated": False,
        "health_score": health,
        "goals_generated": goals,
        "tasks_processed": tasks,
        "tasks_succeeded": executor.get("succeeded", 0),
        "skills_extracted": skills,
        "trajectories_exported": exported,
        "summary": " | ".join(summary_parts),
        "duration_ms": round(result.get("duration_ms", 0)),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
COMMANDS = {
    "heartbeat": heartbeat,
    "watchdog": watchdog,
    "status": status,
    "train": train,
    "export": export_trajectories,
}

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ANAH Cron Bridge")
    parser.add_argument("command", choices=list(COMMANDS.keys()), help="Command to run")
    parser.add_argument("--no-generate", action="store_true", help="Skip cortex generation")
    parser.add_argument("--no-execute", action="store_true", help="Skip task execution")
    parser.add_argument("--no-learn", action="store_true", help="Skip hippocampus learning")
    parser.add_argument("--model-name", default="anah-tuned", help="Model name for training")
    args = parser.parse_args()

    if args.command == "heartbeat":
        result = heartbeat(
            generate=not args.no_generate,
            execute=not args.no_execute,
            learn=not args.no_learn,
        )
    elif args.command == "train":
        result = train(args.model_name)
    else:
        result = COMMANDS[args.command]()

    print(json.dumps(result, indent=2))

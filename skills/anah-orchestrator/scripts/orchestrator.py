#!/usr/bin/env python3
"""ANAH Orchestrator — Central nervous system coordinating all brain organelles.

Chains brainstem → cerebellum → cortex → hippocampus in a single heartbeat cycle.
Designed to run standalone or as an Autoclaw cron job.
"""

import asyncio
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

# Resolve sibling skill script directories
SKILLS_DIR = Path(__file__).resolve().parent.parent.parent
BRAINSTEM_DIR = SKILLS_DIR / "anah-brainstem" / "scripts"
CEREBELLUM_DIR = SKILLS_DIR / "anah-cerebellum" / "scripts"
CORTEX_DIR = SKILLS_DIR / "anah-cortex" / "scripts"
EXECUTOR_DIR = SKILLS_DIR / "anah-executor" / "scripts"
HIPPOCAMPUS_DIR = SKILLS_DIR / "anah-hippocampus" / "scripts"
MEMORY_DIR = SKILLS_DIR / "anah-memory" / "scripts"

ANAH_DIR = Path.home() / ".anah"

# Add script dirs to path for imports
for d in (BRAINSTEM_DIR, CEREBELLUM_DIR, CORTEX_DIR, EXECUTOR_DIR, HIPPOCAMPUS_DIR, MEMORY_DIR):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))


def load_env():
    """Load .env from ~/.anah/.env if present."""
    env_file = ANAH_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


def run_brainstem(levels: list[int] | None = None) -> dict:
    """Run brainstem health checks (async wrapper)."""
    import brainstem
    return asyncio.run(brainstem.run_checks(levels=levels))


def run_cerebellum(brainstem_results: dict) -> dict:
    """Ingest brainstem results and analyze patterns."""
    import cerebellum

    db = cerebellum.get_db()

    # Ingest brainstem check results into health_logs
    cerebellum.ingest_brainstem_results(db, brainstem_results["results"])

    # Load state for context building
    state_file = ANAH_DIR / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {"levels": {}, "gating": {}}

    # Build context and detect patterns
    context = cerebellum.build_context(db, state)
    patterns = cerebellum.analyze(db, state)

    db.close()
    return {
        "context": context,
        "patterns": [asdict(p) for p in patterns],
    }


def run_cortex(cerebellum_output: dict, generate: bool = True) -> dict:
    """Generate goals from cerebellum context."""
    if not generate:
        return {"skipped": True, "reason": "generation disabled"}

    import cortex

    context = cerebellum_output.get("context", {})
    patterns = cerebellum_output.get("patterns", [])
    enacted = cortex.run_generation(context, patterns)
    return {"enacted": enacted, "count": len(enacted)}


def run_executor(limit: int = 5) -> dict:
    """Execute queued tasks."""
    import executor
    db = executor.get_db()
    results = executor.run_queue(db, limit=limit)
    db.close()
    succeeded = sum(1 for r in results if r.get("success"))
    failed = sum(1 for r in results if not r.get("success"))
    return {"processed": len(results), "succeeded": succeeded, "failed": failed, "results": results}


def run_hippocampus(hours: float = 1.0) -> dict:
    """Evaluate recent tasks for skill extraction."""
    import hippocampus
    results = hippocampus.evaluate_recent(hours)
    extracted = [r for r in results if r.get("extracted")]
    return {"evaluated": len(results), "extracted": len(extracted), "results": results}


def run_memory_status() -> dict:
    """Get memory utilization."""
    import memory
    return memory.memory_status()


# ---------------------------------------------------------------------------
# Cycle modes
# ---------------------------------------------------------------------------
def full_cycle(generate: bool = True, execute: bool = True, learn: bool = True) -> dict:
    """Run a complete heartbeat cycle through all organelles."""
    t0 = time.time()
    result = {"timestamp": t0, "cycle": "full"}

    # 1. Brainstem — all levels
    print("[brainstem] Running L1-L3 health checks...", file=sys.stderr)
    brainstem_out = run_brainstem()
    result["brainstem"] = {
        "health_score": brainstem_out["summary"]["health_score"],
        "passed": brainstem_out["summary"]["passed"],
        "failed": brainstem_out["summary"]["failed"],
        "l1_healthy": brainstem_out["gating"]["l1_healthy"],
    }

    # Gate: if L1 failed, skip higher functions
    if not brainstem_out["gating"]["l1_healthy"]:
        result["gated"] = True
        result["duration_ms"] = (time.time() - t0) * 1000
        print("[GATED] L1 failure — skipping cerebellum/cortex/hippocampus", file=sys.stderr)
        return result

    # 2. Cerebellum — ingest + analyze
    print("[cerebellum] Ingesting and analyzing...", file=sys.stderr)
    cerebellum_out = run_cerebellum(brainstem_out)
    result["cerebellum"] = {
        "patterns": len(cerebellum_out["patterns"]),
        "health_score": cerebellum_out["context"].get("health_score"),
        "queue": cerebellum_out["context"].get("queue", {}),
    }

    # 3. Executor — process queued tasks before generating new ones
    if execute:
        print("[executor] Processing queued tasks...", file=sys.stderr)
        exec_out = run_executor(limit=5)
        result["executor"] = exec_out

    # 4. Cortex — goal generation
    print("[cortex] Generating goals...", file=sys.stderr)
    cortex_out = run_cortex(cerebellum_out, generate=generate)
    result["cortex"] = cortex_out

    # 5. Hippocampus — skill learning
    if learn:
        print("[hippocampus] Evaluating for skill extraction...", file=sys.stderr)
        hippo_out = run_hippocampus()
        result["hippocampus"] = hippo_out

    result["duration_ms"] = (time.time() - t0) * 1000
    return result


def watchdog_cycle() -> dict:
    """Quick L1-only check — minimal overhead."""
    t0 = time.time()
    brainstem_out = run_brainstem(levels=[1])
    return {
        "timestamp": t0,
        "cycle": "watchdog",
        "l1_healthy": brainstem_out["gating"]["l1_healthy"],
        "checks": brainstem_out["summary"],
        "duration_ms": (time.time() - t0) * 1000,
    }


def status_overview() -> dict:
    """Aggregate status from all organelles."""
    result = {}

    # Brainstem
    brainstem_out = run_brainstem()
    result["brainstem"] = {
        "health_score": brainstem_out["summary"]["health_score"],
        "l1_healthy": brainstem_out["gating"]["l1_healthy"],
        "total_checks": brainstem_out["summary"]["total"],
        "passed": brainstem_out["summary"]["passed"],
    }

    # Memory
    result["memory"] = run_memory_status()

    # Cortex goals
    try:
        import cortex
        db = cortex.get_db()
        goals = cortex.get_recent_goals(db)
        result["cortex"] = {
            "total_goals": len(goals),
            "enacted": sum(1 for g in goals if g["status"] == "enacted"),
            "proposed": sum(1 for g in goals if g["status"] == "proposed"),
        }
        db.close()
    except Exception:
        result["cortex"] = {"error": "no goals table yet"}

    # Hippocampus
    try:
        import hippocampus
        skills = hippocampus.list_learned_skills()
        result["hippocampus"] = {"learned_skills": len(skills)}
    except Exception:
        result["hippocampus"] = {"learned_skills": 0}

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ANAH Orchestrator — central nervous system")
    parser.add_argument("--cycle", "-c", action="store_true", help="Run full heartbeat cycle")
    parser.add_argument("--watchdog", "-w", action="store_true", help="Quick L1-only watchdog")
    parser.add_argument("--status", "-s", action="store_true", help="Status overview of all organelles")
    parser.add_argument("--generate", "-g", action="store_true", help="Enable cortex goal generation")
    parser.add_argument("--execute", "-e", action="store_true", help="Execute queued tasks")
    parser.add_argument("--no-learn", action="store_true", help="Skip hippocampus learning")
    args = parser.parse_args()

    ANAH_DIR.mkdir(exist_ok=True)
    load_env()

    if args.cycle:
        result = full_cycle(generate=args.generate, execute=args.execute, learn=not args.no_learn)
        print(json.dumps(result, indent=2))
    elif args.watchdog:
        result = watchdog_cycle()
        print(json.dumps(result, indent=2))
    elif args.status:
        result = status_overview()
        print(json.dumps(result, indent=2))
    else:
        parser.print_help()

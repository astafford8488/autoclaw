#!/usr/bin/env python3
"""ANAH Cortex — L5 autonomous goal generation.

Analyzes cerebellum context and generates actionable goals via LLM
or pattern-based fallback. Handles deduplication and goal lifecycle.
"""

import json
import os
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path

ANAH_DIR = Path.home() / ".anah"
STATE_FILE = ANAH_DIR / "state.json"
DB_FILE = ANAH_DIR / "anah.db"

COOLDOWN_SEC = 180  # 3 minutes between generation cycles

SYSTEM_PROMPT = """You are ANAH's L5 Goal Generation engine — the autonomous reasoning layer (cortex) of a self-directed agent hierarchy.

Your role: analyze system state and generate actionable goals that improve system health, performance, and capabilities.

The hierarchy:
- L1 (Brainstem): network, filesystem, compute monitoring
- L2 (Brainstem): config integrity, DB integrity, backups
- L3 (Brainstem): external API health, integration pings
- L4 (Cerebellum): task metrics, pattern detection
- L5 (Cortex): YOU — goal generation, planning, self-improvement

IMPORTANT: You will receive a list of recently generated goals. DO NOT propose similar goals.
Generate NOVEL goals covering different aspects of the system.

Generate 1-3 specific, actionable tasks as a JSON array. Each task:
- title: descriptive action title
- priority: 0-9 (higher = more urgent)
- description: what to accomplish
- reasoning: why this is valuable now

If the system is healthy and idle, generate exploratory or self-improvement tasks."""

USER_PROMPT = """Current system state:

{context}

Recently generated goals (DO NOT repeat these or similar topics):
{recent_goals}

Generate 1-3 actionable tasks that are DIFFERENT from recent goals. JSON only."""


@dataclass
class Goal:
    title: str
    priority: int
    description: str
    reasoning: str
    source: str  # "llm" or "pattern"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_FILE))
    db.row_factory = sqlite3.Row
    return db


def get_recent_goals(db: sqlite3.Connection, limit: int = 20) -> list[dict]:
    cursor = db.execute(
        "SELECT * FROM generated_goals ORDER BY timestamp DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in cursor]


def log_goal(db: sqlite3.Connection, goal: Goal, context: dict | None = None) -> int:
    cursor = db.execute(
        """INSERT INTO generated_goals (timestamp, title, priority, description, reasoning, source, context, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed')""",
        (time.time(), goal.title, goal.priority, goal.description, goal.reasoning,
         goal.source, json.dumps(context) if context else None),
    )
    db.commit()
    return cursor.lastrowid


def enqueue_task(db: sqlite3.Connection, goal: Goal, goal_id: int) -> int:
    cursor = db.execute(
        """INSERT INTO task_queue (created_at, priority, source, title, description, status)
           VALUES (?, ?, 'l5_generated', ?, ?, 'queued')""",
        (time.time(), goal.priority, goal.title, goal.description),
    )
    task_id = cursor.lastrowid
    db.execute(
        "UPDATE generated_goals SET status = 'enacted', task_id = ? WHERE id = ?",
        (task_id, goal_id),
    )
    db.commit()
    return task_id


def dismiss_goal(db: sqlite3.Connection, goal_id: int):
    db.execute("UPDATE generated_goals SET status = 'dismissed' WHERE id = ?", (goal_id,))
    db.commit()


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------
def titles_similar(a: str, b: str, threshold: float = 0.5) -> bool:
    stop = {"health_report:", "self_diagnostic:", "cleanup:", "echo:", "a", "the", "and", "of", "for", "to", "in"}
    words_a = set(a.lower().split()) - stop
    words_b = set(b.lower().split()) - stop
    if not words_a or not words_b:
        return False
    overlap = len(words_a & words_b)
    return overlap / min(len(words_a), len(words_b)) >= threshold


def dedup_goals(new_goals: list[Goal], recent_titles: list[str]) -> list[Goal]:
    filtered = []
    for g in new_goals:
        if not any(titles_similar(g.title, rt) for rt in recent_titles):
            filtered.append(g)
    return filtered


# ---------------------------------------------------------------------------
# LLM generation — Ollama (primary) → Haiku (fallback)
# ---------------------------------------------------------------------------
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
HAIKU_MODEL = "claude-haiku-4-5-20251001"


def _parse_llm_response(content: str) -> list[Goal]:
    """Extract goals from LLM response text (handles markdown fences)."""
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0]
    elif "```" in content:
        content = content.split("```")[1].split("```")[0]

    tasks = json.loads(content.strip())
    if isinstance(tasks, dict):
        tasks = [tasks]  # Single goal returned without array wrapper
    return [Goal(
        title=t["title"], priority=t.get("priority", 3),
        description=t.get("description", ""), reasoning=t.get("reasoning", ""),
        source="llm",
    ) for t in tasks]


def _build_user_message(context: dict, recent_goals_text: str) -> str:
    return USER_PROMPT.format(
        context=json.dumps(context, indent=2),
        recent_goals=recent_goals_text,
    )


def generate_goals_ollama(context: dict, recent_goals_text: str) -> list[Goal]:
    """Generate goals via local Ollama instance (free, no API key)."""
    import urllib.request
    try:
        body = json.dumps({
            "model": OLLAMA_MODEL,
            "stream": False,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_message(context, recent_goals_text)},
            ],
        }).encode()

        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=body,
            headers={"content-type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read())
        content = data["message"]["content"]
        return _parse_llm_response(content)
    except Exception as e:
        print(f"[cortex] Ollama failed ({OLLAMA_MODEL}): {e}", file=__import__("sys").stderr)
        return []


def generate_goals_haiku(context: dict, recent_goals_text: str) -> list[Goal]:
    """Generate goals via Anthropic Haiku (cheap cloud fallback)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return []

    import urllib.request
    try:
        body = json.dumps({
            "model": HAIKU_MODEL,
            "max_tokens": 1024,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": _build_user_message(context, recent_goals_text)}],
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        content = data["content"][0]["text"]
        return _parse_llm_response(content)
    except Exception as e:
        print(f"[cortex] Haiku failed: {e}", file=__import__("sys").stderr)
        return []


def generate_goals_llm(context: dict, recent_goals_text: str) -> list[Goal]:
    """Try Ollama first, fall back to Haiku, then return empty for pattern fallback."""
    # 1. Ollama (free, local)
    goals = generate_goals_ollama(context, recent_goals_text)
    if goals:
        print(f"[cortex] Generated {len(goals)} goals via Ollama ({OLLAMA_MODEL})", file=__import__("sys").stderr)
        return goals

    # 2. Haiku (cheap cloud)
    goals = generate_goals_haiku(context, recent_goals_text)
    if goals:
        print(f"[cortex] Generated {len(goals)} goals via Haiku", file=__import__("sys").stderr)
        return goals

    # 3. Return empty → caller uses pattern fallback
    return []


def generate_goals_fallback(context: dict, patterns: list[dict]) -> list[Goal]:
    goals = []
    for p in patterns:
        if p.get("suggested_action"):
            prio = {"critical": 7, "warning": 5, "info": 3}.get(p.get("severity", "info"), 3)
            goals.append(Goal(
                title=p["suggested_action"], priority=prio,
                description=p["description"],
                reasoning=f"Pattern: {p['title']} ({p['category']})",
                source="pattern",
            ))
    if not goals and context.get("health_score", 0) >= 80:
        queue = context.get("queue", {})
        if queue.get("queued", 0) == 0 and queue.get("running", 0) == 0:
            goals.append(Goal(
                title="System health report: proactive assessment",
                priority=2,
                description="System is healthy and idle. Generate a comprehensive health report.",
                reasoning=f"Health score {context.get('health_score')}%, queue empty.",
                source="pattern",
            ))
    return goals


# ---------------------------------------------------------------------------
# Main generation cycle
# ---------------------------------------------------------------------------
def run_generation(context: dict, patterns: list[dict]) -> list[dict]:
    """Full L5 generation cycle. Returns list of enacted goal dicts."""
    db = get_db()

    # Fetch recent goals for dedup
    recent = get_recent_goals(db)
    recent_titles = [g["title"] for g in recent if g.get("status") != "dismissed"]
    recent_text = "\n".join(
        f"- [{g.get('status', '?')}] {g['title']}" for g in recent[:15]
    ) if recent else "None (first generation cycle)"

    # Generate
    goals = generate_goals_llm(context, recent_text)
    if not goals:
        goals = generate_goals_fallback(context, patterns)

    # Dedup
    goals = dedup_goals(goals, recent_titles)

    # Enqueue
    enacted = []
    for goal in goals:
        goal_id = log_goal(db, goal, context)
        task_id = enqueue_task(db, goal, goal_id)
        enacted.append({**asdict(goal), "goal_id": goal_id, "task_id": task_id})

    db.close()
    return enacted


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import sys
    # Load .env if available
    env_file = ANAH_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

    parser = argparse.ArgumentParser(description="ANAH Cortex — L5 goal generation")
    parser.add_argument("--generate", "-g", action="store_true", help="Run a generation cycle")
    parser.add_argument("--status", "-s", action="store_true", help="Show recent goals and stats")
    parser.add_argument("--dismiss", "-d", type=int, help="Dismiss a goal by ID")
    parser.add_argument("--context", type=str, help="Path to cerebellum context JSON (or - for stdin)")
    args = parser.parse_args()

    ANAH_DIR.mkdir(exist_ok=True)

    if args.dismiss:
        db = get_db()
        dismiss_goal(db, args.dismiss)
        db.close()
        print(json.dumps({"dismissed": args.dismiss}))
    elif args.status:
        db = get_db()
        goals = get_recent_goals(db)
        stats = {
            "total": len(goals),
            "enacted": sum(1 for g in goals if g["status"] == "enacted"),
            "proposed": sum(1 for g in goals if g["status"] == "proposed"),
            "dismissed": sum(1 for g in goals if g["status"] == "dismissed"),
        }
        print(json.dumps({"stats": stats, "recent": goals[:10]}, indent=2, default=str))
        db.close()
    elif args.generate:
        # Load context from cerebellum
        if args.context == "-":
            data = json.load(sys.stdin)
        elif args.context:
            data = json.loads(Path(args.context).read_text())
        else:
            # Run cerebellum inline
            sys.path.insert(0, str(Path(__file__).parent.parent.parent / "anah-cerebellum" / "scripts"))
            from cerebellum import get_db as cdb_get, build_context, analyze
            cdb = cdb_get()
            state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"levels": {}, "gating": {}}
            ctx = build_context(cdb, state)
            patterns = analyze(cdb, state)
            data = {"context": ctx, "patterns": [asdict(p) for p in patterns]}
            cdb.close()

        context = data.get("context", {})
        patterns = data.get("patterns", [])
        enacted = run_generation(context, patterns)
        print(json.dumps({"enacted": enacted, "count": len(enacted)}, indent=2))
    else:
        parser.print_help()

#!/usr/bin/env python3
"""ANAH Hippocampus — Autonomous skill creation and learning loop.

Evaluates completed tasks and extracts reusable procedures into
Autoclaw-compatible SKILL.md files. Inspired by Hermes Agent's
self-improving skill creation architecture.
"""

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path

ANAH_DIR = Path.home() / ".anah"
DB_FILE = ANAH_DIR / "anah.db"
SKILLS_DIR = ANAH_DIR / "skills"
LEARNING_LOG = ANAH_DIR / "learning_log.json"

# Minimum complexity for skill extraction
MIN_STEPS_FOR_SKILL = 3
SKILL_COOLDOWN_SEC = 300  # 5 min between skill creations


@dataclass
class TaskEvidence:
    task_id: int
    title: str
    description: str
    source: str
    status: str
    duration_ms: float | None
    result: dict | None
    created_at: float


@dataclass
class SkillCandidate:
    name: str
    description: str
    instructions: str
    category: str  # diagnostic, maintenance, monitoring, optimization
    confidence: float  # 0-1 how confident we are this is a good skill
    evidence_task_id: int


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_FILE))
    db.row_factory = sqlite3.Row
    return db


def get_task(db: sqlite3.Connection, task_id: int) -> TaskEvidence | None:
    cursor = db.execute("SELECT * FROM task_queue WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    if not row:
        return None
    r = dict(row)
    result = r.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            result = {"raw": result}
    duration = None
    if r.get("completed_at") and r.get("started_at"):
        duration = (r["completed_at"] - r["started_at"]) * 1000
    return TaskEvidence(
        task_id=r["id"], title=r["title"], description=r.get("description", ""),
        source=r["source"], status=r["status"], duration_ms=duration,
        result=result, created_at=r["created_at"],
    )


def get_completed_tasks(db: sqlite3.Connection, since: float, limit: int = 20) -> list[TaskEvidence]:
    cursor = db.execute(
        "SELECT * FROM task_queue WHERE status = 'completed' AND completed_at > ? ORDER BY completed_at DESC LIMIT ?",
        (since, limit),
    )
    tasks = []
    for row in cursor:
        r = dict(row)
        result = r.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                result = {"raw": result}
        duration = None
        if r.get("completed_at") and r.get("started_at"):
            duration = (r["completed_at"] - r["started_at"]) * 1000
        tasks.append(TaskEvidence(
            task_id=r["id"], title=r["title"], description=r.get("description", ""),
            source=r["source"], status=r["status"], duration_ms=duration,
            result=result, created_at=r["created_at"],
        ))
    return tasks


# ---------------------------------------------------------------------------
# Skill analysis
# ---------------------------------------------------------------------------
def extract_task_type(title: str) -> str:
    """Extract the handler type from a task title."""
    for prefix in ("health_report:", "self_diagnostic:", "cleanup:", "echo:", "hermes:"):
        if title.lower().startswith(prefix):
            return prefix.rstrip(":")
    return "general"


def assess_complexity(task: TaskEvidence) -> int:
    """Estimate how many distinct steps a task involved."""
    score = 1
    if task.result:
        result = task.result
        # Count nested structures as steps
        if isinstance(result, dict):
            score += len(result)
            for v in result.values():
                if isinstance(v, dict):
                    score += len(v)
                elif isinstance(v, list):
                    score += min(len(v), 5)
    if task.duration_ms and task.duration_ms > 1000:
        score += 1  # Long tasks are more complex
    return score


def should_extract_skill(task: TaskEvidence) -> tuple[bool, str]:
    """Decide if a completed task should be analyzed for skill extraction."""
    if task.status != "completed":
        return False, "task not completed"

    complexity = assess_complexity(task)
    if complexity < MIN_STEPS_FOR_SKILL:
        return False, f"too simple ({complexity} steps, need {MIN_STEPS_FOR_SKILL})"

    # Check if we already have a skill for this type
    task_type = extract_task_type(task.title)
    skill_dir = SKILLS_DIR / f"learned-{task_type}"
    if skill_dir.exists():
        # Could still refine — but skip for now
        return False, f"skill already exists for {task_type}"

    return True, f"complex enough ({complexity} steps)"


def generate_skill_candidate(task: TaskEvidence) -> SkillCandidate | None:
    """Generate a skill candidate from task evidence."""
    task_type = extract_task_type(task.title)
    clean_title = task.title
    for prefix in ("health_report:", "self_diagnostic:", "cleanup:", "echo:", "hermes:"):
        if clean_title.lower().startswith(prefix):
            clean_title = clean_title[len(prefix):].strip()
            break

    # Build skill name
    name = f"learned-{task_type}"
    slug = re.sub(r"[^a-z0-9]+", "-", clean_title.lower()).strip("-")[:30]
    if slug:
        name = f"learned-{slug}"

    # Categorize
    category = "general"
    if "diagnostic" in task.title.lower() or "investigate" in task.title.lower():
        category = "diagnostic"
    elif "cleanup" in task.title.lower() or "maintenance" in task.title.lower():
        category = "maintenance"
    elif "health" in task.title.lower() or "report" in task.title.lower():
        category = "monitoring"
    elif "performance" in task.title.lower() or "optimize" in task.title.lower():
        category = "optimization"

    # Build instructions from result
    instructions_parts = [
        f"## Task: {clean_title}",
        "",
        f"Category: {category}",
        f"Original source: {task.source}",
        "",
        "## Procedure",
        "",
        f"This skill was auto-generated from task #{task.task_id}.",
    ]

    if task.description:
        instructions_parts.extend(["", "### Context", "", task.description])

    if task.result and isinstance(task.result, dict):
        instructions_parts.extend(["", "### What Worked", ""])
        if "diagnostic" in task.result:
            diag = task.result["diagnostic"]
            for level_name, checks in diag.items():
                if isinstance(checks, list):
                    passed = sum(1 for c in checks if c.get("passed"))
                    total = len(checks)
                    instructions_parts.append(f"- {level_name}: {passed}/{total} checks passed")
        elif "hierarchy_summary" in task.result:
            for name, status in task.result["hierarchy_summary"].items():
                instructions_parts.append(f"- {name}: {status}")

    if task.duration_ms:
        instructions_parts.extend([
            "", f"### Performance", "",
            f"Expected duration: ~{int(task.duration_ms)}ms",
        ])

    instructions = "\n".join(instructions_parts)

    confidence = min(assess_complexity(task) / 10, 1.0)

    return SkillCandidate(
        name=name, description=f"Auto-learned skill: {clean_title}",
        instructions=instructions, category=category,
        confidence=confidence, evidence_task_id=task.task_id,
    )


# ---------------------------------------------------------------------------
# Skill writing
# ---------------------------------------------------------------------------
def write_skill(candidate: SkillCandidate):
    """Write a skill candidate to disk as an Autoclaw-compatible skill."""
    skill_dir = SKILLS_DIR / candidate.name
    skill_dir.mkdir(parents=True, exist_ok=True)

    skill_md = f"""---
name: {candidate.name}
description: "{candidate.description}. Auto-generated by ANAH hippocampus from task #{candidate.evidence_task_id}."
---

# {candidate.description}

{candidate.instructions}

## Metadata

- Generated by: ANAH Hippocampus (learning loop)
- Source task: #{candidate.evidence_task_id}
- Category: {candidate.category}
- Confidence: {candidate.confidence:.1%}
- Created: {time.strftime('%Y-%m-%d %H:%M:%S')}
"""
    (skill_dir / "SKILL.md").write_text(skill_md)


def log_learning(candidate: SkillCandidate, action: str):
    """Append to the learning log."""
    log = []
    if LEARNING_LOG.exists():
        try:
            log = json.loads(LEARNING_LOG.read_text())
        except Exception:
            log = []
    log.append({
        "timestamp": time.time(),
        "action": action,
        "skill_name": candidate.name,
        "task_id": candidate.evidence_task_id,
        "confidence": candidate.confidence,
        "category": candidate.category,
    })
    # Keep last 100 entries
    LEARNING_LOG.write_text(json.dumps(log[-100:], indent=2))


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------
def evaluate_task(task_id: int) -> dict:
    """Evaluate a single task for skill extraction."""
    db = get_db()
    task = get_task(db, task_id)
    db.close()

    if not task:
        return {"error": f"Task {task_id} not found"}

    should, reason = should_extract_skill(task)
    if not should:
        return {"task_id": task_id, "extracted": False, "reason": reason}

    candidate = generate_skill_candidate(task)
    if not candidate:
        return {"task_id": task_id, "extracted": False, "reason": "could not generate skill"}

    write_skill(candidate)
    log_learning(candidate, "created")

    return {
        "task_id": task_id,
        "extracted": True,
        "skill_name": candidate.name,
        "category": candidate.category,
        "confidence": candidate.confidence,
        "path": str(SKILLS_DIR / candidate.name / "SKILL.md"),
    }


def evaluate_recent(hours: float = 1.0) -> list[dict]:
    """Evaluate all recent completed tasks for skill extraction."""
    db = get_db()
    since = time.time() - (hours * 3600)
    tasks = get_completed_tasks(db, since)
    db.close()

    results = []
    for task in tasks:
        should, reason = should_extract_skill(task)
        if should:
            candidate = generate_skill_candidate(task)
            if candidate:
                write_skill(candidate)
                log_learning(candidate, "created")
                results.append({
                    "task_id": task.task_id, "extracted": True,
                    "skill_name": candidate.name, "confidence": candidate.confidence,
                })
            else:
                results.append({"task_id": task.task_id, "extracted": False, "reason": "generation failed"})
        else:
            results.append({"task_id": task.task_id, "extracted": False, "reason": reason})
    return results


def list_learned_skills() -> list[dict]:
    """List all auto-generated skills."""
    skills = []
    if SKILLS_DIR.exists():
        for skill_dir in sorted(SKILLS_DIR.iterdir()):
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                content = (skill_dir / "SKILL.md").read_text()
                # Extract name from frontmatter
                name = skill_dir.name
                desc = ""
                for line in content.splitlines():
                    if line.startswith("description:"):
                        desc = line.split(":", 1)[1].strip().strip('"')
                        break
                skills.append({"name": name, "description": desc, "path": str(skill_dir)})
    return skills


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ANAH Hippocampus — learning loop")
    parser.add_argument("--evaluate", "-e", type=int, help="Evaluate a task by ID for skill extraction")
    parser.add_argument("--evaluate-recent", action="store_true", help="Evaluate all recent tasks")
    parser.add_argument("--hours", type=float, default=1.0, help="Hours of recent tasks to evaluate")
    parser.add_argument("--list-skills", "-l", action="store_true", help="List auto-generated skills")
    args = parser.parse_args()

    ANAH_DIR.mkdir(exist_ok=True)
    SKILLS_DIR.mkdir(exist_ok=True)

    if args.evaluate:
        result = evaluate_task(args.evaluate)
        print(json.dumps(result, indent=2))
    elif args.evaluate_recent:
        results = evaluate_recent(args.hours)
        print(json.dumps({"evaluated": len(results), "results": results}, indent=2))
    elif args.list_skills:
        skills = list_learned_skills()
        print(json.dumps({"skills": skills, "count": len(skills)}, indent=2))
    else:
        parser.print_help()

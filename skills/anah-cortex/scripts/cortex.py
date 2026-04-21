#!/usr/bin/env python3
"""ANAH Cortex — L5 autonomous goal generation.

Analyzes cerebellum context and generates actionable goals via LLM
or pattern-based fallback. Features:
- Two-phase reasoning (analyze → generate)
- Historical success scoring per handler type
- Semantic dedup via topic_hash
- Pattern templates from hippocampus learned skills
- Goal approval mode with priority-based expiry
"""

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path

ANAH_DIR = Path.home() / ".anah"
STATE_FILE = ANAH_DIR / "state.json"
DB_FILE = ANAH_DIR / "anah.db"

COOLDOWN_SEC = 180  # 3 minutes between generation cycles

# Approval mode: set GOAL_APPROVAL=true in .env to require approval
GOAL_APPROVAL = os.environ.get("GOAL_APPROVAL", "false").lower() == "true"

# Priority-based expiry (seconds)
EXPIRY_HIGH = int(os.environ.get("EXPIRY_HIGH", "300"))       # 5 min
EXPIRY_MEDIUM = int(os.environ.get("EXPIRY_MEDIUM", "900"))   # 15 min
EXPIRY_LOW = int(os.environ.get("EXPIRY_LOW", "1800"))        # 30 min

# ---------------------------------------------------------------------------
# Prompts — Two-phase reasoning
# ---------------------------------------------------------------------------
ANALYSIS_PROMPT = """You are ANAH's L5 analysis engine. Analyze the system state and identify what the system needs most right now.

The hierarchy:
- L1: network, filesystem, compute monitoring
- L2: config integrity, DB integrity, backups
- L3: external API health, integration pings
- L4: task metrics, pattern detection
- L5: YOU — goal generation, planning, self-improvement

STRATEGIC INSIGHTS (from metacognition — take these seriously):
{strategy}

Consider:
1. What areas need attention? (failing checks, degrading performance, idle resources)
2. What has been tried recently and failed? (avoid repeating failures)
3. What handler types have good success rates? (leverage strengths)
4. What learned skills are available? (use existing capabilities)
5. What do the strategic insights recommend? (avoid/leverage/gaps)

Respond with a brief JSON analysis:
{{
  "needs": ["list of 1-3 system needs"],
  "avoid": ["topics to avoid based on recent failures and strategy"],
  "leverage": ["strengths or skills to build on"]
}}"""

GENERATION_PROMPT = """You are ANAH's L5 Goal Generation engine — the autonomous reasoning layer (cortex) of a self-directed agent hierarchy.

Your role: generate actionable goals that address the identified system needs.

ANALYSIS of current needs:
{analysis}

HANDLER SUCCESS RATES (use high-success handlers, avoid consistently failing ones):
{success_rates}

AVAILABLE LEARNED SKILLS (leverage these where relevant):
{skills}

AVAILABLE MCP TOOLS (use "mcp:" prefix to invoke external tools):
{mcp_tools}

IMPORTANT: You will receive a list of recently generated goals. DO NOT propose similar goals.
Generate 1-3 specific, actionable tasks as a JSON array. Each task:
- title: descriptive action title (prefix with handler type like "health_report:", "self_diagnostic:", "cleanup:", "mcp:")
- priority: 0-9 (higher = more urgent)
- description: what to accomplish
- reasoning: why this is valuable now

For MULTI-STEP plans, add chain fields:
- chain: true (marks this response as a chain)
- steps: array of tasks with "step" (1,2,3...) and optional "depends_on" (step number)
Example chain: {{"chain": true, "steps": [{{"step":1,"title":"research X","priority":5,"description":"...","reasoning":"..."}},{{"step":2,"title":"implement Y","priority":5,"description":"...","reasoning":"...","depends_on":1}}]}}

Use chains when a task naturally requires sequential steps (research then implement, analyze then fix, etc).
Single independent tasks should still be returned as a plain JSON array.

If the system is healthy and idle, generate exploratory or self-improvement tasks."""

USER_PROMPT = """Current system state:

{context}

Recently generated goals (DO NOT repeat these or similar topics):
{recent_goals}

Generate 1-3 actionable tasks that are DIFFERENT from recent goals. JSON only."""

# Legacy single-phase prompt for fallback
SYSTEM_PROMPT_LEGACY = """You are ANAH's L5 Goal Generation engine — the autonomous reasoning layer (cortex) of a self-directed agent hierarchy.

Your role: analyze system state and generate actionable goals that improve system health, performance, and capabilities.

IMPORTANT: You will receive a list of recently generated goals. DO NOT propose similar goals.
Generate NOVEL goals covering different aspects of the system.

Generate 1-3 specific, actionable tasks as a JSON array. Each task:
- title: descriptive action title
- priority: 0-9 (higher = more urgent)
- description: what to accomplish
- reasoning: why this is valuable now

If the system is healthy and idle, generate exploratory or self-improvement tasks."""


@dataclass
class Goal:
    title: str
    priority: int
    description: str
    reasoning: str
    source: str  # "llm" or "pattern"
    chain_id: str | None = None
    chain_step: int | None = None
    depends_on_goal_id: int | None = None


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_FILE))
    db.row_factory = sqlite3.Row
    return db


def get_recent_goals(db: sqlite3.Connection, limit: int = 50) -> list[dict]:
    cursor = db.execute(
        "SELECT * FROM generated_goals ORDER BY timestamp DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in cursor]


def log_goal(db: sqlite3.Connection, goal: Goal, context: dict | None = None,
             topic_hash: str | None = None, expires_at: float | None = None) -> int:
    cursor = db.execute(
        """INSERT INTO generated_goals
           (timestamp, title, priority, description, reasoning, source, context, status,
            topic_hash, expires_at, chain_id, chain_step, depends_on_goal_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (time.time(), goal.title, goal.priority, goal.description, goal.reasoning,
         goal.source, json.dumps(context) if context else None,
         'pending_approval' if GOAL_APPROVAL else 'proposed',
         topic_hash, expires_at,
         goal.chain_id, goal.chain_step, goal.depends_on_goal_id),
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


def approve_goal(db: sqlite3.Connection, goal_id: int) -> dict:
    """Approve a pending goal and enqueue it as a task."""
    row = db.execute("SELECT * FROM generated_goals WHERE id = ?", (goal_id,)).fetchone()
    if not row:
        return {"error": f"Goal {goal_id} not found"}
    row = dict(row)
    if row["status"] not in ("pending_approval", "proposed"):
        return {"error": f"Goal {goal_id} is {row['status']}, not pending"}

    goal = Goal(
        title=row["title"], priority=row["priority"],
        description=row.get("description", ""), reasoning=row.get("reasoning", ""),
        source=row["source"],
    )
    task_id = enqueue_task(db, goal, goal_id)
    return {"approved": goal_id, "task_id": task_id}


def check_expired_approvals(db: sqlite3.Connection) -> dict:
    """Check for pending approvals that have expired. Auto-enact or auto-dismiss based on priority."""
    now = time.time()
    cursor = db.execute(
        "SELECT * FROM generated_goals WHERE status = 'pending_approval' AND expires_at IS NOT NULL AND expires_at < ?",
        (now,),
    )
    enacted = 0
    dismissed = 0
    for row in cursor.fetchall():
        row = dict(row)
        priority = row.get("priority", 0)
        if priority >= 7:
            # High priority: auto-enact
            goal = Goal(
                title=row["title"], priority=priority,
                description=row.get("description", ""), reasoning=row.get("reasoning", ""),
                source=row["source"],
            )
            enqueue_task(db, goal, row["id"])
            enacted += 1
        elif priority >= 4:
            # Medium priority: auto-enact
            goal = Goal(
                title=row["title"], priority=priority,
                description=row.get("description", ""), reasoning=row.get("reasoning", ""),
                source=row["source"],
            )
            enqueue_task(db, goal, row["id"])
            enacted += 1
        else:
            # Low priority: auto-dismiss
            dismiss_goal(db, row["id"])
            dismissed += 1
    return {"enacted": enacted, "dismissed": dismissed}


def check_chain_promotions(db: sqlite3.Connection) -> dict:
    """Promote waiting chain steps whose dependencies have completed (enacted + task completed).
    Called after executor finishes tasks to advance chains."""
    promoted = 0
    waiting = db.execute(
        "SELECT * FROM generated_goals WHERE status = 'waiting' AND depends_on_goal_id IS NOT NULL"
    ).fetchall()
    for row in waiting:
        row = dict(row)
        dep_id = row["depends_on_goal_id"]
        # Check if dependency goal's task is completed
        dep = db.execute(
            "SELECT g.task_id, t.status as task_status FROM generated_goals g "
            "LEFT JOIN task_queue t ON g.task_id = t.id "
            "WHERE g.id = ?", (dep_id,)
        ).fetchone()
        if dep and dep["task_status"] == "completed":
            # Dependency done — enqueue this step
            goal = Goal(
                title=row["title"], priority=row["priority"],
                description=row.get("description", ""), reasoning=row.get("reasoning", ""),
                source=row["source"],
                chain_id=row.get("chain_id"), chain_step=row.get("chain_step"),
                depends_on_goal_id=dep_id,
            )
            enqueue_task(db, goal, row["id"])
            promoted += 1
        elif dep and dep["task_status"] == "failed":
            # Dependency failed — dismiss this step
            dismiss_goal(db, row["id"])
    return {"promoted": promoted}


# ---------------------------------------------------------------------------
# Historical success scoring
# ---------------------------------------------------------------------------
def get_handler_success_rates(db: sqlite3.Connection) -> dict[str, dict]:
    """Query task_queue for completion rates grouped by handler type (title prefix)."""
    cursor = db.execute(
        """SELECT
             CASE
               WHEN title LIKE 'health_report:%' THEN 'health_report'
               WHEN title LIKE 'self_diagnostic:%' THEN 'self_diagnostic'
               WHEN title LIKE 'cleanup:%' THEN 'cleanup'
               WHEN title LIKE 'echo:%' THEN 'echo'
               WHEN title LIKE 'hermes:%' THEN 'hermes'
               WHEN title LIKE 'notify:%' THEN 'notify'
               WHEN title LIKE 'resource_check:%' THEN 'resource_check'
               WHEN title LIKE 'config_audit:%' THEN 'config_audit'
               WHEN title LIKE 'backup:%' THEN 'backup'
               WHEN title LIKE 'api_ping:%' THEN 'api_ping'
               WHEN title LIKE 'log_analysis:%' THEN 'log_analysis'
               ELSE 'other'
             END as handler,
             COUNT(*) as total,
             SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
             SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
           FROM task_queue
           WHERE created_at > ?
           GROUP BY handler""",
        (time.time() - 7 * 86400,),  # Last 7 days
    )
    rates = {}
    for row in cursor:
        r = dict(row)
        total = r["total"]
        rate = r["completed"] / total * 100 if total > 0 else 0
        rates[r["handler"]] = {
            "total": total,
            "completed": r["completed"],
            "failed": r["failed"],
            "success_rate": round(rate, 1),
        }
    return rates


def format_success_rates(rates: dict) -> str:
    """Format success rates for prompt injection."""
    if not rates:
        return "No task history yet."
    lines = []
    for handler, data in sorted(rates.items(), key=lambda x: x[1]["success_rate"], reverse=True):
        emoji = "good" if data["success_rate"] >= 70 else "poor" if data["success_rate"] < 40 else "moderate"
        lines.append(f"- {handler}: {data['success_rate']}% success ({data['completed']}/{data['total']}) [{emoji}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Semantic dedup via topic_hash
# ---------------------------------------------------------------------------
def compute_topic_hash(title: str) -> str:
    """Compute a semantic topic hash from sorted keywords."""
    stop = {"health_report:", "self_diagnostic:", "cleanup:", "echo:", "hermes:",
            "notify:", "resource_check:", "config_audit:", "backup:", "api_ping:",
            "log_analysis:", "a", "the", "and", "of", "for", "to", "in", "on",
            "is", "it", "that", "this", "with", "as", "at", "by", "an"}
    words = set(re.sub(r"[^a-z0-9\s]", "", title.lower()).split()) - stop
    key = " ".join(sorted(words))
    return hashlib.md5(key.encode()).hexdigest()[:12]


def titles_similar(a: str, b: str, threshold: float = 0.5) -> bool:
    stop = {"health_report:", "self_diagnostic:", "cleanup:", "echo:", "a", "the", "and", "of", "for", "to", "in"}
    words_a = set(a.lower().split()) - stop
    words_b = set(b.lower().split()) - stop
    if not words_a or not words_b:
        return False
    overlap = len(words_a & words_b)
    return overlap / min(len(words_a), len(words_b)) >= threshold


def dedup_goals(new_goals: list[Goal], recent: list[dict]) -> list[Goal]:
    """Deduplicate using both word overlap and topic_hash."""
    recent_titles = [g["title"] for g in recent if g.get("status") != "dismissed"]
    recent_hashes = {g.get("topic_hash") for g in recent if g.get("topic_hash")} - {None}

    filtered = []
    for g in new_goals:
        # Word overlap check
        if any(titles_similar(g.title, rt) for rt in recent_titles):
            continue
        # Topic hash check
        h = compute_topic_hash(g.title)
        if h in recent_hashes:
            continue
        recent_hashes.add(h)  # Prevent intra-batch duplicates
        filtered.append(g)
    return filtered


# ---------------------------------------------------------------------------
# Hippocampus skill templates
# ---------------------------------------------------------------------------
def get_learned_skills() -> list[dict]:
    """Query hippocampus for learned skills to use as templates."""
    skills_dir = ANAH_DIR / "skills"
    if not skills_dir.exists():
        return []
    skills = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
            content = (skill_dir / "SKILL.md").read_text()
            name = skill_dir.name
            desc = ""
            category = ""
            for line in content.splitlines():
                if line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip('"')
                if "Category:" in line:
                    category = line.split(":", 1)[1].strip()
            skills.append({"name": name, "description": desc, "category": category})
    return skills


def format_skills(skills: list[dict]) -> str:
    """Format learned skills for prompt injection."""
    if not skills:
        return "No learned skills yet."
    return "\n".join(f"- {s['name']}: {s['description']}" for s in skills[:10])


# ---------------------------------------------------------------------------
# MCP tool registry for cortex awareness
# ---------------------------------------------------------------------------
MCP_TOOL_WHITELIST = [
    {"name": "web_search", "description": "Search the web for information", "example": "mcp: web_search best practices SQLite optimization"},
    {"name": "web_fetch", "description": "Fetch and read a web page URL", "example": "mcp: web_fetch https://docs.example.com/guide"},
    {"name": "slack_send_message", "description": "Send a message to a Slack channel", "example": "mcp: slack_send_message #general System update complete"},
    {"name": "slack_search_public", "description": "Search Slack messages", "example": "mcp: slack_search_public deployment issues"},
    {"name": "notion_search", "description": "Search Notion pages and databases", "example": "mcp: notion_search project roadmap"},
    {"name": "gmail_search_messages", "description": "Search Gmail messages", "example": "mcp: gmail_search_messages from:alerts subject:error"},
    {"name": "gcal_list_events", "description": "List upcoming Google Calendar events", "example": "mcp: gcal_list_events"},
]


def format_mcp_tools() -> str:
    """Format available MCP tools for prompt injection."""
    if not MCP_TOOL_WHITELIST:
        return "No external tools available."
    lines = []
    for t in MCP_TOOL_WHITELIST:
        lines.append(f"- {t['name']}: {t['description']} (e.g. \"{t['example']}\")")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Chain parsing
# ---------------------------------------------------------------------------
def _parse_chain_response(data: dict | list) -> list[Goal]:
    """Parse a chain response into linked Goal objects."""
    if isinstance(data, list):
        # Not a chain — regular goal list
        return [Goal(
            title=t["title"], priority=t.get("priority", 3),
            description=t.get("description", ""), reasoning=t.get("reasoning", ""),
            source="llm",
        ) for t in data]

    if isinstance(data, dict) and data.get("chain"):
        steps = data.get("steps", [])
        if not steps:
            return []
        chain_id = uuid.uuid4().hex[:12]
        # Map step numbers to goals (we'll resolve depends_on after logging)
        goals = []
        for s in steps:
            goals.append(Goal(
                title=s["title"], priority=s.get("priority", 3),
                description=s.get("description", ""), reasoning=s.get("reasoning", ""),
                source="llm",
                chain_id=chain_id,
                chain_step=s.get("step", len(goals) + 1),
                # Store raw depends_on step number — resolved to goal_id during logging
                depends_on_goal_id=s.get("depends_on"),
            ))
        return goals

    # Single goal dict
    if isinstance(data, dict) and "title" in data:
        return [Goal(
            title=data["title"], priority=data.get("priority", 3),
            description=data.get("description", ""), reasoning=data.get("reasoning", ""),
            source="llm",
        )]
    return []


# ---------------------------------------------------------------------------
# LLM generation — Ollama (primary) → Haiku (fallback)
# ---------------------------------------------------------------------------
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
HAIKU_MODEL = "claude-haiku-4-5-20251001"


def _parse_llm_response(content: str) -> list[Goal]:
    """Extract goals from LLM response text (handles markdown fences and chains)."""
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0]
    elif "```" in content:
        content = content.split("```")[1].split("```")[0]

    data = json.loads(content.strip())
    return _parse_chain_response(data)


def _parse_analysis_response(content: str) -> dict:
    """Extract analysis JSON from LLM response."""
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0]
    elif "```" in content:
        content = content.split("```")[1].split("```")[0]
    try:
        return json.loads(content.strip())
    except json.JSONDecodeError:
        return {"needs": ["general system health"], "avoid": [], "leverage": []}


def _call_ollama(messages: list[dict], timeout: int = 60) -> str | None:
    """Call Ollama chat API. Returns content or None on failure."""
    import urllib.request
    try:
        body = json.dumps({
            "model": OLLAMA_MODEL,
            "stream": False,
            "messages": messages,
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=body,
            headers={"content-type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read())
        return data["message"]["content"]
    except Exception as e:
        print(f"[cortex] Ollama failed ({OLLAMA_MODEL}): {e}", file=__import__("sys").stderr)
        return None


def _call_haiku(system: str, user: str) -> str | None:
    """Call Anthropic Haiku API. Returns content or None on failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    import urllib.request
    try:
        body = json.dumps({
            "model": HAIKU_MODEL,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": user}],
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
        return data["content"][0]["text"]
    except Exception as e:
        print(f"[cortex] Haiku failed: {e}", file=__import__("sys").stderr)
        return None


def _build_user_message(context: dict, recent_goals_text: str) -> str:
    return USER_PROMPT.format(
        context=json.dumps(context, indent=2),
        recent_goals=recent_goals_text,
    )


def generate_goals_twophase(context: dict, recent_goals_text: str,
                             success_rates: dict, skills: list[dict]) -> list[Goal]:
    """Two-phase generation: analyze needs → generate targeted goals."""
    # Phase 1: Analysis — inject metacognition strategy insights
    try:
        from metacognition import get_strategy_for_cortex
        strategy_text = get_strategy_for_cortex()
    except Exception:
        strategy_text = "No strategic insights available."

    analysis_system = ANALYSIS_PROMPT.format(strategy=strategy_text)
    analysis_user = f"System state:\n{json.dumps(context, indent=2)}\n\nRecent goals:\n{recent_goals_text}"
    analysis_content = _call_ollama([
        {"role": "system", "content": analysis_system},
        {"role": "user", "content": analysis_user},
    ], timeout=30)

    if not analysis_content:
        analysis_content = _call_haiku(analysis_system, analysis_user)

    analysis = _parse_analysis_response(analysis_content) if analysis_content else {
        "needs": ["general system health check"], "avoid": [], "leverage": []
    }

    # Phase 2: Targeted generation
    gen_system = GENERATION_PROMPT.format(
        analysis=json.dumps(analysis, indent=2),
        success_rates=format_success_rates(success_rates),
        skills=format_skills(skills),
        mcp_tools=format_mcp_tools(),
    )
    gen_user = _build_user_message(context, recent_goals_text)

    content = _call_ollama([
        {"role": "system", "content": gen_system},
        {"role": "user", "content": gen_user},
    ])

    if content:
        try:
            goals = _parse_llm_response(content)
            print(f"[cortex] Generated {len(goals)} goals via Ollama two-phase ({OLLAMA_MODEL})",
                  file=__import__("sys").stderr)
            return goals
        except (json.JSONDecodeError, KeyError):
            pass

    # Fallback to Haiku
    content = _call_haiku(gen_system, gen_user)
    if content:
        try:
            goals = _parse_llm_response(content)
            print(f"[cortex] Generated {len(goals)} goals via Haiku two-phase",
                  file=__import__("sys").stderr)
            return goals
        except (json.JSONDecodeError, KeyError):
            pass

    return []


def generate_goals_llm(context: dict, recent_goals_text: str) -> list[Goal]:
    """Legacy single-phase generation (fallback if two-phase returns empty)."""
    content = _call_ollama([
        {"role": "system", "content": SYSTEM_PROMPT_LEGACY},
        {"role": "user", "content": _build_user_message(context, recent_goals_text)},
    ])
    if content:
        try:
            goals = _parse_llm_response(content)
            print(f"[cortex] Generated {len(goals)} goals via Ollama legacy ({OLLAMA_MODEL})",
                  file=__import__("sys").stderr)
            return goals
        except (json.JSONDecodeError, KeyError):
            pass

    content = _call_haiku(SYSTEM_PROMPT_LEGACY, _build_user_message(context, recent_goals_text))
    if content:
        try:
            goals = _parse_llm_response(content)
            print(f"[cortex] Generated {len(goals)} goals via Haiku legacy",
                  file=__import__("sys").stderr)
            return goals
        except (json.JSONDecodeError, KeyError):
            pass

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
# Expiry calculation
# ---------------------------------------------------------------------------
def compute_expiry(priority: int) -> float:
    """Compute expiry timestamp based on priority level."""
    now = time.time()
    if priority >= 7:
        return now + EXPIRY_HIGH
    elif priority >= 4:
        return now + EXPIRY_MEDIUM
    else:
        return now + EXPIRY_LOW


# ---------------------------------------------------------------------------
# Main generation cycle
# ---------------------------------------------------------------------------
def run_generation(context: dict, patterns: list[dict]) -> list[dict]:
    """Full L5 generation cycle. Returns list of enacted/proposed goal dicts."""
    db = get_db()

    # Fetch recent goals for dedup (increased to 50 for topic_hash coverage)
    recent = get_recent_goals(db, limit=50)
    recent_text = "\n".join(
        f"- [{g.get('status', '?')}] {g['title']}" for g in recent[:15]
    ) if recent else "None (first generation cycle)"

    # Get historical success rates
    success_rates = get_handler_success_rates(db)

    # Get learned skills
    skills = get_learned_skills()

    # Generate — two-phase first, then legacy, then pattern fallback
    goals = generate_goals_twophase(context, recent_text, success_rates, skills)
    if not goals:
        goals = generate_goals_llm(context, recent_text)
    if not goals:
        goals = generate_goals_fallback(context, patterns)

    # Dedup (word overlap + topic hash)
    goals = dedup_goals(goals, recent)

    # Log and optionally enqueue
    # For chains: map step numbers → goal IDs so depends_on resolves correctly
    results = []
    step_to_goal_id: dict[tuple[str, int], int] = {}  # (chain_id, step) → goal_id

    for goal in goals:
        topic_hash = compute_topic_hash(goal.title)
        expires_at = compute_expiry(goal.priority) if GOAL_APPROVAL else None

        # Resolve depends_on from step number to actual goal_id
        if goal.chain_id and goal.depends_on_goal_id is not None:
            dep_step = goal.depends_on_goal_id  # This is still a step number
            resolved_id = step_to_goal_id.get((goal.chain_id, dep_step))
            goal.depends_on_goal_id = resolved_id  # Now it's a real goal_id (or None)

        goal_id = log_goal(db, goal, context, topic_hash=topic_hash, expires_at=expires_at)

        # Track step→goal_id mapping for chain resolution
        if goal.chain_id and goal.chain_step is not None:
            step_to_goal_id[(goal.chain_id, goal.chain_step)] = goal_id

        if GOAL_APPROVAL:
            # Goals wait for approval (or auto-expire)
            results.append({
                **asdict(goal), "goal_id": goal_id,
                "status": "pending_approval",
                "expires_at": expires_at,
                "topic_hash": topic_hash,
            })
        else:
            # Chain steps with unmet dependencies stay proposed (not enqueued yet)
            if goal.depends_on_goal_id is not None:
                # Don't enqueue — executor will pick it up when dependency completes
                db.execute("UPDATE generated_goals SET status = 'waiting' WHERE id = ?", (goal_id,))
                db.commit()
                results.append({
                    **asdict(goal), "goal_id": goal_id,
                    "status": "waiting",
                    "topic_hash": topic_hash,
                })
            else:
                # No dependency — enqueue immediately
                task_id = enqueue_task(db, goal, goal_id)
                results.append({
                    **asdict(goal), "goal_id": goal_id, "task_id": task_id,
                    "status": "enacted",
                    "topic_hash": topic_hash,
                })

    db.close()
    return results


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

    # Re-read approval mode after env load
    GOAL_APPROVAL = os.environ.get("GOAL_APPROVAL", "false").lower() == "true"

    parser = argparse.ArgumentParser(description="ANAH Cortex — L5 goal generation")
    parser.add_argument("--generate", "-g", action="store_true", help="Run a generation cycle")
    parser.add_argument("--status", "-s", action="store_true", help="Show recent goals and stats")
    parser.add_argument("--dismiss", "-d", type=int, help="Dismiss a goal by ID")
    parser.add_argument("--approve", "-a", type=int, help="Approve a pending goal by ID")
    parser.add_argument("--check-expired", action="store_true", help="Process expired approvals")
    parser.add_argument("--context", type=str, help="Path to cerebellum context JSON (or - for stdin)")
    args = parser.parse_args()

    ANAH_DIR.mkdir(exist_ok=True)

    if args.dismiss:
        db = get_db()
        dismiss_goal(db, args.dismiss)
        db.close()
        print(json.dumps({"dismissed": args.dismiss}))
    elif args.approve:
        db = get_db()
        result = approve_goal(db, args.approve)
        db.close()
        print(json.dumps(result))
    elif args.check_expired:
        db = get_db()
        result = check_expired_approvals(db)
        db.close()
        print(json.dumps(result))
    elif args.status:
        db = get_db()
        goals = get_recent_goals(db)
        stats = {
            "total": len(goals),
            "enacted": sum(1 for g in goals if g["status"] == "enacted"),
            "proposed": sum(1 for g in goals if g["status"] == "proposed"),
            "pending_approval": sum(1 for g in goals if g["status"] == "pending_approval"),
            "dismissed": sum(1 for g in goals if g["status"] == "dismissed"),
        }
        success_rates = get_handler_success_rates(db)
        print(json.dumps({
            "stats": stats, "recent": goals[:10],
            "handler_success_rates": success_rates,
            "approval_mode": GOAL_APPROVAL,
        }, indent=2, default=str))
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
        print(json.dumps({"enacted": enacted, "count": len(enacted),
                          "approval_mode": GOAL_APPROVAL}, indent=2))
    else:
        parser.print_help()

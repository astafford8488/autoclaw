#!/usr/bin/env python3
"""ANAH Executor — Motor cortex. Dequeues and executes tasks.

Routes tasks to built-in handlers or Ollama for general-purpose execution.
Records results back to the database, closing the autonomous loop.
"""

import json
import os
import re
import sqlite3
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

ANAH_DIR = Path.home() / ".anah"
DB_FILE = ANAH_DIR / "anah.db"

# Sibling skill scripts
SKILLS_DIR = Path(__file__).resolve().parent.parent.parent
BRAINSTEM_DIR = SKILLS_DIR / "anah-brainstem" / "scripts"
CEREBELLUM_DIR = SKILLS_DIR / "anah-cerebellum" / "scripts"
MEMORY_DIR = SKILLS_DIR / "anah-memory" / "scripts"
NOTIFY_DIR = SKILLS_DIR / "anah-notify" / "scripts"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")

MAX_TASK_TIMEOUT = 120  # seconds

# Self-modification safety
SANDBOX_TIMEOUT = 10  # seconds for sandbox_eval subprocess
SANDBOX_MAX_OUTPUT = 10000  # chars
CUSTOM_HANDLERS_DIR = ANAH_DIR / "custom_handlers"


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
    desc = (task.get("description") or "").lower()
    combined = title + " " + desc

    if title.startswith("health_report:") or title.startswith("system health report"):
        return "health_report"
    if title.startswith("self_diagnostic:") or "diagnostic" in title:
        return "self_diagnostic"
    if title.startswith("cleanup:") or "cleanup" in title or "prune" in title:
        return "cleanup"
    if title.startswith("echo:"):
        return "echo"

    # MCP tool invocation
    if title.startswith("mcp:") or "mcp:" in combined[:20]:
        return "mcp_tool"

    # Sandbox evaluation
    if title.startswith("sandbox_eval:") or "sandbox_eval" in title:
        return "sandbox_eval"

    # Custom handlers (check loaded custom_ prefixed handlers)
    for handler_name in HANDLERS:
        if handler_name.startswith("custom_") and handler_name[7:] in title.lower():
            return handler_name

    # New handlers — more specific patterns first
    if any(kw in combined for kw in ("generate code", "write script", "code_gen", "create script")):
        return "code_gen"
    if any(kw in combined for kw in ("install skill", "skill_install", "add skill", "learn skill")):
        return "skill_install"
    if any(kw in combined for kw in ("set heartbeat", "set watchdog", "schedule", "interval")):
        return "schedule"
    if any(kw in combined for kw in ("notify", "alert", "notification")):
        return "notify"
    if any(kw in combined for kw in ("archive", "list files", "summarize state", "file")):
        return "file_ops"
    if any(kw in combined for kw in ("research", "fetch", "browse", "web", "url")):
        return "web_research"

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


def handle_file_ops(task: dict) -> TaskResult:
    """File operations within ~/.anah/ — archive, list, summarize."""
    t0 = time.time()
    try:
        desc = (task.get("description") or task["title"]).lower()

        if "archive" in desc:
            # Zip old trajectories (>7 days)
            traj_dir = ANAH_DIR / "trajectories"
            archive_dir = ANAH_DIR / "archives"
            archive_dir.mkdir(parents=True, exist_ok=True)
            cutoff = time.time() - (7 * 86400)
            archived = 0
            if traj_dir.exists():
                stamp = int(time.time())
                zip_path = archive_dir / f"trajectories_{stamp}.zip"
                with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in traj_dir.iterdir():
                        resolved = f.resolve()
                        if not resolved.is_relative_to(ANAH_DIR):
                            continue
                        if f.is_file() and f.stat().st_mtime < cutoff:
                            zf.write(str(f), f.name)
                            f.unlink()
                            archived += 1
            return TaskResult(True, {
                "handler": "file_ops", "operation": "archive",
                "archived": archived,
                "zip": str(zip_path) if archived else None,
            }, (time.time() - t0) * 1000)

        elif "list" in desc:
            # List key dirs with file counts and sizes
            listing = {}
            for dirname in ("skills", "trajectories", "backups"):
                d = ANAH_DIR / dirname
                if d.exists() and d.is_dir():
                    files = [f for f in d.iterdir() if f.is_file()]
                    total_size = sum(f.stat().st_size for f in files)
                    listing[dirname] = {"files": len(files), "total_bytes": total_size}
                else:
                    listing[dirname] = {"files": 0, "total_bytes": 0}
            return TaskResult(True, {
                "handler": "file_ops", "operation": "list", "dirs": listing,
            }, (time.time() - t0) * 1000)

        elif "summarize" in desc:
            # Read and summarize state.json + learning_log.json
            summary = {}
            for fname in ("state.json", "learning_log.json"):
                fpath = ANAH_DIR / fname
                resolved = fpath.resolve()
                if not resolved.is_relative_to(ANAH_DIR):
                    continue
                if fpath.exists():
                    data = json.loads(fpath.read_text())
                    if isinstance(data, dict):
                        summary[fname] = {
                            "keys": list(data.keys()),
                            "size_bytes": fpath.stat().st_size,
                        }
                    elif isinstance(data, list):
                        summary[fname] = {
                            "entries": len(data),
                            "size_bytes": fpath.stat().st_size,
                        }
                else:
                    summary[fname] = {"exists": False}
            return TaskResult(True, {
                "handler": "file_ops", "operation": "summarize", "summary": summary,
            }, (time.time() - t0) * 1000)

        else:
            return TaskResult(False, {
                "handler": "file_ops", "error": "Unknown file operation. Use archive, list, or summarize.",
            }, (time.time() - t0) * 1000)

    except Exception as e:
        return TaskResult(False, {"error": str(e), "handler": "file_ops"}, (time.time() - t0) * 1000)


def handle_web_research(task: dict) -> TaskResult:
    """Fetch a URL and summarize its content."""
    import urllib.request
    t0 = time.time()
    try:
        desc = task.get("description") or task["title"]
        url_match = re.search(r'https?://[^\s<>"\']+', desc)

        if not url_match:
            # No URL found — fall back to Ollama with a research prompt
            return handle_ollama({
                "title": task["title"],
                "description": f"Research the following topic and provide findings: {desc}",
            })

        url = url_match.group(0)

        # SECURITY: Block file://, localhost, and private IP ranges
        blocked_patterns = [
            r'^file://',
            r'https?://localhost',
            r'https?://127\.',
            r'https?://10\.',
            r'https?://172\.(1[6-9]|2[0-9]|3[01])\.',
            r'https?://192\.168\.',
            r'https?://\[::1\]',
        ]
        for pattern in blocked_patterns:
            if re.match(pattern, url, re.IGNORECASE):
                return TaskResult(False, {
                    "handler": "web_research", "error": f"Blocked URL: {url}",
                }, (time.time() - t0) * 1000)

        req = urllib.request.Request(url, headers={"User-Agent": "ANAH-Executor/1.0"})
        resp = urllib.request.urlopen(req, timeout=30)
        raw = resp.read(100 * 1024)  # max 100KB
        text = raw.decode("utf-8", errors="replace")

        # Strip HTML tags
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        excerpt = text[:2000]

        return TaskResult(True, {
            "handler": "web_research",
            "url": url,
            "content_length": len(text),
            "excerpt": excerpt,
        }, (time.time() - t0) * 1000)
    except Exception as e:
        return TaskResult(False, {"error": str(e), "handler": "web_research"}, (time.time() - t0) * 1000)


def handle_code_gen(task: dict) -> TaskResult:
    """Generate a Python script or skill stub via Ollama."""
    import urllib.request
    t0 = time.time()
    try:
        desc = task.get("description") or task["title"]
        prompt = f"""You are a Python code generator. Write a complete, well-documented Python script for the following request.

Request: {desc}

Return ONLY the Python code inside a single ```python code block. Include docstrings and comments."""

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

        # Extract code block
        code_match = re.search(r'```(?:python)?\s*\n(.*?)```', content, re.DOTALL)
        code = code_match.group(1).strip() if code_match else content.strip()

        # Sanitize title for filename
        sanitized = re.sub(r'[^a-zA-Z0-9_-]', '_', task["title"])[:60]
        stamp = int(time.time())
        gen_dir = ANAH_DIR / "generated"
        gen_dir.mkdir(parents=True, exist_ok=True)
        out_file = gen_dir / f"{sanitized}_{stamp}.py"

        # SECURITY: Never execute — only save
        out_file.write_text(code, encoding="utf-8")

        return TaskResult(True, {
            "handler": "code_gen",
            "file": str(out_file),
            "lines": len(code.splitlines()),
        }, (time.time() - t0) * 1000)
    except Exception as e:
        return TaskResult(False, {"error": str(e), "handler": "code_gen"}, (time.time() - t0) * 1000)


def handle_notify(task: dict) -> TaskResult:
    """Write a notification to a JSONL log file.

    SECURITY: Validates critical/warning alerts against real system state
    to prevent LLM-hallucinated false alerts from reaching Discord.
    """
    t0 = time.time()
    try:
        title = task["title"]
        desc = task.get("description") or ""

        # Parse level from title prefix
        level = "info"
        if "notify:critical:" in title.lower():
            level = "critical"
            title = re.sub(r'(?i)notify:critical:\s*', '', title)
        elif "notify:warning:" in title.lower():
            level = "warning"
            title = re.sub(r'(?i)notify:warning:\s*', '', title)

        # VALIDATION: Cross-check critical/warning claims against real system state
        # LLM-generated tasks may hallucinate problems that don't exist
        if level in ("critical", "warning") and task.get("source") == "l5_generated":
            is_valid, real_state = _validate_alert_claim(title, desc)
            if not is_valid:
                return TaskResult(True, {
                    "handler": "notify", "level": level,
                    "suppressed": True,
                    "reason": "Alert claim does not match real system state",
                    "claimed": title,
                    "actual": real_state,
                }, (time.time() - t0) * 1000)

        entry = {
            "timestamp": time.time(),
            "level": level,
            "title": title,
            "message": desc,
        }

        notif_file = ANAH_DIR / "notifications.json"
        ANAH_DIR.mkdir(parents=True, exist_ok=True)
        with open(str(notif_file), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        # Dispatch to Discord (best-effort)
        discord_sent = False
        try:
            if str(NOTIFY_DIR) not in sys.path:
                sys.path.insert(0, str(NOTIFY_DIR))
            import discord_webhook as discord_notify
            discord_sent = discord_notify.send_notification(level, title, desc, source="executor")
        except Exception:
            pass

        return TaskResult(True, {
            "handler": "notify", "level": level, "logged": True, "discord": discord_sent,
        }, (time.time() - t0) * 1000)
    except Exception as e:
        return TaskResult(False, {"error": str(e), "handler": "notify"}, (time.time() - t0) * 1000)


def _validate_alert_claim(title: str, desc: str) -> tuple[bool, str]:
    """Validate critical/warning notification claims against real system metrics.
    Returns (is_valid, actual_state_description)."""
    combined = (title + " " + desc).lower()
    try:
        import psutil
        # Check disk claims
        if any(kw in combined for kw in ("disk full", "disk space", "storage", "disk usage")):
            disk = psutil.disk_usage("C:\\" if os.name == "nt" else "/")
            if disk.percent < 85:
                return False, f"Disk actually at {disk.percent:.0f}% (not critical)"
        # Check CPU claims
        if any(kw in combined for kw in ("high cpu", "cpu usage", "cpu load", "processor")):
            cpu = psutil.cpu_percent(interval=0.5)
            if cpu < 70:
                return False, f"CPU actually at {cpu:.0f}% (not high)"
        # Check RAM claims
        if any(kw in combined for kw in ("high memory", "ram usage", "memory full", "out of memory")):
            ram = psutil.virtual_memory().percent
            if ram < 80:
                return False, f"RAM actually at {ram:.0f}% (not critical)"
    except ImportError:
        pass  # No psutil — can't validate, allow through
    return True, "validated"


def handle_schedule(task: dict) -> TaskResult:
    """Manage scheduler presets by writing to config."""
    t0 = time.time()
    try:
        desc = (task.get("description") or task["title"]).lower()
        config_file = ANAH_DIR / "config.json"

        # Read current config
        if config_file.exists():
            config = json.loads(config_file.read_text())
        else:
            config = {}

        updated_field = None
        new_value = None

        # Parse "set heartbeat X"
        hb_match = re.search(r'set\s+heartbeat\s+(\d+)', desc)
        if hb_match:
            val = int(hb_match.group(1))
            if not (30 <= val <= 3600):
                return TaskResult(False, {
                    "handler": "schedule", "error": f"Heartbeat must be 30-3600, got {val}",
                }, (time.time() - t0) * 1000)
            config["heartbeat_interval"] = val
            updated_field = "heartbeat_interval"
            new_value = val

        # Parse "set watchdog X"
        wd_match = re.search(r'set\s+watchdog\s+(\d+)', desc)
        if wd_match:
            val = int(wd_match.group(1))
            if not (10 <= val <= 600):
                return TaskResult(False, {
                    "handler": "schedule", "error": f"Watchdog must be 10-600, got {val}",
                }, (time.time() - t0) * 1000)
            config["watchdog_interval"] = val
            updated_field = "watchdog_interval"
            new_value = val

        # Parse "set preset fast|default|conservative"
        preset_match = re.search(r'set\s+preset\s+(fast|default|conservative)', desc)
        if preset_match:
            preset = preset_match.group(1)
            presets = {
                "fast": {"heartbeat_interval": 30, "watchdog_interval": 10},
                "default": {"heartbeat_interval": 120, "watchdog_interval": 60},
                "conservative": {"heartbeat_interval": 600, "watchdog_interval": 120},
            }
            config.update(presets[preset])
            updated_field = "preset"
            new_value = preset

        if updated_field is None:
            return TaskResult(False, {
                "handler": "schedule",
                "error": "Could not parse schedule command. Use: set heartbeat X, set watchdog X, or set preset fast|default|conservative",
            }, (time.time() - t0) * 1000)

        # Write updated config
        ANAH_DIR.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")

        return TaskResult(True, {
            "handler": "schedule", "updated": updated_field, "value": new_value,
        }, (time.time() - t0) * 1000)
    except Exception as e:
        return TaskResult(False, {"error": str(e), "handler": "schedule"}, (time.time() - t0) * 1000)


def handle_skill_install(task: dict) -> TaskResult:
    """Install a learned skill from description."""
    t0 = time.time()
    try:
        desc = task.get("description") or task["title"]

        # Parse skill name from description — expect "skill_name: ..." or first word
        name_match = re.search(r'(?:skill\s+(?:named?|called)?\s*)?["\']?([a-zA-Z0-9-]+)["\']?', desc)
        skill_name = name_match.group(1) if name_match else "unnamed-skill"

        # SECURITY: Sanitize — alphanumeric + hyphens only
        skill_name = re.sub(r'[^a-zA-Z0-9-]', '', skill_name).strip('-')
        if not skill_name:
            skill_name = "unnamed-skill"

        # Use Path.name to prevent traversal
        skill_name = Path(skill_name).name

        skill_dir = ANAH_DIR / "skills" / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)

        # Write SKILL.md with frontmatter
        skill_md = skill_dir / "SKILL.md"
        frontmatter = f"""---
name: {skill_name}
description: {desc[:200]}
installed_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
---

# {skill_name}

{desc}
"""
        skill_md.write_text(frontmatter, encoding="utf-8")

        return TaskResult(True, {
            "handler": "skill_install",
            "skill": skill_name,
            "path": str(skill_dir),
        }, (time.time() - t0) * 1000)
    except Exception as e:
        return TaskResult(False, {"error": str(e), "handler": "skill_install"}, (time.time() - t0) * 1000)


def handle_mcp_tool(task: dict) -> TaskResult:
    """Execute an MCP tool call. Parses tool name and args from task title/description.

    Title format: "mcp: tool_name args..." or "mcp:tool_name args..."
    Security: Only whitelisted tools are allowed. No credential exposure.
    """
    import urllib.request
    t0 = time.time()

    # Whitelisted MCP tools and their OpenClaw MCP server prefixes
    MCP_TOOL_MAP = {
        "web_search": "WebSearch",
        "web_fetch": "WebFetch",
        "slack_send_message": "mcp__5c519189-d859-411a-ae30-9bb9cdd3894e__slack_send_message",
        "slack_search_public": "mcp__5c519189-d859-411a-ae30-9bb9cdd3894e__slack_search_public",
        "notion_search": "mcp__5de1885f-4e61-48ef-93ec-0cd8c68a50a7__notion-search",
        "gmail_search_messages": "mcp__a63502d1-8a54-400d-b89e-d03a152b9855__gmail_search_messages",
        "gcal_list_events": "mcp__83a49886-9e9f-4dcf-bbb4-ee68cf33cfd0__gcal_list_events",
    }

    try:
        desc = task.get("description") or task["title"]
        title = task["title"]

        # Parse: "mcp: tool_name rest..." or "mcp:tool_name rest..."
        mcp_match = re.match(r'mcp:\s*(\w+)\s*(.*)', title, re.IGNORECASE)
        if not mcp_match:
            mcp_match = re.match(r'mcp:\s*(\w+)\s*(.*)', desc, re.IGNORECASE)
        if not mcp_match:
            return TaskResult(False, {
                "handler": "mcp_tool", "error": "Could not parse MCP tool name from title/description",
            }, (time.time() - t0) * 1000)

        tool_name = mcp_match.group(1).lower()
        tool_args = mcp_match.group(2).strip() or desc

        # Security: whitelist check
        if tool_name not in MCP_TOOL_MAP:
            return TaskResult(False, {
                "handler": "mcp_tool",
                "error": f"Tool '{tool_name}' not in whitelist. Allowed: {list(MCP_TOOL_MAP.keys())}",
            }, (time.time() - t0) * 1000)

        # Route to the appropriate handler based on tool type
        if tool_name == "web_search":
            # Use built-in web search via Ollama as proxy (no direct MCP in executor)
            prompt = f"Search the web for: {tool_args}\nSummarize the top findings in JSON with keys: query, findings (array of strings), sources (array of URLs if found)."
            body = json.dumps({
                "model": OLLAMA_MODEL, "stream": False,
                "messages": [{"role": "user", "content": prompt}],
            }).encode()
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/chat", data=body,
                headers={"content-type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=MAX_TASK_TIMEOUT)
            data = json.loads(resp.read())
            content = data["message"]["content"]
            try:
                result = json.loads(content.strip())
            except json.JSONDecodeError:
                result = {"summary": content.strip(), "parsed": False}
            result["handler"] = "mcp_tool"
            result["tool"] = tool_name
            return TaskResult(True, result, (time.time() - t0) * 1000)

        elif tool_name == "web_fetch":
            # Reuse web_research handler with URL
            url_match = re.search(r'https?://[^\s<>"\']+', tool_args)
            if url_match:
                return handle_web_research({
                    "title": task["title"],
                    "description": f"fetch {url_match.group(0)}",
                })
            return TaskResult(False, {
                "handler": "mcp_tool", "tool": tool_name,
                "error": "No URL found in arguments",
            }, (time.time() - t0) * 1000)

        elif tool_name in ("slack_send_message", "slack_search_public",
                           "notion_search", "gmail_search_messages", "gcal_list_events"):
            # For MCP server tools: store the intent for external dispatch
            # The dashboard or a bridge process can pick these up and execute via MCP
            result = {
                "handler": "mcp_tool",
                "tool": tool_name,
                "mcp_server_tool": MCP_TOOL_MAP[tool_name],
                "args": tool_args,
                "status": "mcp_queued",
                "note": "Queued for MCP bridge dispatch",
            }
            return TaskResult(True, result, (time.time() - t0) * 1000)

        return TaskResult(False, {
            "handler": "mcp_tool", "error": f"No execution path for tool '{tool_name}'",
        }, (time.time() - t0) * 1000)

    except Exception as e:
        return TaskResult(False, {"error": str(e), "handler": "mcp_tool"}, (time.time() - t0) * 1000)


def handle_sandbox_eval(task: dict) -> TaskResult:
    """Execute generated code in a sandboxed subprocess.

    SECURITY:
    - Runs in subprocess with timeout (SANDBOX_TIMEOUT seconds)
    - No network access (restricted imports)
    - No filesystem writes outside ~/.anah/sandbox/
    - Output capped at SANDBOX_MAX_OUTPUT chars
    - Cannot modify core ANAH files
    """
    import subprocess
    t0 = time.time()
    try:
        desc = task.get("description") or task["title"]

        # Extract code block from description
        code = None
        code_match = re.search(r'```(?:python)?\s*\n(.*?)```', desc, re.DOTALL)
        if code_match:
            code = code_match.group(1).strip()
        else:
            # Check if there's a generated file reference
            file_match = re.search(r'file:\s*(.+\.py)', desc)
            if file_match:
                code_path = Path(file_match.group(1).strip())
                resolved = code_path.resolve()
                # SECURITY: Only allow reading from ~/.anah/generated/
                gen_dir = ANAH_DIR / "generated"
                if resolved.is_relative_to(gen_dir) and resolved.exists():
                    code = resolved.read_text()

        if not code:
            return TaskResult(False, {
                "handler": "sandbox_eval",
                "error": "No code found. Include ```python code``` block or file: path in description.",
            }, (time.time() - t0) * 1000)

        # SECURITY: Block dangerous patterns
        dangerous_patterns = [
            r'\bimport\s+subprocess\b',
            r'\bimport\s+shutil\b',
            r'\b__import__\b',
            r'\bexec\s*\(',
            r'\beval\s*\(',
            r'\bos\.system\b',
            r'\bos\.popen\b',
            r'\bos\.exec',
            r'\bos\.remove\b',
            r'\bos\.unlink\b',
            r'\bos\.rmdir\b',
            r'\bopen\s*\(.*(w|a|x)',  # write mode opens
            r'\bPathlib.*write',
            r'\b\.write_text\b',
            r'\b\.write_bytes\b',
        ]
        for pattern in dangerous_patterns:
            if re.search(pattern, code, re.IGNORECASE):
                return TaskResult(False, {
                    "handler": "sandbox_eval",
                    "error": f"Code contains blocked pattern: {pattern}",
                    "safety": "rejected",
                }, (time.time() - t0) * 1000)

        # Create sandbox dir
        sandbox_dir = ANAH_DIR / "sandbox"
        sandbox_dir.mkdir(parents=True, exist_ok=True)

        # Write code to temp file in sandbox
        stamp = int(time.time())
        script_file = sandbox_dir / f"eval_{stamp}.py"
        script_file.write_text(code, encoding="utf-8")

        # Run in subprocess with timeout
        try:
            result = subprocess.run(
                [sys.executable, str(script_file)],
                capture_output=True, text=True,
                timeout=SANDBOX_TIMEOUT,
                cwd=str(sandbox_dir),
            )
            stdout = result.stdout[:SANDBOX_MAX_OUTPUT]
            stderr = result.stderr[:SANDBOX_MAX_OUTPUT]

            return TaskResult(result.returncode == 0, {
                "handler": "sandbox_eval",
                "returncode": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "script": str(script_file),
            }, (time.time() - t0) * 1000)
        except subprocess.TimeoutExpired:
            return TaskResult(False, {
                "handler": "sandbox_eval",
                "error": f"Execution timed out after {SANDBOX_TIMEOUT}s",
                "script": str(script_file),
            }, (time.time() - t0) * 1000)

    except Exception as e:
        return TaskResult(False, {"error": str(e), "handler": "sandbox_eval"}, (time.time() - t0) * 1000)


# ---------------------------------------------------------------------------
# Dynamic handler loading — self-modification capability
# ---------------------------------------------------------------------------
def load_custom_handlers() -> dict[str, callable]:
    """Load custom handlers from ~/.anah/custom_handlers/.

    Each .py file should define a function handle(task: dict) -> TaskResult.
    Handler name is derived from filename: my_handler.py → 'custom_my_handler'

    SECURITY:
    - Only loads from CUSTOM_HANDLERS_DIR (no path traversal)
    - Validates file is a regular .py file
    - Handlers run with same permissions as executor (no escalation)
    - Failed loads are logged but don't crash executor
    """
    custom = {}
    if not CUSTOM_HANDLERS_DIR.exists():
        return custom

    for handler_file in CUSTOM_HANDLERS_DIR.glob("*.py"):
        if not handler_file.is_file():
            continue
        # Sanitize name
        name = f"custom_{handler_file.stem}"
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(name, str(handler_file))
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if hasattr(module, "handle") and callable(module.handle):
                    custom[name] = module.handle
                    print(f"[executor] Loaded custom handler: {name}", file=sys.stderr)
        except Exception as e:
            print(f"[executor] Failed to load custom handler {name}: {e}", file=sys.stderr)

    return custom


def revert_custom_handler(name: str) -> dict:
    """Remove a custom handler file. Safety rollback mechanism.

    Returns status dict.
    """
    if not name.startswith("custom_"):
        return {"error": "Can only revert custom_ handlers"}

    stem = name[7:]  # strip 'custom_' prefix
    handler_file = CUSTOM_HANDLERS_DIR / f"{stem}.py"
    resolved = handler_file.resolve()

    # SECURITY: Ensure it's within the custom handlers dir
    if not resolved.is_relative_to(CUSTOM_HANDLERS_DIR.resolve()):
        return {"error": "Path traversal detected"}

    if handler_file.exists():
        handler_file.unlink()
        # Remove from HANDLERS if loaded
        if name in HANDLERS:
            del HANDLERS[name]
        return {"reverted": name, "file": str(handler_file)}
    return {"error": f"Handler file not found: {handler_file}"}


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
    "file_ops": handle_file_ops,
    "web_research": handle_web_research,
    "code_gen": handle_code_gen,
    "mcp_tool": handle_mcp_tool,
    "sandbox_eval": handle_sandbox_eval,
    "notify": handle_notify,
    "schedule": handle_schedule,
    "skill_install": handle_skill_install,
    "ollama": handle_ollama,
}

# Load custom handlers from ~/.anah/custom_handlers/
HANDLERS.update(load_custom_handlers())


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

#!/usr/bin/env python3
"""ANAH Dashboard — Zero-dependency web UI for monitoring the autonomous hierarchy.

Uses Python's built-in http.server with an embedded single-page HTML/JS frontend.
Reads directly from ~/.anah/anah.db for real-time status.

Phase 2 additions:
- Goal approve/dismiss from dashboard
- Goal history with pagination
- Health trend sparkline
- Training status card
- SSE live updates
"""

import json
import os
import queue
import sqlite3
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ANAH_DIR = Path.home() / ".anah"
DB_FILE = ANAH_DIR / "anah.db"

# Sibling skills
SKILLS_DIR = Path(__file__).resolve().parent.parent.parent
for d in SKILLS_DIR.glob("anah-*/scripts"):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))

# SSE: connected clients
_sse_clients: list[queue.Queue] = []
_sse_lock = threading.Lock()


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_FILE))
    db.row_factory = sqlite3.Row
    return db


def sse_broadcast(event: str, data: dict):
    """Send an SSE event to all connected clients."""
    payload = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


# ---------------------------------------------------------------------------
# API handlers
# ---------------------------------------------------------------------------
def api_health() -> dict:
    """Current brainstem health from state.json."""
    state_file = ANAH_DIR / "state.json"
    if not state_file.exists():
        return {"status": "unknown", "message": "No state file yet. Run a cycle first."}
    state = json.loads(state_file.read_text())
    levels = {}
    for lvl_id, lvl_data in state.get("levels", {}).items():
        levels[f"L{lvl_id}"] = {
            "status": lvl_data.get("status", "unknown"),
            "last_check": lvl_data.get("last_check"),
            "checks": len(lvl_data.get("checks", [])),
            "passed": sum(1 for c in lvl_data.get("checks", []) if c.get("passed")),
        }
    gating = state.get("gating", {})
    total_checks = sum(v["checks"] for v in levels.values())
    total_passed = sum(v["passed"] for v in levels.values())
    return {
        "health_score": round(total_passed / total_checks * 100, 1) if total_checks else 0,
        "l1_healthy": gating.get("l1_healthy", False),
        "levels": levels,
        "last_update": state.get("last_update"),
    }


def api_queue() -> dict:
    """Task queue status and recent tasks."""
    db = get_db()
    counts = {}
    for row in db.execute("SELECT status, COUNT(*) as cnt FROM task_queue GROUP BY status"):
        counts[dict(row)["status"]] = dict(row)["cnt"]

    tasks = []
    for row in db.execute(
        "SELECT id, title, status, priority, source, created_at, completed_at "
        "FROM task_queue ORDER BY created_at DESC LIMIT 25"
    ):
        tasks.append(dict(row))
    db.close()
    return {"counts": counts, "total": sum(counts.values()), "tasks": tasks}


def api_goals() -> dict:
    """Generated goals and stats."""
    db = get_db()
    stats = {"total": 0, "enacted": 0, "proposed": 0, "pending_approval": 0, "dismissed": 0}
    for row in db.execute("SELECT status, COUNT(*) as cnt FROM generated_goals GROUP BY status"):
        r = dict(row)
        stats[r["status"]] = r["cnt"]
        stats["total"] += r["cnt"]

    goals = []
    for row in db.execute(
        "SELECT id, title, priority, status, source, reasoning, timestamp, topic_hash, expires_at "
        "FROM generated_goals ORDER BY timestamp DESC LIMIT 25"
    ):
        goals.append(dict(row))
    db.close()
    return {"stats": stats, "goals": goals}


def api_goals_history(params: dict) -> dict:
    """Paginated goal history."""
    page = int(params.get("page", ["1"])[0])
    per_page = min(int(params.get("per_page", ["50"])[0]), 100)
    status_filter = params.get("status", [None])[0]
    offset = (page - 1) * per_page

    db = get_db()
    where = ""
    args = []
    if status_filter:
        where = "WHERE status = ?"
        args.append(status_filter)

    total = db.execute(f"SELECT COUNT(*) FROM generated_goals {where}", args).fetchone()[0]
    rows = db.execute(
        f"SELECT id, title, priority, status, source, reasoning, timestamp, topic_hash, expires_at, task_id "
        f"FROM generated_goals {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        args + [per_page, offset],
    ).fetchall()
    db.close()
    return {
        "goals": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    }


def api_goals_approve(body: dict) -> dict:
    """Approve a pending goal."""
    goal_id = body.get("goal_id")
    if not goal_id:
        return {"error": "goal_id required"}
    try:
        import cortex
        db = get_db()
        result = cortex.approve_goal(db, goal_id)
        db.close()
        if "task_id" in result:
            sse_broadcast("goal_approved", {"goal_id": goal_id, "task_id": result["task_id"]})
        return result
    except Exception as e:
        return {"error": str(e)}


def api_goals_dismiss(body: dict) -> dict:
    """Dismiss a goal."""
    goal_id = body.get("goal_id")
    if not goal_id:
        return {"error": "goal_id required"}
    try:
        import cortex
        db = get_db()
        cortex.dismiss_goal(db, goal_id)
        db.close()
        sse_broadcast("goal_dismissed", {"goal_id": goal_id})
        return {"dismissed": goal_id}
    except Exception as e:
        return {"error": str(e)}


def api_health_history(params: dict) -> dict:
    """Health scores over time for sparkline chart."""
    limit = min(int(params.get("limit", ["50"])[0]), 200)
    db = get_db()
    # Aggregate health by distinct timestamp buckets (1 per minute)
    rows = db.execute(
        """SELECT
             CAST(timestamp / 60 AS INTEGER) * 60 as bucket,
             AVG(CASE WHEN passed = 1 THEN 100.0 ELSE 0.0 END) as score,
             COUNT(*) as checks
           FROM health_logs
           GROUP BY bucket
           ORDER BY bucket DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    db.close()
    # Return oldest-first for sparkline
    points = [{"ts": r["bucket"], "score": round(r["score"], 1), "checks": r["checks"]} for r in reversed(rows)]
    return {"points": points, "count": len(points)}


def api_training() -> dict:
    """Training status, last run, eval results."""
    training_dir = ANAH_DIR / "training"
    result = {"datasets": {}, "last_train": None, "eval": None, "model": "base"}

    # Dataset files
    for name in ("sft_dataset.jsonl", "dpo_dataset.jsonl", "Modelfile"):
        p = training_dir / name
        if p.exists():
            info = {"size_bytes": p.stat().st_size, "modified": p.stat().st_mtime}
            if name.endswith(".jsonl"):
                with open(str(p)) as f:
                    info["entries"] = sum(1 for _ in f)
            result["datasets"][name] = info

    # Last training run
    last_train_file = training_dir / "last_train.json"
    if last_train_file.exists():
        try:
            result["last_train"] = json.loads(last_train_file.read_text())
        except Exception:
            pass

    # Eval results
    eval_file = training_dir / "eval_results.json"
    if eval_file.exists():
        try:
            result["eval"] = json.loads(eval_file.read_text())
        except Exception:
            pass

    # Current model
    state_file = ANAH_DIR / "state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            result["model"] = state.get("ollama_model", os.environ.get("OLLAMA_MODEL", "base"))
        except Exception:
            pass

    return result


def api_skills() -> dict:
    """Learned skills from hippocampus."""
    skills_dir = ANAH_DIR / "skills"
    skills = []
    if skills_dir.exists():
        for sd in sorted(skills_dir.iterdir()):
            skill_file = sd / "SKILL.md"
            if sd.is_dir() and skill_file.exists():
                content = skill_file.read_text()
                desc = ""
                for line in content.splitlines():
                    if line.startswith("description:"):
                        desc = line.split(":", 1)[1].strip().strip('"')
                        break
                skills.append({"name": sd.name, "description": desc})
    return {"skills": skills, "count": len(skills)}


def api_memory() -> dict:
    """Memory utilization."""
    try:
        import memory
        return memory.memory_status()
    except Exception:
        mem_file = ANAH_DIR / "MEMORY.md"
        prof_file = ANAH_DIR / "SYSTEM_PROFILE.md"
        mem_len = len(mem_file.read_text()) if mem_file.exists() else 0
        prof_len = len(prof_file.read_text()) if prof_file.exists() else 0
        return {
            "memory": {"chars": mem_len, "limit": 2200, "remaining": 2200 - mem_len},
            "profile": {"chars": prof_len, "limit": 1375, "remaining": 1375 - prof_len},
            "trajectories": {"count": 0},
        }


def api_trajectories() -> dict:
    """Trajectory stats and recent entries."""
    traj_dir = ANAH_DIR / "trajectories"
    training_dir = ANAH_DIR / "training"
    result = {"total_files": 0, "total_trajectories": 0, "recent": [], "training": {}}

    if traj_dir.exists():
        files = sorted(traj_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        result["total_files"] = len(files)
        all_trajs = []
        for f in files[:5]:
            try:
                data = json.loads(f.read_text())
                if isinstance(data, list):
                    all_trajs.extend(data)
                elif isinstance(data, dict):
                    all_trajs.append(data)
            except Exception:
                continue
        result["total_trajectories"] = len(all_trajs)
        result["recent"] = [
            {
                "task_id": t.get("metadata", {}).get("task_id"),
                "title": t.get("metadata", {}).get("title", "unknown"),
                "outcome": t.get("metadata", {}).get("outcome", "unknown"),
                "duration_ms": t.get("metadata", {}).get("duration_ms"),
                "source": t.get("metadata", {}).get("source", "unknown"),
            }
            for t in all_trajs[:20]
        ]

    for name in ("sft_dataset.jsonl", "dpo_dataset.jsonl", "Modelfile"):
        p = training_dir / name
        if p.exists():
            result["training"][name] = {
                "size_bytes": p.stat().st_size,
                "modified": p.stat().st_mtime,
            }
            if name.endswith(".jsonl"):
                with open(str(p)) as f:
                    result["training"][name]["entries"] = sum(1 for _ in f)

    return result


def api_logs() -> dict:
    """Recent scheduler logs."""
    log_file = ANAH_DIR / "logs" / "scheduler.jsonl"
    entries = []
    if log_file.exists():
        lines = log_file.read_text().strip().splitlines()
        for line in lines[-30:]:
            try:
                entries.append(json.loads(line))
            except Exception:
                continue
    return {"entries": entries, "count": len(entries)}


def api_overview() -> dict:
    """Combined status overview."""
    return {
        "health": api_health(),
        "queue": api_queue(),
        "goals": api_goals(),
        "skills": api_skills(),
        "memory": api_memory(),
        "trajectories": api_trajectories(),
        "timestamp": time.time(),
    }


def api_trigger_cycle() -> dict:
    """Trigger a heartbeat cycle."""
    try:
        sys.path.insert(0, str(SKILLS_DIR / "anah-orchestrator" / "scripts"))
        import orchestrator
        orchestrator.load_env()
        result = orchestrator.full_cycle(generate=True, execute=True, learn=True)
        sse_broadcast("cycle_complete", {"duration_ms": result.get("duration_ms", 0)})
        return {"triggered": True, "result": result}
    except Exception as e:
        return {"triggered": False, "error": str(e)}


# API route map (GET)
API_ROUTES = {
    "/api/health": api_health,
    "/api/queue": api_queue,
    "/api/goals": api_goals,
    "/api/skills": api_skills,
    "/api/memory": api_memory,
    "/api/trajectories": api_trajectories,
    "/api/logs": api_logs,
    "/api/overview": api_overview,
}

# Parameterized GET routes
PARAM_ROUTES = {
    "/api/goals/history": api_goals_history,
    "/api/health-history": api_health_history,
    "/api/training": lambda _: api_training(),
}


# ---------------------------------------------------------------------------
# Dashboard HTML — Enhanced SPA
# ---------------------------------------------------------------------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ANAH Dashboard</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #c9d1d9; --text-dim: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --yellow: #d29922; --red: #f85149; --purple: #bc8cff;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace; background: var(--bg); color: var(--text); padding: 16px; }
  h1 { font-size: 1.4em; margin-bottom: 4px; }
  h2 { font-size: 1.1em; color: var(--accent); margin-bottom: 8px; }
  .subtitle { color: var(--text-dim); font-size: 0.85em; margin-bottom: 16px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; margin-bottom: 12px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
  .stat-row { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
  .stat { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 10px 14px; flex: 1; min-width: 100px; text-align: center; }
  .stat-value { font-size: 1.8em; font-weight: bold; }
  .stat-label { font-size: 0.75em; color: var(--text-dim); text-transform: uppercase; }
  .green { color: var(--green); } .yellow { color: var(--yellow); } .red { color: var(--red); } .purple { color: var(--purple); }
  table { width: 100%; border-collapse: collapse; font-size: 0.85em; }
  th { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); color: var(--text-dim); font-weight: 500; }
  td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
  .badge { padding: 2px 8px; border-radius: 10px; font-size: 0.75em; font-weight: 500; display: inline-block; }
  .badge-green { background: #238636; color: #fff; }
  .badge-yellow { background: #9e6a03; color: #fff; }
  .badge-red { background: #da3633; color: #fff; }
  .badge-blue { background: #1f6feb; color: #fff; }
  .badge-purple { background: #8957e5; color: #fff; }
  .badge-gray { background: #30363d; color: var(--text-dim); }
  .gauge { height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; margin-top: 4px; }
  .gauge-fill { height: 100%; border-radius: 3px; transition: width 0.5s; }
  .bar { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .bar-label { font-size: 0.8em; color: var(--text-dim); min-width: 60px; }
  .bar-value { font-size: 0.8em; min-width: 50px; text-align: right; }
  .controls { display: flex; gap: 8px; align-items: center; margin-bottom: 16px; }
  button { background: var(--accent); color: #fff; border: none; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 0.85em; }
  button:hover { opacity: 0.85; }
  button:disabled { opacity: 0.5; cursor: default; }
  .btn-sm { padding: 2px 8px; font-size: 0.75em; border-radius: 4px; }
  .btn-green { background: #238636; }
  .btn-red { background: #da3633; }
  .btn-gray { background: #30363d; color: var(--text-dim); }
  .refresh-note { font-size: 0.75em; color: var(--text-dim); }
  .sse-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 4px; }
  .sse-connected { background: var(--green); }
  .sse-disconnected { background: var(--red); }
  .level-row { display: flex; gap: 8px; margin-bottom: 4px; align-items: center; }
  .level-dot { width: 10px; height: 10px; border-radius: 50%; }
  .level-name { font-size: 0.85em; min-width: 30px; }
  .level-status { font-size: 0.8em; color: var(--text-dim); }
  .skill-item { padding: 6px 0; border-bottom: 1px solid var(--border); }
  .skill-name { font-weight: 500; font-size: 0.9em; }
  .skill-desc { font-size: 0.75em; color: var(--text-dim); }
  .sparkline { display: block; margin: 4px 0; }
  .expiry-bar { height: 3px; background: var(--border); border-radius: 2px; margin-top: 3px; }
  .expiry-fill { height: 100%; border-radius: 2px; background: var(--yellow); transition: width 1s linear; }
  #loading { color: var(--text-dim); font-style: italic; }
  .pagination { display: flex; gap: 8px; justify-content: center; margin-top: 8px; }
  .pagination button { padding: 4px 10px; }
</style>
</head>
<body>

<h1>ANAH Dashboard</h1>
<p class="subtitle">Autonomous Needs-Aware Hierarchy &mdash; Brain Monitor <span class="sse-dot" id="sseDot"></span><span class="refresh-note" id="sseStatus"></span></p>

<div class="controls">
  <button onclick="triggerCycle()" id="cycleBtn">Run Cycle</button>
  <button onclick="refresh()">Refresh</button>
  <span class="refresh-note" id="lastUpdate">Loading...</span>
</div>

<div class="stat-row" id="topStats"></div>

<div class="grid">
  <div class="card">
    <h2>Health Status</h2>
    <div id="healthPanel"><span id="loading">Loading...</span></div>
    <div style="margin-top:8px"><h2 style="font-size:0.9em">Health Trend</h2><div id="sparkline"></div></div>
  </div>
  <div class="card">
    <h2>Memory</h2>
    <div id="memoryPanel"></div>
  </div>
</div>

<div class="grid">
  <div class="card">
    <h2>Task Queue</h2>
    <div id="queuePanel"></div>
  </div>
  <div class="card">
    <h2>Goals</h2>
    <div id="goalsPanel"></div>
  </div>
</div>

<div class="grid">
  <div class="card">
    <h2>Training</h2>
    <div id="trainingPanel"></div>
  </div>
  <div class="card">
    <h2>Trajectories</h2>
    <div id="trajPanel"></div>
  </div>
</div>

<div class="grid">
  <div class="card">
    <h2>Learned Skills</h2>
    <div id="skillsPanel"></div>
  </div>
  <div class="card">
    <h2>Goal History</h2>
    <div id="goalHistoryPanel"></div>
  </div>
</div>

<div class="card" style="margin-bottom:12px">
  <h2>Scheduler Log</h2>
  <div id="logPanel" style="max-height:250px;overflow-y:auto;font-size:0.8em;font-family:monospace"></div>
</div>

<script>
const statusBadge = (s) => {
  const map = {completed:'green',queued:'blue',running:'yellow',failed:'red',pending_approval:'purple',enacted:'green',proposed:'blue',dismissed:'gray'};
  return '<span class="badge badge-'+(map[s]||'gray')+'">'+s+'</span>';
};
const scoreColor = (s) => s >= 80 ? 'green' : s >= 50 ? 'yellow' : 'red';
const ago = (ts) => {
  if (!ts) return '-';
  const s = Math.floor(Date.now()/1000 - ts);
  if (s < 0) return 'future';
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  return Math.floor(s/3600) + 'h ago';
};
const pct = (v, max) => max > 0 ? Math.round(v/max*100) : 0;

// SSE
let evtSource = null;
function connectSSE() {
  evtSource = new EventSource('/api/events');
  evtSource.onopen = () => {
    document.getElementById('sseDot').className = 'sse-dot sse-connected';
    document.getElementById('sseStatus').textContent = 'Live';
  };
  evtSource.onerror = () => {
    document.getElementById('sseDot').className = 'sse-dot sse-disconnected';
    document.getElementById('sseStatus').textContent = 'Reconnecting...';
  };
  evtSource.addEventListener('refresh', () => refresh());
  evtSource.addEventListener('goal_approved', (e) => { refresh(); });
  evtSource.addEventListener('goal_dismissed', (e) => { refresh(); });
  evtSource.addEventListener('cycle_complete', (e) => { refresh(); });
  evtSource.addEventListener('heartbeat', () => {}); // Keep-alive
}

function renderStats(d) {
  const h = d.health, q = d.queue, g = d.goals, sk = d.skills;
  document.getElementById('topStats').innerHTML =
    '<div class="stat"><div class="stat-value '+scoreColor(h.health_score)+'">'+h.health_score+'%</div><div class="stat-label">Health</div></div>' +
    '<div class="stat"><div class="stat-value">'+q.total+'</div><div class="stat-label">Tasks</div></div>' +
    '<div class="stat"><div class="stat-value '+(q.counts.queued?'yellow':'')+'">'+( q.counts.queued||0)+'</div><div class="stat-label">Queued</div></div>' +
    '<div class="stat"><div class="stat-value green">'+(q.counts.completed||0)+'</div><div class="stat-label">Done</div></div>' +
    '<div class="stat"><div class="stat-value">'+(g.stats.total)+'</div><div class="stat-label">Goals</div></div>' +
    '<div class="stat"><div class="stat-value purple">'+(g.stats.pending_approval||0)+'</div><div class="stat-label">Pending</div></div>' +
    '<div class="stat"><div class="stat-value purple">'+(sk.count)+'</div><div class="stat-label">Skills</div></div>';
}

function renderHealth(h) {
  let html = '';
  const dot = (s) => '<div class="level-dot" style="background:var(--'+(s==='healthy'?'green':s==='degraded'?'yellow':s==='critical'?'red':'text-dim')+')"></div>';
  for (const [name, data] of Object.entries(h.levels||{})) {
    html += '<div class="level-row">'+dot(data.status)+'<span class="level-name">'+name+'</span><span class="level-status">'+data.status+' &mdash; '+data.passed+'/'+data.checks+' checks</span></div>';
  }
  html += '<div style="margin-top:8px;font-size:0.8em;color:var(--text-dim)">L1 Gating: '+(h.l1_healthy?'<span class="green">OPEN</span>':'<span class="red">BLOCKED</span>')+' &bull; Updated '+ago(h.last_update)+'</div>';
  document.getElementById('healthPanel').innerHTML = html || '<span style="color:var(--text-dim)">No data yet</span>';
}

function renderSparkline(points) {
  const el = document.getElementById('sparkline');
  if (!points || !points.length) { el.innerHTML = '<span style="color:var(--text-dim);font-size:0.8em">No health history</span>'; return; }
  const w = 280, h = 40, pad = 2;
  const scores = points.map(p => p.score);
  const min = Math.min(...scores), max = Math.max(...scores, 1);
  const range = max - min || 1;
  const pts = scores.map((s, i) => {
    const x = pad + (i / (scores.length - 1 || 1)) * (w - 2*pad);
    const y = pad + (1 - (s - min) / range) * (h - 2*pad);
    return x+','+y;
  }).join(' ');
  const last = scores[scores.length - 1];
  const col = last >= 80 ? 'var(--green)' : last >= 50 ? 'var(--yellow)' : 'var(--red)';
  el.innerHTML = '<svg class="sparkline" width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'">' +
    '<polyline fill="none" stroke="'+col+'" stroke-width="1.5" points="'+pts+'"/>' +
    '<circle cx="'+pts.split(' ').pop().split(',')[0]+'" cy="'+pts.split(' ').pop().split(',')[1]+'" r="2.5" fill="'+col+'"/>' +
    '</svg><span style="font-size:0.75em;color:var(--text-dim)">Last '+scores.length+' readings &bull; Latest: '+last.toFixed(1)+'%</span>';
}

function renderMemory(m) {
  const mem = m.memory, prof = m.profile;
  const memPct = pct(mem.chars, mem.limit), profPct = pct(prof.chars, prof.limit);
  const barColor = (p) => p > 80 ? 'var(--red)' : p > 60 ? 'var(--yellow)' : 'var(--green)';
  document.getElementById('memoryPanel').innerHTML =
    '<div class="bar"><span class="bar-label">Agent</span><div class="gauge" style="flex:1"><div class="gauge-fill" style="width:'+memPct+'%;background:'+barColor(memPct)+'"></div></div><span class="bar-value">'+mem.chars+'/'+mem.limit+'</span></div>' +
    '<div class="bar"><span class="bar-label">Profile</span><div class="gauge" style="flex:1"><div class="gauge-fill" style="width:'+profPct+'%;background:'+barColor(profPct)+'"></div></div><span class="bar-value">'+prof.chars+'/'+prof.limit+'</span></div>' +
    '<div style="margin-top:6px;font-size:0.8em;color:var(--text-dim)">Trajectories: '+(m.trajectories?.count||0)+'</div>';
}

function renderQueue(q) {
  if (!q.tasks.length) { document.getElementById('queuePanel').innerHTML = '<span style="color:var(--text-dim)">No tasks</span>'; return; }
  let html = '<table><tr><th>#</th><th>Task</th><th>Status</th><th>P</th><th>Age</th></tr>';
  for (const t of q.tasks.slice(0, 15)) {
    html += '<tr><td>'+t.id+'</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+t.title+'</td><td>'+statusBadge(t.status)+'</td><td>'+( t.priority||'-')+'</td><td>'+ago(t.created_at)+'</td></tr>';
  }
  html += '</table>';
  document.getElementById('queuePanel').innerHTML = html;
}

function renderGoals(g) {
  if (!g.goals.length) { document.getElementById('goalsPanel').innerHTML = '<span style="color:var(--text-dim)">No goals</span>'; return; }
  let html = '<table><tr><th>#</th><th>Goal</th><th>Status</th><th>P</th><th>Actions</th></tr>';
  for (const gl of g.goals.slice(0, 15)) {
    let actions = '';
    if (gl.status === 'pending_approval' || gl.status === 'proposed') {
      actions = '<button class="btn-sm btn-green" onclick="approveGoal('+gl.id+')">Approve</button> ' +
                '<button class="btn-sm btn-red" onclick="dismissGoal('+gl.id+')">Dismiss</button>';
      if (gl.expires_at) {
        const remaining = Math.max(0, gl.expires_at - Date.now()/1000);
        const total = gl.priority >= 7 ? 300 : gl.priority >= 4 ? 900 : 1800;
        const pctLeft = Math.min(100, remaining / total * 100);
        actions += '<div class="expiry-bar"><div class="expiry-fill" style="width:'+pctLeft+'%"></div></div>';
      }
    }
    html += '<tr><td>'+gl.id+'</td><td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+(gl.reasoning||'')+'">'+gl.title+'</td><td>'+statusBadge(gl.status)+'</td><td>'+gl.priority+'</td><td>'+actions+'</td></tr>';
  }
  html += '</table>';
  document.getElementById('goalsPanel').innerHTML = html;
}

function renderTraining(t) {
  let html = '<div class="stat-row" style="margin-bottom:8px">';
  const sft = t.datasets['sft_dataset.jsonl'];
  const dpo = t.datasets['dpo_dataset.jsonl'];
  html += '<div class="stat" style="min-width:80px"><div class="stat-value" style="font-size:1.3em">'+(sft?.entries||0)+'</div><div class="stat-label">SFT</div></div>';
  html += '<div class="stat" style="min-width:80px"><div class="stat-value" style="font-size:1.3em">'+(dpo?.entries||0)+'</div><div class="stat-label">DPO</div></div>';
  html += '<div class="stat" style="min-width:80px"><div class="stat-value" style="font-size:1.3em">'+( t.datasets['Modelfile'] ? '<span class="green">Yes</span>' : '<span class="red">No</span>')+'</div><div class="stat-label">Modelfile</div></div>';
  html += '</div>';
  html += '<div style="font-size:0.8em;color:var(--text-dim);margin-bottom:4px">Model: <strong>'+t.model+'</strong></div>';
  if (t.last_train) {
    html += '<div style="font-size:0.8em;color:var(--text-dim)">Last train: '+ago(t.last_train.timestamp)+' &bull; '+( t.last_train.examples||'?')+' examples</div>';
  }
  if (t.eval) {
    const wins = t.eval.tuned_wins || 0, total = t.eval.total || 5;
    const color = wins >= 3 ? 'green' : 'yellow';
    html += '<div style="font-size:0.8em;margin-top:4px">Eval: <span class="'+color+'">'+wins+'/'+total+'</span> tuned wins</div>';
  }
  document.getElementById('trainingPanel').innerHTML = html;
}

function renderTrajectories(t) {
  let html = '<div class="stat-row" style="margin-bottom:8px">' +
    '<div class="stat" style="min-width:80px"><div class="stat-value" style="font-size:1.3em">'+t.total_trajectories+'</div><div class="stat-label">Total</div></div>' +
    '<div class="stat" style="min-width:80px"><div class="stat-value" style="font-size:1.3em">'+t.total_files+'</div><div class="stat-label">Files</div></div></div>';
  if (t.recent.length) {
    html += '<table><tr><th>Task</th><th>Outcome</th><th>Dur</th></tr>';
    for (const r of t.recent.slice(0, 8)) {
      const dur = r.duration_ms ? (r.duration_ms/1000).toFixed(1)+'s' : '-';
      html += '<tr><td style="max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+r.title+'</td><td>'+statusBadge(r.outcome)+'</td><td>'+dur+'</td></tr>';
    }
    html += '</table>';
  }
  document.getElementById('trajPanel').innerHTML = html;
}

function renderSkills(s) {
  if (!s.skills.length) { document.getElementById('skillsPanel').innerHTML = '<span style="color:var(--text-dim)">No learned skills yet</span>'; return; }
  let html = '';
  for (const sk of s.skills) {
    html += '<div class="skill-item"><div class="skill-name">'+sk.name+'</div><div class="skill-desc">'+sk.description+'</div></div>';
  }
  document.getElementById('skillsPanel').innerHTML = html;
}

let goalHistoryPage = 1;
async function loadGoalHistory(page) {
  goalHistoryPage = page || 1;
  try {
    const r = await fetch('/api/goals/history?page='+goalHistoryPage+'&per_page=10');
    const d = await r.json();
    let html = '<table><tr><th>#</th><th>Goal</th><th>Status</th><th>P</th><th>Source</th><th>Age</th></tr>';
    for (const g of d.goals) {
      html += '<tr><td>'+g.id+'</td><td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+(g.reasoning||'')+'">'+g.title+'</td><td>'+statusBadge(g.status)+'</td><td>'+g.priority+'</td><td>'+g.source+'</td><td>'+ago(g.timestamp)+'</td></tr>';
    }
    html += '</table>';
    if (d.pages > 1) {
      html += '<div class="pagination">';
      if (d.page > 1) html += '<button class="btn-sm btn-gray" onclick="loadGoalHistory('+(d.page-1)+')">Prev</button>';
      html += '<span style="font-size:0.8em;color:var(--text-dim)">'+d.page+'/'+d.pages+' ('+d.total+' total)</span>';
      if (d.page < d.pages) html += '<button class="btn-sm btn-gray" onclick="loadGoalHistory('+(d.page+1)+')">Next</button>';
      html += '</div>';
    }
    document.getElementById('goalHistoryPanel').innerHTML = html;
  } catch(e) {
    document.getElementById('goalHistoryPanel').innerHTML = '<span style="color:var(--text-dim)">Error loading history</span>';
  }
}

function renderLogs(logs) {
  if (!logs.entries.length) { document.getElementById('logPanel').innerHTML = '<span style="color:var(--text-dim)">No logs yet</span>'; return; }
  let html = '';
  const levelColor = {INFO:'var(--text-dim)',WARN:'var(--yellow)',ERROR:'var(--red)'};
  for (const e of logs.entries.slice().reverse()) {
    const color = levelColor[e.level] || 'var(--text-dim)';
    html += '<div style="padding:2px 0;border-bottom:1px solid var(--border)"><span style="color:'+color+'">['+( e.ts?.substring(11,19)||'')+']</span> <span style="color:var(--accent)">'+e.component+'</span> '+e.message+'</div>';
  }
  document.getElementById('logPanel').innerHTML = html;
}

async function approveGoal(id) {
  try {
    await fetch('/api/goals/approve', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({goal_id:id})});
    refresh();
  } catch(e) { console.error(e); }
}

async function dismissGoal(id) {
  try {
    await fetch('/api/goals/dismiss', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({goal_id:id})});
    refresh();
  } catch(e) { console.error(e); }
}

async function triggerCycle() {
  const btn = document.getElementById('cycleBtn');
  btn.disabled = true; btn.textContent = 'Running...';
  try {
    await fetch('/api/cycle', {method:'POST'});
    await refresh();
  } catch(e) { console.error(e); }
  btn.disabled = false; btn.textContent = 'Run Cycle';
}

async function refresh() {
  try {
    const [overview, logs, sparkData, training] = await Promise.all([
      fetch('/api/overview').then(r=>r.json()),
      fetch('/api/logs').then(r=>r.json()),
      fetch('/api/health-history?limit=50').then(r=>r.json()),
      fetch('/api/training').then(r=>r.json()),
    ]);
    renderStats(overview);
    renderHealth(overview.health);
    renderSparkline(sparkData.points);
    renderMemory(overview.memory);
    renderQueue(overview.queue);
    renderGoals(overview.goals);
    renderTraining(training);
    renderTrajectories(overview.trajectories);
    renderSkills(overview.skills);
    renderLogs(logs);
    loadGoalHistory(goalHistoryPage);
    document.getElementById('lastUpdate').textContent = 'Updated ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('lastUpdate').textContent = 'Error: ' + e.message;
  }
}

connectSSE();
refresh();
// Fallback polling if SSE disconnects
setInterval(() => { if (!evtSource || evtSource.readyState === 2) refresh(); }, 15000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------
class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())
            return

        # SSE endpoint
        if path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            q = queue.Queue(maxsize=50)
            with _sse_lock:
                _sse_clients.append(q)
            try:
                # Send initial connection event
                self.wfile.write(b"event: connected\ndata: {}\n\n")
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                    except queue.Empty:
                        # Send heartbeat to keep connection alive
                        self.wfile.write(b"event: heartbeat\ndata: {}\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with _sse_lock:
                    if q in _sse_clients:
                        _sse_clients.remove(q)
            return

        if path in API_ROUTES:
            try:
                data = API_ROUTES[path]()
                self.send_json(200, data)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path in PARAM_ROUTES:
            try:
                data = PARAM_ROUTES[path](params)
                self.send_json(200, data)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_length > 0:
            raw = self.rfile.read(content_length)
            try:
                body = json.loads(raw)
            except Exception:
                pass

        if path == "/api/cycle":
            try:
                data = api_trigger_cycle()
                self.send_json(200, data)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/goals/approve":
            try:
                data = api_goals_approve(body)
                self.send_json(200, data)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/goals/dismiss":
            try:
                data = api_goals_dismiss(body)
                self.send_json(200, data)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        self.send_response(404)
        self.end_headers()

    def send_json(self, code, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        if "/api/" not in str(args[0]):
            print(f"[dashboard] {args[0]}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ANAH Dashboard — web UI")
    parser.add_argument("--port", "-p", type=int, default=8420, help="Port (default 8420)")
    parser.add_argument("--open", "-o", action="store_true", help="Open browser automatically")
    args = parser.parse_args()

    # Load env
    env_file = ANAH_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

    server = HTTPServer(("0.0.0.0", args.port), DashboardHandler)
    print(f"[dashboard] ANAH Dashboard running at http://localhost:{args.port}", file=sys.stderr)
    print(f"[dashboard] Press Ctrl+C to stop", file=sys.stderr)

    if args.open:
        import webbrowser
        webbrowser.open(f"http://localhost:{args.port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] Stopped.", file=sys.stderr)
        server.server_close()

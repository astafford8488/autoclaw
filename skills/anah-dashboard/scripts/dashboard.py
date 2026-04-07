#!/usr/bin/env python3
"""ANAH Dashboard — Zero-dependency web UI for monitoring the autonomous hierarchy.

Uses Python's built-in http.server with an embedded single-page HTML/JS frontend.
Reads directly from ~/.anah/anah.db for real-time status.
"""

import json
import os
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


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_FILE))
    db.row_factory = sqlite3.Row
    return db


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
    stats = {"total": 0, "enacted": 0, "proposed": 0, "dismissed": 0}
    for row in db.execute("SELECT status, COUNT(*) as cnt FROM generated_goals GROUP BY status"):
        r = dict(row)
        stats[r["status"]] = r["cnt"]
        stats["total"] += r["cnt"]

    goals = []
    for row in db.execute(
        "SELECT id, title, priority, status, source, reasoning, timestamp "
        "FROM generated_goals ORDER BY timestamp DESC LIMIT 25"
    ):
        goals.append(dict(row))
    db.close()
    return {"stats": stats, "goals": goals}


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
        # Load recent trajectories for display
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

    # Training dataset info
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
        return {"triggered": True, "result": result}
    except Exception as e:
        return {"triggered": False, "error": str(e)}


# API route map
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


# ---------------------------------------------------------------------------
# Dashboard HTML
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
  .stat { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 10px 14px; flex: 1; min-width: 120px; text-align: center; }
  .stat-value { font-size: 1.8em; font-weight: bold; }
  .stat-label { font-size: 0.75em; color: var(--text-dim); text-transform: uppercase; }
  .green { color: var(--green); } .yellow { color: var(--yellow); } .red { color: var(--red); } .purple { color: var(--purple); }
  table { width: 100%; border-collapse: collapse; font-size: 0.85em; }
  th { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); color: var(--text-dim); font-weight: 500; }
  td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
  .badge { padding: 2px 8px; border-radius: 10px; font-size: 0.75em; font-weight: 500; }
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
  .refresh-note { font-size: 0.75em; color: var(--text-dim); }
  .level-row { display: flex; gap: 8px; margin-bottom: 4px; align-items: center; }
  .level-dot { width: 10px; height: 10px; border-radius: 50%; }
  .level-name { font-size: 0.85em; min-width: 30px; }
  .level-status { font-size: 0.8em; color: var(--text-dim); }
  .skill-item { padding: 6px 0; border-bottom: 1px solid var(--border); }
  .skill-name { font-weight: 500; font-size: 0.9em; }
  .skill-desc { font-size: 0.75em; color: var(--text-dim); }
  #loading { color: var(--text-dim); font-style: italic; }
</style>
</head>
<body>

<h1>ANAH Dashboard</h1>
<p class="subtitle">Autonomous Needs-Aware Hierarchy &mdash; Brain Monitor</p>

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
    <h2>Trajectories &amp; Training</h2>
    <div id="trajPanel"></div>
  </div>
  <div class="card">
    <h2>Learned Skills</h2>
    <div id="skillsPanel"></div>
  </div>
</div>

<div class="card" style="margin-bottom:12px">
  <h2>Scheduler Log</h2>
  <div id="logPanel" style="max-height:250px;overflow-y:auto;font-size:0.8em;font-family:monospace"></div>
</div>

<script>
const statusBadge = (s) => {
  const map = {completed:'green',queued:'blue',running:'yellow',failed:'red',pending_approval:'purple',enacted:'green',proposed:'blue',dismissed:'gray'};
  return `<span class="badge badge-${map[s]||'gray'}">${s}</span>`;
};
const scoreColor = (s) => s >= 80 ? 'green' : s >= 50 ? 'yellow' : 'red';
const ago = (ts) => {
  if (!ts) return '-';
  const s = Math.floor(Date.now()/1000 - ts);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  return Math.floor(s/3600) + 'h ago';
};
const pct = (v, max) => max > 0 ? Math.round(v/max*100) : 0;

function renderStats(d) {
  const h = d.health, q = d.queue, g = d.goals, sk = d.skills;
  document.getElementById('topStats').innerHTML = `
    <div class="stat"><div class="stat-value ${scoreColor(h.health_score)}">${h.health_score}%</div><div class="stat-label">Health</div></div>
    <div class="stat"><div class="stat-value">${q.total}</div><div class="stat-label">Tasks</div></div>
    <div class="stat"><div class="stat-value ${q.counts.queued?'yellow':''}">${q.counts.queued||0}</div><div class="stat-label">Queued</div></div>
    <div class="stat"><div class="stat-value green">${q.counts.completed||0}</div><div class="stat-label">Completed</div></div>
    <div class="stat"><div class="stat-value">${g.stats.total}</div><div class="stat-label">Goals</div></div>
    <div class="stat"><div class="stat-value purple">${sk.count}</div><div class="stat-label">Skills</div></div>
    <div class="stat"><div class="stat-value">${d.trajectories?.total_trajectories||0}</div><div class="stat-label">Trajectories</div></div>
  `;
}

function renderHealth(h) {
  let html = '';
  const dot = (s) => `<div class="level-dot" style="background:var(--${s==='healthy'?'green':s==='degraded'?'yellow':s==='critical'?'red':'text-dim'})"></div>`;
  for (const [name, data] of Object.entries(h.levels||{})) {
    html += `<div class="level-row">${dot(data.status)}<span class="level-name">${name}</span><span class="level-status">${data.status} &mdash; ${data.passed}/${data.checks} checks</span></div>`;
  }
  html += `<div style="margin-top:8px;font-size:0.8em;color:var(--text-dim)">L1 Gating: ${h.l1_healthy?'<span class="green">OPEN</span>':'<span class="red">BLOCKED</span>'} &bull; Updated ${ago(h.last_update)}</div>`;
  document.getElementById('healthPanel').innerHTML = html || '<span style="color:var(--text-dim)">No data yet</span>';
}

function renderMemory(m) {
  const mem = m.memory, prof = m.profile;
  const memPct = pct(mem.chars, mem.limit), profPct = pct(prof.chars, prof.limit);
  const barColor = (p) => p > 80 ? 'var(--red)' : p > 60 ? 'var(--yellow)' : 'var(--green)';
  document.getElementById('memoryPanel').innerHTML = `
    <div class="bar"><span class="bar-label">Agent</span><div class="gauge" style="flex:1"><div class="gauge-fill" style="width:${memPct}%;background:${barColor(memPct)}"></div></div><span class="bar-value">${mem.chars}/${mem.limit}</span></div>
    <div class="bar"><span class="bar-label">Profile</span><div class="gauge" style="flex:1"><div class="gauge-fill" style="width:${profPct}%;background:${barColor(profPct)}"></div></div><span class="bar-value">${prof.chars}/${prof.limit}</span></div>
    <div style="margin-top:6px;font-size:0.8em;color:var(--text-dim)">Trajectories: ${m.trajectories?.count||0}</div>
  `;
}

function renderQueue(q) {
  if (!q.tasks.length) { document.getElementById('queuePanel').innerHTML = '<span style="color:var(--text-dim)">No tasks</span>'; return; }
  let html = '<table><tr><th>#</th><th>Task</th><th>Status</th><th>P</th><th>Age</th></tr>';
  for (const t of q.tasks.slice(0, 15)) {
    html += `<tr><td>${t.id}</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${t.title}</td><td>${statusBadge(t.status)}</td><td>${t.priority||'-'}</td><td>${ago(t.created_at)}</td></tr>`;
  }
  html += '</table>';
  document.getElementById('queuePanel').innerHTML = html;
}

function renderGoals(g) {
  if (!g.goals.length) { document.getElementById('goalsPanel').innerHTML = '<span style="color:var(--text-dim)">No goals</span>'; return; }
  let html = '<table><tr><th>#</th><th>Goal</th><th>Status</th><th>P</th><th>Source</th></tr>';
  for (const gl of g.goals.slice(0, 15)) {
    html += `<tr><td>${gl.id}</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${gl.reasoning||''}">${gl.title}</td><td>${statusBadge(gl.status)}</td><td>${gl.priority}</td><td>${gl.source}</td></tr>`;
  }
  html += '</table>';
  document.getElementById('goalsPanel').innerHTML = html;
}

function renderSkills(s) {
  if (!s.skills.length) { document.getElementById('skillsPanel').innerHTML = '<span style="color:var(--text-dim)">No learned skills yet</span>'; return; }
  let html = '';
  for (const sk of s.skills) {
    html += `<div class="skill-item"><div class="skill-name">${sk.name}</div><div class="skill-desc">${sk.description}</div></div>`;
  }
  document.getElementById('skillsPanel').innerHTML = html;
}

function renderTrajectories(t) {
  let html = `<div class="stat-row" style="margin-bottom:8px">
    <div class="stat" style="min-width:80px"><div class="stat-value" style="font-size:1.3em">${t.total_trajectories}</div><div class="stat-label">Trajectories</div></div>
    <div class="stat" style="min-width:80px"><div class="stat-value" style="font-size:1.3em">${t.total_files}</div><div class="stat-label">Files</div></div>
    <div class="stat" style="min-width:80px"><div class="stat-value" style="font-size:1.3em">${t.training?.['sft_dataset.jsonl']?.entries||0}</div><div class="stat-label">SFT Ready</div></div>
  </div>`;
  if (t.training?.['sft_dataset.jsonl']) {
    const sft = t.training['sft_dataset.jsonl'];
    html += `<div style="font-size:0.8em;color:var(--text-dim);margin-bottom:6px">SFT dataset: ${sft.entries} examples (${(sft.size_bytes/1024).toFixed(1)}KB) &bull; Updated ${ago(sft.modified)}</div>`;
  }
  if (t.training?.['Modelfile']) {
    html += `<div style="font-size:0.8em;margin-bottom:6px"><span class="badge badge-green">Modelfile ready</span></div>`;
  }
  if (t.recent.length) {
    html += '<table><tr><th>Task</th><th>Outcome</th><th>Duration</th></tr>';
    for (const r of t.recent.slice(0, 8)) {
      const dur = r.duration_ms ? (r.duration_ms/1000).toFixed(1)+'s' : '-';
      html += `<tr><td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.title}</td><td>${statusBadge(r.outcome)}</td><td>${dur}</td></tr>`;
    }
    html += '</table>';
  }
  document.getElementById('trajPanel').innerHTML = html;
}

function renderLogs(logs) {
  if (!logs.entries.length) { document.getElementById('logPanel').innerHTML = '<span style="color:var(--text-dim)">No logs yet</span>'; return; }
  let html = '';
  const levelColor = {INFO:'var(--text-dim)',WARN:'var(--yellow)',ERROR:'var(--red)'};
  for (const e of logs.entries.slice().reverse()) {
    const color = levelColor[e.level] || 'var(--text-dim)';
    html += `<div style="padding:2px 0;border-bottom:1px solid var(--border)"><span style="color:${color}">[${e.ts?.substring(11,19)||''}]</span> <span style="color:var(--accent)">${e.component}</span> ${e.message}</div>`;
  }
  document.getElementById('logPanel').innerHTML = html;
}

async function refresh() {
  try {
    const [overview, logs] = await Promise.all([
      fetch('/api/overview').then(r=>r.json()),
      fetch('/api/logs').then(r=>r.json()),
    ]);
    renderStats(overview);
    renderHealth(overview.health);
    renderMemory(overview.memory);
    renderQueue(overview.queue);
    renderGoals(overview.goals);
    renderSkills(overview.skills);
    renderTrajectories(overview.trajectories);
    renderLogs(logs);
    document.getElementById('lastUpdate').textContent = 'Updated ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('lastUpdate').textContent = 'Error: ' + e.message;
  }
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

refresh();
setInterval(refresh, 10000);
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

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())
            return

        if path in API_ROUTES:
            try:
                data = API_ROUTES[path]()
                self.send_json(200, data)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/cycle":
            try:
                data = api_trigger_cycle()
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
        # Suppress default logging noise, keep it clean
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

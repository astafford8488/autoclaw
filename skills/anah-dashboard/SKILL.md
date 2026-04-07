---
name: anah-dashboard
description: "ANAH dashboard — real-time web UI for monitoring the autonomous hierarchy. Zero-dependency Python server with embedded HTML/JS frontend. Shows health status, task queue, goals, learned skills, and memory utilization. Triggers on phrases like 'anah dashboard', 'open dashboard', 'show anah status', 'anah ui', 'brain monitor'."
---

# ANAH Dashboard — The Eyes

A lightweight web dashboard for monitoring and controlling the ANAH autonomous hierarchy. Zero external dependencies — uses Python's built-in `http.server` with an embedded single-page app.

## Features

- Real-time health status (brainstem L1-L3)
- Task queue with status tracking
- Goal generation history
- Learned skills catalog
- Memory utilization gauges
- Auto-refresh every 10 seconds

## Usage

```bash
# Start dashboard on default port 8420
python scripts/dashboard.py

# Custom port
python scripts/dashboard.py --port 3000

# Open browser automatically
python scripts/dashboard.py --open
```

Then visit http://localhost:8420

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| GET / | Dashboard HTML |
| GET /api/health | Brainstem health status |
| GET /api/queue | Task queue with counts |
| GET /api/goals | Generated goals |
| GET /api/skills | Learned skills |
| GET /api/memory | Memory utilization |
| GET /api/overview | Combined status overview |
| POST /api/cycle | Trigger a heartbeat cycle |

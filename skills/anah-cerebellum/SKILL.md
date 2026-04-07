---
name: anah-cerebellum
description: "ANAH L4 performance monitoring and pattern analysis. Tracks task completion rates, error rates, throughput, latency, and queue health. Detects recurring failures, performance trends, idle opportunities, and maintenance needs. Triggers on phrases like 'performance check', 'L4 check', 'pattern analysis', 'task metrics', 'system performance', 'cerebellum check'."
---

# ANAH Cerebellum — Performance & Coordination (L4)

The cerebellum monitors task execution quality and detects patterns. It feeds signals to the cortex (L5) for goal generation.

## What it Monitors

### Task Metrics (2min interval)
- **completion_rate**: % of tasks completed vs failed
- **error_rate**: Recent failures per time window
- **throughput**: Tasks completed per hour
- **avg_latency**: Mean task execution time
- **queue_health**: Queue depth and aging

### Pattern Detection (5 detectors)
1. **Recurring failures**: Same check failing repeatedly
2. **Performance trends**: Degrading metrics over time windows
3. **Idle opportunities**: System healthy + queue empty = time for self-improvement
4. **Maintenance needs**: Old logs, stale data, backup age
5. **Check anomalies**: Unusual check durations or result patterns

## Usage

```bash
python scripts/cerebellum.py --metrics
python scripts/cerebellum.py --patterns
python scripts/cerebellum.py --all
```

## Output

JSON context summary consumed by the cortex for goal generation:
```json
{
  "health_score": 98.5,
  "active_levels": 5,
  "queue": {"queued": 0, "running": 0, "completed": 42, "failed": 1},
  "patterns": [{"title": "...", "severity": "warning", "suggested_action": "..."}],
  "idle": true
}
```

## Integration

The cerebellum reads state from `~/.anah/state.json` (written by brainstem) and task history from `~/.anah/anah.db`. Its output context is passed to the cortex when goal generation is triggered.

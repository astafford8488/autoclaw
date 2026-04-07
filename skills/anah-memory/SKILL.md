---
name: anah-memory
description: "ANAH bounded memory and trajectory export system. Manages experience logging with hard character limits (forcing consolidation), exports task trajectories in ShareGPT format for future RL training (Phase 4). Triggers on phrases like 'memory status', 'export trajectories', 'training data', 'consolidate memory', 'anah memory', 'trajectory export'."
---

# ANAH Memory — Bounded Experience & Trajectory Export

The memory system captures experience with enforced constraints, and exports structured training data for future model fine-tuning.

## Bounded Memory

Unlike unbounded logs, ANAH memory has hard limits:
- **Agent memory**: 2,200 characters max
- **System profile**: 1,375 characters max
- When full, the system must **consolidate or replace** — forcing prioritization of what matters most

This constraint (inspired by Hermes Agent) prevents memory bloat and forces the system to learn what's actually important.

## Memory Files

```
~/.anah/
├── MEMORY.md           Agent memory (bounded, auto-consolidated)
├── SYSTEM_PROFILE.md   System understanding (bounded)
├── learning_log.json   Hippocampus learning events
└── trajectories/       Exported training data
```

## Trajectory Export

Task execution traces are exported in ShareGPT format for future RL training:
```json
{
  "conversations": [
    {"from": "system", "value": "System context..."},
    {"from": "human", "value": "Task: investigate network latency"},
    {"from": "gpt", "value": "Running diagnostics...", "tool_calls": [...]}
  ],
  "metadata": {"task_id": 42, "outcome": "success", "duration_ms": 1500}
}
```

## Usage

```bash
# View memory status
python scripts/memory.py --status

# Consolidate memory (force prioritization)
python scripts/memory.py --consolidate

# Export trajectories for training
python scripts/memory.py --export --since 24h --format sharegpt

# Prune old trajectories
python scripts/memory.py --prune --keep 1000
```

## Phase 4 Integration

The trajectory export format is designed to feed directly into the Phase 4 self-improvement pipeline:
1. Export trajectories from successful tasks
2. Feed into LoRA adapter training (via lm-eval-harness)
3. Validate adapted weights against regression suite
4. Promote improvements through dual-weight validation

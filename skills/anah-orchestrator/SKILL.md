---
name: anah-orchestrator
description: "ANAH full-cycle orchestrator. Chains brainstem → cerebellum → cortex → hippocampus in a single heartbeat cycle. The central nervous system that coordinates all brain organelles. Triggers on phrases like 'anah heartbeat', 'run anah cycle', 'anah orchestrator', 'brain cycle', 'full cycle', 'anah status'."
---

# ANAH Orchestrator — Central Nervous System

The orchestrator is the backbone that coordinates all ANAH brain organelles in a single heartbeat cycle, analogous to the central nervous system.

## Heartbeat Cycle

```
Brainstem (L1-L3)  →  Cerebellum (L4)  →  Cortex (L5)  →  Hippocampus
   health checks       ingest + analyze    goal generation    skill learning
```

Each heartbeat:
1. **Brainstem** runs all health checks (L1-L3), with L1 gating
2. **Cerebellum** ingests brainstem results into DB, detects patterns, builds context
3. **Cortex** receives context + patterns, generates goals, enqueues tasks
4. **Hippocampus** evaluates recently completed tasks for skill extraction

## Cadence Presets

| Preset   | Brainstem | Cerebellum | Cortex | Hippocampus |
|----------|-----------|------------|--------|-------------|
| fast     | L1-L3     | always     | always | always      |
| normal   | L1-L3     | always     | if idle| if completed|
| watchdog | L1 only   | skip       | skip   | skip        |

## Usage

```bash
# Full heartbeat cycle
python scripts/orchestrator.py --cycle

# Quick L1-only watchdog
python scripts/orchestrator.py --watchdog

# Status overview of all organelles
python scripts/orchestrator.py --status

# Run with cortex generation (requires ANTHROPIC_API_KEY in ~/.anah/.env)
python scripts/orchestrator.py --cycle --generate
```

## Autoclaw Cron Integration

Register as cron jobs:
```bash
# Full cycle every 3 minutes
autoclaw cron add --name "anah-heartbeat" --every 3m --message "Run ANAH heartbeat cycle"

# L1 watchdog every 30 seconds
autoclaw cron add --name "anah-watchdog" --every 30s --system-event "anah:watchdog"
```

Or run directly via Python for lower overhead.

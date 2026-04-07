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

ANAH can run as Autoclaw cron jobs for system-restart survival and framework integration.

### Quick Setup

```bash
# Register all ANAH cron jobs (requires built openclaw CLI + running gateway)
./scripts/register_cron.sh

# Check status
./scripts/register_cron.sh --status

# Remove all ANAH cron jobs
./scripts/register_cron.sh --remove
```

### Registered Jobs

| Job | Schedule | Type | Purpose |
|-----|----------|------|---------|
| `anah-heartbeat` | Every 3m | Isolated agent | Full cycle: brainstem → cortex → executor → hippocampus |
| `anah-watchdog` | Every 30s | Main session | Quick L1 health check |
| `anah-training` | Daily 3 AM | Isolated agent | SFT/DPO dataset preparation |

### Cron Bridge

The `cron_bridge.py` script provides a clean JSON interface between Autoclaw cron and ANAH:

```bash
python scripts/cron_bridge.py heartbeat   # Full cycle with structured summary
python scripts/cron_bridge.py watchdog    # Quick L1 check
python scripts/cron_bridge.py status      # Status overview
python scripts/cron_bridge.py train       # Training pipeline
python scripts/cron_bridge.py export      # Export trajectories
```

### Standalone Mode

For development or when gateway isn't available, use the standalone scheduler:

```bash
python scripts/scheduler.py --fast --generate   # Aggressive intervals + goal generation
python scripts/scheduler.py --daemon             # Background mode
```

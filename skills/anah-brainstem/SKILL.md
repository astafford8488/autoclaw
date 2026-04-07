---
name: anah-brainstem
description: "ANAH L1-L3 autonomic health monitoring. Runs survival checks (network, filesystem, compute, WiFi), state safety checks (config integrity, DB integrity, backup recency), and ecosystem checks (API connectivity, integration health). Triggers on phrases like 'run health checks', 'system status', 'brainstem check', 'L1 check', 'L2 check', 'L3 check', 'survival check'."
---

# ANAH Brainstem — Autonomic Survival (L1-L3)

The brainstem is ANAH's autonomic nervous system. It monitors operational survival without LLM involvement — pure signal checks that keep the system alive.

## Hierarchy Levels

### L1 — Operational Survival (30s heartbeat)
- **network_connectivity**: DNS resolution + TCP reachability
- **filesystem_access**: Read/write test to working directory
- **compute_resources**: CPU, RAM, disk usage against thresholds
- **wifi_interface**: Active network interfaces present

### L2 — Persistent State Safety (5min interval)
- **config_integrity**: SHA-256 checksum of config files
- **db_integrity**: SQLite PRAGMA integrity_check
- **backup_recency**: Auto-backup if stale (>10min)

### L3 — Task Ecosystem Health (15min interval)
- **anthropic_api**: Claude API reachability (expects 401/403/405 = reachable)
- **integration_health**: Configurable endpoint pings

## Gating Rule

If L1 fails, L2-L5 are **suspended**. The brainstem protects higher functions.

## Usage

Run all checks:
```bash
python scripts/brainstem.py --all
```

Run a specific level:
```bash
python scripts/brainstem.py --level 1
python scripts/brainstem.py --level 2
python scripts/brainstem.py --level 3
```

Output is JSON to stdout for machine consumption by other ANAH organelles.

## Integration with Autoclaw Cron

The brainstem should be scheduled via Autoclaw's cron system:
- L1: every 30 seconds
- L2: every 5 minutes
- L3: every 15 minutes

Results are written to `~/.anah/state.json` for consumption by the cerebellum and cortex.

## Thresholds (configurable in ~/.anah/config.json)

```json
{
  "thresholds": {
    "cpu_percent_max": 90,
    "ram_percent_max": 85,
    "disk_percent_max": 90,
    "dns_timeout_sec": 5,
    "api_ping_timeout_sec": 10
  }
}
```

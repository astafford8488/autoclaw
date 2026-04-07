---
name: anah-cortex
description: "ANAH L5 autonomous goal generation and task planning. Analyzes system state via the cerebellum's pattern data and generates actionable goals. Uses Claude API for LLM reasoning with pattern-based fallback. Handles goal deduplication, dismissal, and goal-to-task decomposition. Triggers on phrases like 'generate goals', 'L5 generation', 'what should I work on', 'cortex check', 'autonomous planning', 'self-directed goals'."
---

# ANAH Cortex — Goal Generation & Planning (L5)

The cortex is ANAH's prefrontal cortex — the seat of autonomous reasoning. It generates goals when the system is healthy and idle.

## Activation Conditions

1. L1-L3 must be healthy (brainstem gating)
2. L4 performance data available (cerebellum feed)
3. Task queue must be empty or below threshold
4. Cooldown period elapsed since last generation (3 minutes)

## Goal Generation Modes

### LLM Mode (primary)
Uses Claude API to analyze system context and generate 1-3 actionable tasks. The prompt includes:
- Current hierarchy status from brainstem
- Performance metrics from cerebellum
- Detected patterns and anomalies
- Recent goal history (for deduplication)

### Pattern Fallback Mode
When no API key or API unreachable, generates goals from detected patterns:
- Recurring failures → diagnostic tasks
- Healthy + idle → health reports
- Task milestones → integrity checks

## Deduplication

Two-layer dedup prevents repetitive goals:
1. **Prompt-level**: Recent goals included in LLM context with instructions to avoid repetition
2. **Post-generation filter**: Word-overlap similarity check (50% threshold) against last 20 goals

## Goal Lifecycle

```
proposed → enacted (queued as task) → completed/failed
proposed → dismissed (user rejected, won't repeat)
```

## Usage

```bash
python scripts/cortex.py --generate
python scripts/cortex.py --status
python scripts/cortex.py --dismiss <goal_id>
```

## Integration with Autoclaw

Generated goals become Autoclaw tasks. Instead of ANAH's limited 4-handler executor, goals now have access to Autoclaw's full tool suite:
- Browser automation for web research
- Terminal execution for system tasks
- File system operations
- Web search and fetch
- MCP server integrations
- All messaging channels

This is the key advantage of the Autoclaw integration — ANAH's brain generates the goals, Autoclaw's body executes them.

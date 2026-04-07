---
name: anah-executor
description: "ANAH task executor — the motor cortex. Dequeues tasks from the queue, routes them to appropriate handlers, executes them, and records results. Closes the autonomous loop between goal generation and action. Triggers on phrases like 'execute tasks', 'run queue', 'task executor', 'process tasks', 'anah executor'."
---

# ANAH Executor — Motor Cortex

The executor is the hands of the system — it picks up tasks from the queue and actually does them. Without it, the cortex generates goals that sit idle forever.

## Execution Loop

```
Dequeue Task → Route to Handler → Execute → Record Result → Hippocampus learns
```

## Built-in Handlers

| Handler | Trigger | What it does |
|---------|---------|-------------|
| health_report | title starts with `health_report:` | Runs brainstem + cerebellum, produces report |
| self_diagnostic | title starts with `self_diagnostic:` | Full system diagnostic with recommendations |
| cleanup | title starts with `cleanup:` | Prunes old logs, trajectories, backups |
| echo | title starts with `echo:` | Returns the task description (testing) |
| ollama | title contains `ollama` or `llm` | Sends task to Ollama for freeform execution |

Tasks not matching any handler are sent to Ollama for general-purpose execution.

## Task Lifecycle

```
queued → running → completed/failed
                ↘ pending_approval (if approval gate enabled)
```

## Usage

```bash
# Process next queued task
python scripts/executor.py --next

# Process up to N tasks
python scripts/executor.py --run --limit 5

# Process all queued tasks
python scripts/executor.py --run

# Show queue status
python scripts/executor.py --status

# Drain mode: process until queue is empty
python scripts/executor.py --drain
```

---
name: anah-hippocampus
description: "ANAH autonomous skill creation and learning loop. Inspired by Hermes Agent's self-improving architecture. After task completion, evaluates what happened, extracts reusable procedures into new Autoclaw skills, and refines existing skills based on outcomes. Triggers on phrases like 'learn from task', 'create skill from experience', 'refine skill', 'hippocampus', 'learning loop', 'extract procedure', 'what did I learn'."
---

# ANAH Hippocampus — Learning & Skill Creation

The hippocampus transforms experience into reusable knowledge. After complex tasks complete, it evaluates outcomes and creates or refines Autoclaw skills autonomously.

## The Learning Loop

```
Task Completes → Evaluate Outcome → Extract Pattern → Create/Refine Skill
     ↑                                                        |
     └────────── Future similar tasks use the skill ──────────┘
```

This is the key Hermes-inspired feature: skills aren't just static files — they grow from experience.

## When it Activates

The hippocampus evaluates a task for skill extraction when:
1. Task completed successfully
2. Task involved 3+ distinct steps or tool calls
3. Task type hasn't been seen before, OR existing skill produced suboptimal results
4. Cooldown elapsed since last skill creation (to avoid thrashing)

## Skill Extraction Process

1. **Gather evidence**: Task title, description, result, duration, tool calls used
2. **Analyze patterns**: What approach worked? What was the sequence?
3. **Draft skill**: Create SKILL.md with frontmatter, instructions, and references
4. **Validate**: Check for duplicates against existing skills
5. **Register**: Write to `~/.anah/skills/` directory for auto-loading

## Skill Refinement

When a task uses an existing skill and the outcome provides new information:
1. Compare actual outcome vs skill expectations
2. If outcome differs, update the skill with new learnings
3. Track skill version and refinement history

## Usage

```bash
# Evaluate a completed task for skill extraction
python scripts/hippocampus.py --evaluate <task_id>

# List auto-generated skills
python scripts/hippocampus.py --list-skills

# Refine an existing skill based on new evidence
python scripts/hippocampus.py --refine <skill_name> --evidence <task_id>
```

## Skill Format

Generated skills follow Autoclaw's standard format:
```
~/.anah/skills/<skill-name>/
├── SKILL.md          (frontmatter + instructions)
├── scripts/          (executable procedures)
└── references/       (supporting docs)
```

## Integration

The hippocampus reads task results from `~/.anah/anah.db` and writes skills to the filesystem. Generated skills are automatically available to Autoclaw on the next invocation.

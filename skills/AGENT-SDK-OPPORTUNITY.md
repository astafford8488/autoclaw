# ANAH + Claude Agent SDK — Evaluation & Backlog

**Date:** 2026-05-21
**Status:** Backlog proposal — evaluate before committing

---

## TL;DR

The Claude Agent SDK provides out-of-the-box versions of the core primitives ANAH is building from scratch: agent loops, subagent spawning, lifecycle hooks, tool orchestration, session persistence. Rebuilding the ANAH orchestration layer on top of it could collapse months of custom work into SDK primitives — but locks the project into Claude as the reasoning engine.

---

## What the Agent SDK Provides (vs what ANAH hand-rolls)

| ANAH Concept | Current Implementation | Agent SDK Equivalent |
|---|---|---|
| Heartbeat cycle / scheduler | Custom Python scheduler loop in `anah-orchestrator` | Built-in agent loop — runs autonomously until task done |
| Brain organelles (brainstem, cerebellum, cortex, etc.) | Separate Python skill dirs, share state via `~/.anah/` + SQLite | **Subagents** — spawn specialized child agents with focused instructions + tool allowlists |
| MCP tool bridge (Phase 3 WS2) | Custom bridge to web_search, notion, slack, gmail, gcal | **Native MCP server integration** — pass MCP config, agent calls tools natively |
| Metacognition / lifecycle monitoring | Custom `anah-cerebellum` + `anah-cortex` | **Hooks** — `PreToolUse`, `PostToolUse`, `SessionStart`, `SessionEnd` callbacks |
| Goal approval (Discord buttons) | Custom Discord persistent views | **AskUserQuestion** tool — structured prompts with choices, blocks until response |
| State persistence across cycles | SQLite at `~/.anah/anah.db` + JSON files | **Sessions** — capture `session_id`, resume later with full context |
| sandbox_eval (Phase 3 WS4) | Custom subprocess sandbox with blocklist + timeouts | **Bash** tool with permissions granularity — allowlist/denylist commands per-agent |
| Trajectory export / training | Custom ShareGPT format export | Session JSONL transcripts — native format, richer than ShareGPT |
| Two-phase LLM reasoning (Phase 2) | Custom analyze → generate pipeline via Ollama/Anthropic | Subagent pattern — analyzer agent feeds into generator agent |

---

## What This Would Look Like

### Architecture: ANAH-on-SDK

```
anah-sdk/
  src/
    orchestrator.py        # Main agent — replaces anah-orchestrator
    agents/
      brainstem.py         # Subagent: L1-L3 health monitoring
      cerebellum.py        # Subagent: L4 performance + pattern detection
      cortex.py            # Subagent: L5 goal generation
      hippocampus.py       # Subagent: skill creation from task evidence
      evolution.py         # Subagent: Phase 4 ARM (arxiv scout → shadow deploy)
    hooks/
      safety.py            # PreToolUse — block dangerous commands
      audit.py             # PostToolUse — log every action
      metacognition.py     # PostToolUse — feed patterns to cerebellum
    mcp_config.py          # MCP server connections (slack, notion, gmail, etc.)
    config.py              # Load from ~/.anah/config.json
```

The orchestrator becomes the **main agent** that spawns organelles as **subagents** per heartbeat cycle. Each subagent has:
- Focused system prompt (its "personality" from current skill instructions)
- Scoped tool access (brainstem gets health-check tools only, cortex gets LLM + web)
- Reports findings back to orchestrator, which decides next action

### Example: One Heartbeat Cycle

```python
from claude_agent_sdk import query, ClaudeAgentOptions

async def heartbeat():
    async for message in query(
        prompt="Run one ANAH heartbeat cycle: health check → performance analysis → goal evaluation",
        options=ClaudeAgentOptions(
            agents={
                "brainstem": {
                    "instructions": "L1-L3 health monitoring. Check network, filesystem, compute, config, DB, external APIs.",
                    "tools": ["Bash", "Read"],
                },
                "cerebellum": {
                    "instructions": "L4 performance analysis. Detect patterns, score handler effectiveness.",
                    "tools": ["Read", "Grep"],
                },
                "cortex": {
                    "instructions": "L5 goal generation. Based on health + performance data, propose autonomous goals.",
                    "tools": ["Read", "Write", "WebSearch"],
                },
            },
            mcp_servers={
                "slack": {"command": "...", "args": [...]},
                "notion": {"command": "...", "args": [...]},
            },
            hooks={
                "PreToolUse": "hooks/safety.py",
                "PostToolUse": "hooks/audit.py",
            },
        ),
    ):
        handle_message(message)
```

---

## Decision Matrix: Rebuild vs Stay on Autoclaw

| Factor | Rebuild on Agent SDK | Stay on Autoclaw |
|---|---|---|
| **Time to Phase 4** | ~1 week (framework is ready, port organelle logic) | ~2-3 weeks (build from scratch per roadmap) |
| **Time to Phase 5 (multi-agent swarm)** | Near-free — subagents ARE the swarm | Major custom work — parallel instances + negotiation |
| **Model lock-in** | Claude only (Opus/Sonnet/Haiku) | Model-agnostic (Ollama local, Anthropic fallback) |
| **Cost per heartbeat cycle** | ~$0.02-0.10/cycle (API token costs) | Free (Ollama local) or ~$0.01 (Haiku fallback) |
| **Cost at 1 cycle/30min, 24/7** | ~$50-150/month | ~$0-15/month |
| **Existing test suite (407 tests)** | Must be ported/rewritten | Already passing |
| **Shadow deployment (Phase 4)** | Subagent isolation is native | Custom `~/.anah-shadow/` (already designed) |
| **Discord integration** | Loses persistent views — need webhook/API adapter | Native (already built) |
| **Self-modification (WS4)** | Possible via Write tool, but SDK sandbox may conflict | Full control via sandbox_eval |
| **VC pitch ("we built our own AGI stack")** | Weaker — "we used Anthropic's SDK" | Stronger — "custom cognitive architecture" |

---

## Recommendation

**Hybrid approach — don't rebuild, but adopt SDK for Phase 5 (multi-agent swarm).**

### Rationale:
1. **Phases 1-3 are shipped and working.** 407 tests passing. Don't rewrite what works.
2. **Phase 4 (ARM/evolution) is deeply designed.** The shadow deployment model is specific to ANAH's needs. SDK subagents don't cleanly map to "spawn a parallel instance of myself with modifications."
3. **Phase 5 (multi-agent swarm) is where the SDK pays off massively.** Spinning up specialized ANAH instances that negotiate task routing IS the subagent pattern. Build Phase 5 on the SDK instead of hand-rolling swarm coordination.
4. **Model lock-in matters for the VC pitch** but less for initial validation. Use Ollama for cost-sensitive heartbeat cycles, SDK for the high-reasoning swarm layer.

### Backlog items to add:

- [ ] **Phase 5 WS5.0 — Agent SDK evaluation spike** (~2 days): Port `anah-orchestrator` to SDK as a proof-of-concept. Run 24h side-by-side with current implementation. Compare: cycle quality, latency, cost, failure modes.
- [ ] **Phase 5 WS5.1 — SDK swarm prototype**: Build 3 specialized ANAH subagents (research, ops, creative) using SDK subagent API. Test task routing via orchestrator agent.
- [ ] **Phase 5 WS5.2 — Hybrid runtime**: Keep Ollama for L1-L4 (cheap, local, fast). Use SDK + Claude for L5 goal generation and swarm coordination (needs reasoning quality).
- [ ] **Evaluate Managed Agents for ANAH-as-a-Service**: If ANAH becomes a product others deploy, Managed Agents ($0.08/session-hour) could be the hosting model. Investigate after Phase 5 validation.

---

## Resources

- [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
- [Subagents in the SDK](https://platform.claude.com/docs/en/agent-sdk/subagents)
- [MCP in the SDK](https://docs.claude.com/en/docs/agent-sdk/mcp)
- [Managed Agents overview](https://platform.claude.com/docs/en/managed-agents/overview)
- SDK packages: `pip install claude-agent-sdk` / `npm install @anthropic-ai/claude-agent-sdk`

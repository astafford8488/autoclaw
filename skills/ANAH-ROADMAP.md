# ANAH Development Roadmap

**ANAH** — Autonomous Needs-Aware Hierarchy. A 5-level agent orchestration layer that builds goal-generating, self-improving autonomy on top of local LLMs and the OpenClaw platform.

The hierarchy:
- **L1**: network / filesystem / compute monitoring (brainstem)
- **L2**: config / DB / backup integrity (brainstem)
- **L3**: external API health, integration pings (brainstem)
- **L4**: task metrics, pattern detection (cerebellum)
- **L5**: autonomous goal generation, planning, self-improvement (cortex)

---

## Completed phases

### Phase 1 — Foundation (done)
Brainstem health checks, cerebellum context builder, basic cortex with pattern-based goal generation, executor with handler routing, SQLite persistence, scheduler loop.

### Phase 2 — Two-phase cognition + hippocampus (done)
- Two-phase LLM reasoning (analyze → generate)
- Hippocampus: learned skills library, dedup via topic hash, historical success scoring
- Discord notifier + goal approval channel
- Dashboard (tabs, suppressed-alert log)
- Training pipeline (trajectory export)

### Phase 3 — Goal chaining, MCP, metacognition, self-modification (done)
- **WS1**: Multi-step goal chaining with dependency resolution
- **WS2**: MCP tool bridge (web_search, web_fetch, slack, notion, gmail, gcal)
- **WS3**: Metacognition — trend analysis, failure chain tracing, handler scoring, capability-gap detection, strategy journal feeding back into L5 prompts
- **WS4**: Self-modification — `sandbox_eval` handler, dynamic custom handler loading from `~/.anah/custom_handlers/`, safety layer (blocklist, subprocess timeout, revert command)

407 tests passing at end of Phase 3.

---

## Phase 4 — L6 Evolution Layer: Adjunct Research Module (ARM)

**Goal:** Give ANAH the ability to autonomously discover, evaluate, and absorb cognitive-architecture advances from published AI research, running proposed improvements in a parallel shadow instance before any promotion to production.

This creates a *sixth tier* above L5 — a metacognitive layer that evolves the agent's own architecture rather than just its goals or handlers. It is the closest thing to open-ended self-improvement ANAH can safely pursue.

### High-level flow

```
(daily, 2am)
  ├─► arxiv_scout: pull new cs.AI / cs.LG / cs.NE / cs.MA papers from last 24h
  ├─► filter: keyword match on cognitive-framework terms + novelty score vs seen cache
  ├─► paper_analyzer: extract framework, components, innovations, claims
  ├─► architecture_diff: compare paper framework to ANAH's self-spec
  ├─► evolution_decider: score compatibility × expected-gain × risk
  │     ├─ if score < threshold → log + discard
  │     └─ if score ≥ threshold → post Discord proposal for user approval
  └─► (on approval) shadow_deploy: spawn ~/.anah-shadow/ with modifications,
                     run side-by-side for N cycles, collect comparative metrics,
                     report verdict, wait for user promote/revert decision
```

### Component breakdown

Five new modules in a new skill `anah-evolution/`:

**1. `arxiv_scout.py`** — Daily paper discovery
- arXiv Atom API (`http://export.arxiv.org/api/query`) — free, no auth required
- Query categories: `cs.AI`, `cs.LG`, `cs.NE`, `cs.MA`, `cs.CL`
- Keyword filters: cognitive architecture, autonomous agent, self-improving, meta-learning, hierarchical planning, metacognition, tool-use, agent reasoning, world model
- Date filter: submissions in the last 24h only
- Dedup cache: `~/.anah/evolution/papers_seen.json` (arxiv_id + title hash)
- Rate limit: 1 req/3s per arXiv TOS
- Output: list of candidate paper metadata (title, authors, abstract, arxiv_id, pdf_url, categories)

**2. `paper_analyzer.py`** — Framework extraction
- Fetch the abstract first; only download full PDF if abstract passes relevance gate
- PDF text extraction: prefer arXiv's HTML rendering (`https://arxiv.org/html/{id}`) over PDF to avoid heavy deps; fall back to `pdfplumber` for PDF
- LLM prompt: extract structured framework spec
  ```json
  {
    "framework_name": "...",
    "components": [{"name": "...", "role": "...", "inputs": [], "outputs": []}],
    "innovations": ["..."],
    "measurable_claims": [{"metric": "...", "baseline": "...", "reported": "..."}],
    "implementation_complexity": "low|medium|high",
    "code_released": true|false
  }
  ```
- Stored at `~/.anah/evolution/analyses/{arxiv_id}.json`

**3. `architecture_diff.py`** — Current-vs-proposed delta
- Maintain `anah-evolution/SELF_SPEC.yaml` — a canonical, machine-readable description of ANAH's current architecture (levels, components, interfaces, handlers)
- Two-stage diff:
  1. Structural: which L1–L5 components does the paper add/replace/augment?
  2. Behavioral: LLM compares the paper's measurable claims against ANAH's current performance (pulled from metacognition journal)
- Output: `{new_capabilities[], better_alternatives[], compatibility, required_rewrites[], estimated_loc, risk_level}`

**4. `evolution_decider.py`** — Go/no-go scoring
- Composite score: `compatibility * expected_gain * (1 - risk_level) * confidence`
- Threshold tunable via `~/.anah/config.json`: `evolution.min_score` (default 0.55)
- Auto-reject if: `risk_level == "high"` AND `code_released == false`, or `required_rewrites` touches security-critical paths (sandbox_eval, notify validator)
- If passes threshold → construct Discord proposal with: TL;DR, paper link, expected gain, estimated LoC, risk notes, 2 buttons: **Spawn Shadow** / **Dismiss**
- Never auto-proceeds past this gate without explicit user approval

**5. `shadow_deploy.py`** — Parallel-copy execution
- On approval, create `~/.anah-shadow/` as a full clone of `~/.anah/` (fresh DB, copied config, empty trajectories)
- Generate the modifications as a patch-set via LLM → save to `~/.anah-shadow/changes/patch_{timestamp}.py`
- Each patch must pass `sandbox_eval` before being written into shadow code tree
- Shadow instance runs its own scheduler on a different port, same Ollama endpoint, fully isolated DB
- Comparative metrics collector runs every cycle: goal quality, task success rate, health trend, handler effectiveness, token cost, latency
- Results stored at `~/.anah/evolution/trials/{trial_id}/metrics.jsonl`
- Trial duration: configurable (`evolution.trial_cycles`, default 48 cycles ≈ 24h)
- End-of-trial report posted to Discord with promote / revert / extend buttons

### Safety model

Non-negotiable constraints — enforced in code, not just policy:

1. **No production code modification without user approval.** `evolution_decider` can *propose*; only a Discord button click or CLI flag authorizes shadow creation.
2. **Shadow isolation.** Shadow writes to `~/.anah-shadow/` only. Its DB is a copy, not a reference. Its executor uses a separate custom-handler dir. Its notify handler uses a test Discord channel if one is configured, else no-ops.
3. **Sandbox-gated patches.** Every proposed code change runs through the existing `sandbox_eval` pipeline before being committed to the shadow tree. The blocklist from WS4 applies.
4. **Promotion requires two approvals.** Promoting shadow → primary requires: (a) sustained improvement across the trial window, AND (b) explicit user confirmation with a 5-minute cooling-off timer between approval click and actual promotion.
5. **Automatic revert.** If shadow's health score drops below `evolution.auto_revert_floor` (default 60) for 3 consecutive cycles, trial aborts and shadow is quarantined.
6. **Audit trail.** Every scouted paper, every decision, every patch, every metric comparison written to `~/.anah/evolution/audit.jsonl`. Append-only.
7. **Cost cap.** Maximum `evolution.monthly_paper_budget` (default 50) papers analyzed per month to prevent API / LLM cost blowup.

### Work streams for Phase 4

**WS4.1 — Scout + Analyzer**
- `arxiv_scout.py` with dedup cache, category/keyword filters, rate limiting
- `paper_analyzer.py` with abstract-first gating, HTML-first extraction, LLM structured output
- Daily cron registration in scheduler
- Tests: mock arXiv responses, dedup correctness, keyword filtering precision, malformed PDF handling

**WS4.2 — Self-Spec + Diff Engine**
- Write initial `SELF_SPEC.yaml` covering L1–L5 and Phase 3 additions
- `architecture_diff.py` with structural + behavioral diff phases
- Integration with metacognition strategy journal for behavioral baseline
- Tests: diff against known-equivalent paper (should return "compatible, no change"), diff against obviously-superior paper (should return significant delta)

**WS4.3 — Decider + Discord Proposal**
- `evolution_decider.py` scoring with configurable weights
- New Discord embed type + persistent view with Spawn Shadow / Dismiss buttons (extends existing `GoalApprovalView` pattern)
- Audit log writer
- Tests: threshold boundary cases, auto-reject rules, scoring determinism

**WS4.4 — Shadow Deployment + Metrics**
- `shadow_deploy.py` with isolated directory, fresh DB copy, patch application via sandbox
- Shadow scheduler runs on separate PID with `--shadow` flag, writes to `~/.anah-shadow/`
- Comparative metrics collector (polls both DBs each cycle)
- End-of-trial report generator
- Tests: isolation verification (shadow cannot write to primary), metrics divergence detection, revert mechanics, promotion ceremony (5-minute cooling-off)

**WS4.5 — CLI + dashboard integration**
- `anah-evolution/scripts/cli.py` with `--scan`, `--status`, `--trials`, `--approve`, `--revert`
- Dashboard `/evolution` tab: recent papers, active trials, metric comparison charts
- Tests: CLI argument handling, dashboard rendering with empty/populated trials

### Dependencies

- **New Python deps:** `pdfplumber` (optional — only if HTML extraction fails), already-installed `urllib` for arXiv API
- **Existing deps:** metacognition (WS3), sandbox_eval (WS4), Discord persistent views (Phase 2), cron scheduler (Phase 1)
- **External:** arXiv public API (no key), Ollama (existing), Anthropic Haiku fallback (existing)

### Risks + open questions

- **Risk: LLM framework extraction is noisy.** Mitigation: strict JSON schema + validation; abstract-only for low-signal papers.
- **Risk: shadow cannibalizes Ollama throughput.** Mitigation: shadow runs at lower cycle frequency than primary (e.g. every 2nd primary tick); optional separate Ollama endpoint via `evolution.ollama_url`.
- **Risk: implementation cost explodes if papers propose whole-architecture rewrites.** Mitigation: `evolution_decider` rejects any paper requiring > `evolution.max_loc` lines of change (default 500); larger changes must be manually decomposed into sub-phases.
- **Open: how aggressive should the keyword filter be?** Phase 4 starts permissive (broad categories + broad keywords) and tightens based on analyst-feedback signal in the audit log. Re-evaluate at end of Phase 4.
- **Open: should promoted shadows auto-commit to git?** Phase 4 does *not* touch git. Promotion updates files in the primary tree and logs to audit; git commits remain a manual/user action.

### Success criteria

- Daily scout runs without failure for 7 consecutive days
- At least one paper per week surfaces with a non-trivial diff
- At least one shadow trial completes end-to-end (scout → analyze → decide → shadow → metrics → report) within Phase 4 window
- Zero unintended modifications to primary `~/.anah/` during any trial
- 95%+ test coverage on the five new modules

### Estimated size

~1200–1800 LoC across five modules + tests + Discord embed + dashboard tab. Comparable in scope to Phase 3 (which landed in ~1600 LoC).

---

## Backlog (post-Phase-4)

- **Phase 5 — Multi-agent swarm**: spin up parallel ANAH instances that specialize (research / ops / creative) and negotiate task routing
- **Phase 6 — Persistent long-term memory**: vector store for trajectories + cross-session retrieval
- **Phase 7 — Open-ended curriculum generation**: L5 generates its own training data for trainer to consume

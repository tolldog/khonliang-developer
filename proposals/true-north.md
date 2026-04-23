# khonliang ecosystem — true-north charter

Decisional artifact: what the platform is, what it isn't, how it grows, and how new feature requests are triaged against scope.

Reassess after every 2-3 shipped milestones, or when a new FR surfaces a question this charter doesn't answer.

## Charter (one paragraph)

The khonliang ecosystem is a small, personally-maintained, local-first LLM engineering platform. It optimizes for **token economy** — every skill is designed so Claude-driven workflows fit inside subscription quota with headroom; **cross-vendor review** — no LLM-authored change lands without review by a *different* LLM, because same-model self-review doesn't count; **local-first inference** — Ollama on the primary host is the hot path, external APIs are reserved for benchmarks and reference ceilings; **elegant correctness** — "elegantly correct" is the target, not "fastest to ship." It explicitly is **not**: a productized LLM platform, a team tool, a general-purpose agent framework, or a data-pipeline host. Feature growth comes from two legitimate channels, equally weighted — user roadmap direction, and dogfooding-friction-driven primitives (the "LLM suggests unmapped primitives; evaluator promotes accepted ones" pattern). Scope is defended by a lightweight rubric at FR intake and by a three-tier architecture that keeps reference consumers out of platform code.

## Three architecture tiers

### Tier 1 — Core platform

- **Services**: khonliang-bus (agent bus + service registry + MCP adapter + artifacts), khonliang-scheduler (LLM inference scheduling).
- **Agents**: reviewer-primary, developer-primary, researcher-primary.
- **Libraries**: khonliang, khonliang-bus-lib, khonliang-reviewer-lib, khonliang-researcher-lib.

Platform code. Changes here ripple everywhere. Maximum-scrutiny review.

### Tier 2 — Extensions

Same ecosystem, same contracts, new capability fitting an existing agent's charter or a clearly-scoped new agent. Must share platform contracts (Skill dataclass, bus transport, library types) — never fork them.

Example patterns:

- New skills on existing agents (a topic-in-context brief generator, a severity-floor filter).
- Future sibling agents sharing platform contracts (a calibration agent, a benchmark-runner agent).

### Tier 3 — Reference consumers

Private sibling apps built **on** the platform. They depend on platform primitives; their domain logic never migrates up.

**Rule**: must not back-contaminate platform APIs with consumer-specific shapes. These are dogfooding targets — friction they surface is captured as *platform-generic* FRs, not consumer-specific ones. The tier boundary is the firewall.

## Capability map (current state)

Three agents, 102 non-deprecated skills (105 total including the deprecated researcher `synergize` group), 1 collaborative flow.

Skill counts reconcile against the per-agent tables below: reviewer 5 (1+3+1) + developer 47 + researcher 53 (including the 3-skill deprecated `synergize` group) = 105 total; excluding the deprecated group yields 102.

### reviewer-primary — LLM-based review (narrow)

| Group | Skills |
|---|---|
| Agent-hygiene | `health_check` |
| Review work | `review_text`, `review_diff`, `review_pr` |
| Observability | `usage_summary` |

### developer-primary — dev lifecycle + git/GitHub

| Group | Count |
|---|---|
| Agent-hygiene | 1 |
| Guides | 1 |
| FR lifecycle (promote/list/get/update/set_dependency/merge/next/migrate/candidates) | 12 |
| Milestone + spec (get/list/propose/review/read/draft/traverse) | 8 |
| Work units (list/next/handoff) | 3 |
| Git local ops (14 standard + status) | 15 |
| GitHub (`pr_ready`) | 1 |
| Session (checkpoint/resume) | 2 |
| Repo hygiene (audit/apply) | 2 |
| Cross-agent proxy (`get_paper_context`) | 1 |
| Testing (`run_tests`) | 1 |

### researcher-primary — corpus + distillation

| Group | Count |
|---|---|
| Agent-hygiene (`health_check`, `worker_status`) | 2 |
| Guides (`catalog`, `coding_guide`, `research_guide`, `response_modes`) | 4 |
| Knowledge store (ingest/search/context + file ingest) | 4 |
| Papers (fetch variants, list, digest, context) | 7 |
| Distillation (paper/pending/start) | 3 |
| Concepts (freshness/matrix/path/taxonomy/tree/per-project) | 6 |
| Triples (add/context/query) | 3 |
| Relevance (find/score/evaluate) | 3 |
| Synergize (deprecated shim + compare + concepts) | 3 |
| Synthesize (landscape/project/topic) | 3 |
| Idea workflow (brief/ingest/research/workspace) | 4 |
| RSS (`browse_feeds`) | 1 |
| Repo workflow (register/list/scan/ingest_github/research/project/landscape) | 7 |

### Overlaps + gaps

**Intentional overlaps** — acceptable:

- `health_check` per agent (agent-hygiene tier).
- Per-agent guide skills (each agent explains its own conventions).

**Concerning overlaps** — worth a cleanup FR when they next drift:

- `coding_guide` (researcher) vs `developer_guide` (developer) — two "how to code in this ecosystem" sources of truth. Cache-invalidation problem.
- `synergize` deprecated on researcher — remove in a future cleanup FR once no callers remain.

**Gaps** — candidate FRs when each need concretizes:

- No cross-agent skill-discovery / capability-negotiation API.
- No streaming / progress-event mechanism for long skills (related to the MCP timeout work but separate concern).
- No persistent cross-agent memory surface (memory is Claude-side; agents have independent SQLite stores).

## Parallelism axis (concurrency, not role)

Distinct from the agent-role axis. Mechanisms in use:

- **Multi-agent bus** — 3 agents concurrent today.
- **Git worktrees** per branch — `git worktree add` for concurrent branches in one repo; not stash+switch.
- **Multi-repo** — the 8 repos in the ecosystem are independent checkout surfaces.
- **Split-iTerm / parallel-Claude** — two Claude sessions, one per repo, sharing state only via memory + bus.
- **Subagent dispatch** — the primary Claude session coordinates multiple parallel implementation agents.

What "defined cleanly at the start" requires:

- **FR boundaries**: two in-flight FRs mustn't touch the same files. Extend the "single-concern PR" rule to "single-concern FR sets."
- **Repo boundaries**: platform-vs-consumer AND agent-vs-agent splits are already enforced by the repo layout.
- **Worktree boundaries**: orthogonal work in one repo goes to its own worktree.
- **Bus-routed state vs session state**: anything two sessions need to coordinate on flows through bus / memory / FR store — never through conversation context.

## Provenance channels (both valid)

1. **User-driven** — roadmap direction, explicit asks, milestone sequencing.
2. **Dogfooding-friction-driven** — Claude hits a friction point in daily use, surfaces a proposal, human evaluates, accepted proposals land as FRs.

Don't reflexively de-prioritize channel 2. Several load-bearing primitives (usage tracking, health-check adoption, version helpers) came from friction provenance. The rubric treats both channels equally; only the evaluation step differs — friction FRs benefit from a "could this be expressed as composition of existing skills?" sanity-check.

## Hardware-fit axis

Model-selection routing has to respect host VRAM. Practical tiers on the primary host:

| Tier | Model size | Fits VRAM | Practical |
|---|---|---|---|
| Fast | 7-8B | yes | yes — hot path |
| Mid | 12-16B | yes | yes — quality tier |
| Large | 31-32B+ | no (CPU offload) | escalation only |

Rule-table routing must separate "model is good" from "model is practical on this host." Hardware upgrades should unlock new tiers via config edits, not refactors — wire the fit axis in from the start.

## FR triage rubric (5 yes/no questions at intake)

Apply to every FR before it lands in the live store:

1. **Does it fit an existing agent's charter?** (Yes → proceed. No → new role — red flag. New roles need explicit architectural buy-in.)
2. **Is it platform-generic or consumer-specific?** (Platform-generic → proceed. Consumer-specific → redirect to the consumer repo, not the platform.)
3. **Is it expressible as a composition of two existing skills?** (Yes → document the composition pattern instead of adding surface. No → proceed.)
4. **Does it pay for itself in token economy?** (Yes → proceed. No → justify why it's still worth the surface; default is rejection.)
5. **Is provenance recorded?** (User-driven or dogfooding-friction — both fine; what matters is that it's tagged. Unrecorded provenance = amend before promote.)

**FRs that fail more than one question go to a `proposals/` holding area** (this directory), not `promote_fr`. They can mature there until they either pass or get dropped.

## When to consult this

- Before filing a new FR: apply the rubric.
- Before creating a new repo or a new agent: check against the tier rules.
- Before promoting a dogfooding-friction observation: confirm channel 2 provenance is recorded.
- When a new hardware-tier comes online: update the hardware-fit table.
- When a consumer-specific pattern keeps almost-but-not-quite fitting the platform: don't bend the platform — the tier boundary is working as designed.

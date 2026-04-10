# khonliang: From Agent Library to Agent Platform

A design document for where the khonliang ecosystem is going and why.

**Companion documents:**
- [`review.md`](review.md) — corpus-backed evaluation of this architecture against 500+ distilled papers
- [`use-cases.md`](use-cases.md) — 8 observed workflows from real Claude sessions, formalized as replayable definitions
- [`design.md`](design.md) — the original bus-MCP brainstorm (superseded by this doc)

---

## The Insight

We're building two things that turn out to be the same thing:

1. **Helper tools for vibing with Claude.** Open a few Claude sessions, point them at MCP servers (researcher, developer), and iterate toward a goal. The MCP servers give Claude access to a research corpus, spec management, FR tracking, and distillation pipelines. Claude stays focused on code and decisions; the MCPs handle everything upstream.

2. **Infrastructure for real distributed agents.** Independent processes with skills, shared state, session memory, dynamic model routing, and cross-agent coordination. The kind of thing you'd build if you were designing a multi-agent platform from scratch.

These overlap almost completely. The MCP tools Claude uses at code-time are the same agent skills that run at service-time. The knowledge store that holds research papers also holds agent memory. The distillation pipeline that compresses papers also compresses agent session history. The same `select_best_of_n` function evaluates both research summaries and agent outputs.

This document captures what we have today, what we were planning next, and a new architectural direction that unifies everything.

---

## Part 1: What We Have Today

### The Ecosystem

```
LIBRARIES (Python)
├─ khonliang (v0.6.4)         — agent primitives, stores, MCP transport
└─ researcher-lib (v0.1.0)    — evaluation primitives (relevance, synthesis, distillation)

APPS (Python MCP servers)
├─ researcher                  — ingests the world: papers, RSS, OSS → corpus → FR ideas
└─ developer (v0.1.0, MS-01)  — consumes the corpus: specs, milestones, worktrees, dispatch

INFRASTRUCTURE (Go)
├─ khonliang-bus (v0.1.0)     — event bus + service registry (HTTP+WebSocket, pub/sub)
└─ khonliang-scheduler        — LLM inference scheduling (built, not yet running)
```

### khonliang — The Agent Library

The foundation. Provides primitives for building multi-role LLM applications on top of Ollama (local inference).

**Agent primitives:**
- `BaseRole` — an agent with a system prompt and model assignment
- `BaseRouter` — routes requests to the right role
- `ModelPool` — maps roles to models, manages connections
- `AgentTeam` — multiple roles working together on a task
- `ConsensusEngine` — agents deliberate and vote on a result
- `Blackboard` — shared state space where agents post observations and read each other's work (public and private sections)

**Storage:**
- `KnowledgeStore` — three-tier knowledge (Axiom/Imported/Derived), SQLite + FTS5 full-text search
- `TripleStore` — semantic triples (subject-predicate-object) with confidence decay
- `DigestStore` — accumulator for structured digest entries

**MCP infrastructure:**
- `KhonliangMCPServer` — base class for building MCP servers. Registers knowledge, triple, and blackboard tools automatically. Apps subclass and add their own `@mcp.tool()` functions.
- `format_response`, `compact_summary` — token-efficient response formatting (compact/brief/full detail levels)

**Current version:** 0.6.4, actively evolving, 20+ test files.

### researcher-lib — Shared Cognitive Primitives

Extracted from the researcher app so both researcher and developer can use them. Seven modules:

| Module | What it does |
|---|---|
| `RelevanceScorer` | Embedding-based relevance scoring with adaptive learning |
| Entity graph builders | `build_entity_matrix`, `build_entity_graph`, `trace_chain`, `find_paths` |
| `BaseQueueWorker` | Background processing with retry tracking |
| `BaseSynthesizer` | Multi-document LLM synthesis |
| `BaseIdeaParser` | Decompose informal text into claims + search queries |
| `select_best_of_n` | Self-distillation: N parallel generations, model picks the best |
| `LocalDocReader` | Structure-aware local file reads (frontmatter, sections, references) |

These are **cognitive primitives** — they work on papers, specs, session histories, or any structured text. Their applicability extends beyond research to any agent that needs to summarize, evaluate, decompose, or read structured documents.

### researcher — The Research Agent

46 MCP tools spanning the full research pipeline:

```
Discovery → Ingestion → Distillation → Exploration → Synthesis → FR Generation
```

- **Discovery:** search arxiv + semantic scholar, browse RSS feeds, scan codebases
- **Ingestion:** fetch papers (PDF, HTML, arxiv), ingest local files, ingest ideas
- **Distillation:** LLM-powered summarization + triple extraction + relevance scoring. Uses `select_best_of_n` (3 parallel 7B runs, model picks best). Papers below relevance threshold are auto-skipped.
- **Exploration:** knowledge search, concept trees/paths/matrices, triple queries
- **Synthesis:** cross-paper topic analysis, project applicability briefs, landscape overviews
- **FR generation:** `synergize` classifies concepts and auto-generates feature requests with backing evidence. FRs are tracked with status progression (open → planned → in_progress → completed).

**Current state:** runs locally, 485 distilled papers in the corpus, no bus integration yet.

### developer — The Development Agent (MS-01)

The inverse of researcher: consumes the corpus to produce internal artifacts. Just landed MS-01 (skeleton + spec/milestone reading). Five MCP tools:

- `read_spec` — parse spec files (bold-line metadata, section index, FR references)
- `list_specs` — discover spec files per project
- `traverse_milestone` — backward-walk milestone → specs → FRs with evidence chain
- `health_check` — DB, workspace, connection status
- `developer_guide` — workflow documentation for Claude

**Architecture:** independent storage (own `developer.db`, never shares with researcher). A `ResearcherClient` seam is interface-complete but stubbed — real cross-agent calls land when the bus supports request/reply.

### khonliang-bus — The Event Bus

Go server + Python client. Provides:

- **Pub/sub messaging** — publish to topics, subscribe via WebSocket, durable subscriptions (offline subscribers receive missed messages on reconnect)
- **Service registry** — TTL-based heartbeat tracking, service discovery
- **Storage backends** — memory (dev), SQLite (production), Redis (planned)
- **Python client** — `BusClient` with sync publish/ack, async subscribe iterator, auto-registration

**What it does NOT have today:** request/reply, NACK/retry, dead letter queues, shared subscriptions, skill registry, MCP transport, session management, flow orchestration.

**Current state:** v0.1, built and tested, not yet wired into researcher or developer.

### khonliang-scheduler — LLM Inference Scheduling

Go server for managing Ollama inference. GPU slot management (VRAM tracking), batch scheduling.

**Current state:** built (single initial commit), not running. Awaiting HTTP API completion and integration with the bus.

---

## Part 2: What We Were Planning

### The Two-Plane Architecture

The original plan for developer MS-02+ was a "two-plane" design:

```
Plane 1: Claude ──stdio──▶ researcher MCP (local tool calls)
         Claude ──stdio──▶ developer MCP  (local tool calls)

Plane 2: developer MCP ──bus──▶ researcher MCP (cross-app coordination)
         researcher MCP ──bus──▶ developer MCP
```

Each MCP server would be both a tool-provider (for Claude, over stdio) and a bus client (for sibling MCPs, over the bus). Two interfaces, two protocols.

### The Problem This Creates

Claude becomes the orchestrator. When a task spans agents — "evaluate this spec against the research corpus" — Claude has to:

1. Call researcher: "get papers on this topic" → tokens
2. Hold the result in context → tokens
3. Call developer: "evaluate the spec against these papers" → tokens
4. Coordinate errors, retries, partial results → tokens

Every cross-agent round-trip burns Claude's context on routing, not decisions. Today's spec evaluation workflow costs ~50K tokens with Claude reasoning over tool outputs. With agents coordinating directly, Claude would only review the pre-evaluated result — ~5K tokens. A 10x reduction for the most common cross-agent workflow. (See [use-cases.md](use-cases.md) §Use Case 2 for the measured comparison.)

With 2-3 MCPs in a session, this is manageable. With more agents, it doesn't scale — Claude's context window becomes the bottleneck for the entire system.

---

## Part 3: The New Direction

### Everything Is an Agent

The key realization: the "services" in the ecosystem are agents. They have skills, they collaborate, they maintain state, they can be composed into teams. The same abstractions khonliang uses for local LLM roles apply at the distributed level.

| Local (single process) | Distributed (bus) |
|---|---|
| `BaseRole` — an agent with skills | Agent process — has skills, registers with bus |
| `ModelPool` — maps roles to models | Scheduler — maps agents to compute |
| `AgentTeam` — named group of roles | Team — named group of agents |
| `ConsensusEngine` — roles deliberate | Same, but agents are remote |
| `Blackboard` — shared state | Blackboard over bus — shared state across processes |
| In-process dispatch | Bus request/reply |

The abstraction is identical. The only thing that changes is transport. This is validated by Microsoft's Magentic-One, Rasa's A2A+MCP orchestration, and AgentsNet's coordination framework — all use the same "agents as modular, composable units with skills" pattern. (See [review.md](review.md) §Part 1 for the corpus evidence.)

### The Bus as Agent Orchestrator

The bus evolves from a message broker to the agent platform's central nervous system:

```
┌─────────┐
│  Claude  │  ← talks to one MCP
└────┬─────┘
     │ stdio
     ▼
┌──────────────────────────────────────────────────┐
│                  bus MCP                          │
│                                                  │
│  agent registry ── skill catalog ── orchestrator │
│       │                │                │        │
│  ┌────┴────┐     ┌────┴────┐     ┌────┴─────┐  │
│  │  who    │     │  what   │     │   how    │  │
│  │ exists  │     │ they do │     │  they    │  │
│  │         │     │         │     │ compose  │  │
│  └─────────┘     └─────────┘     └──────────┘  │
└──────┬──────────────┬──────────────┬────────────┘
       │              │              │
       ▼              ▼              ▼
   agents (independent processes, anywhere)
```

Claude makes one high-level call. The bus routes to the right agent (solo skill) or orchestrates a multi-agent flow (team skill). Claude gets back the result. The coordination happens at bus-message cost — local, fast, free relative to Claude tokens.

### Agent Registration

Each agent registers with the bus on startup. Registration has three layers:

**Layer 1 — Identity + health** (exists today in khonliang-bus)

```yaml
id: researcher
version: "0.6.4"
status: healthy
```

**Layer 2 — Solo skills**

What this agent can do on its own. Each skill becomes a tool that Claude can call through the bus MCP. Skills are tagged with the version they were introduced, enabling version-gated dependencies.

```yaml
skills:
  - name: find_papers
    description: "Search arxiv + semantic scholar"
    since: "0.1.0"
    parameters:
      query: { type: string, required: true }
```

**Layer 3 — Team skills (collaborative)**

What this agent can do with other agents. Declared as flows with version requirements. The bus validates that all dependencies are met before exposing the team skill to Claude.

```yaml
collaborations:
  - name: evaluate_spec_against_corpus
    description: "Evaluate a spec against research evidence"
    requires:
      researcher: ">=0.5.0"
    flow:
      - call: developer.read_spec
        args: { path: "{{path}}" }
        output: spec
      - call: researcher.find_relevant
        args: { query: "{{spec.title}}" }
        output: papers
      - call: researcher.paper_context
        args: { query: "{{spec.title}}" }
        output: evidence
      - call: developer.evaluate_with_evidence
        args: { spec: "{{spec}}", evidence: "{{evidence}}", papers: "{{papers}}" }
        output: evaluation
    returns: evaluation
```

**Proactive skills** — agents can also declare background capabilities that run on a schedule or in response to events, not just on-demand:

```yaml
proactive:
  - name: scan_new_papers
    trigger: cron
    schedule: "0 */6 * * *"
    description: "Scan RSS feeds for new papers, auto-ingest and distill"
    publishes: ["researcher.papers_ingested"]
```

Proactive agents initiate actions based on accumulated context and observed patterns — researcher auto-ingesting new papers, developer re-evaluating active specs when new evidence arrives. The bus + scheduler infrastructure already supports this; it just needs to be declarable in the registration schema. (See [review.md](review.md) §Proactive agents for the Yutori production evidence.)

**Consumer/producer boundary:** each agent registers its own skills only. It can declare collaborations that *call* other agents' skills. It cannot register skills on behalf of another agent. The bus validates that declared dependencies exist and meet version requirements.

**A2A protocol note:** the skill registration schema (Layers 2-3) is close to Google's Agent-to-Agent (A2A) protocol's Agent Card format. A2A defines capability declaration, task lifecycle (send/working/completed/failed), and streaming artifacts. When A2A stabilizes, adding a protocol adapter should be a plugin, not a rewrite — the registration schema just needs a few additional fields (streaming support, authentication requirements). Design decisions here should prefer A2A-compatible shapes over custom ones. (See [review.md](review.md) §A2A.)

### Schema Versioning

Skills have version gates (`since: "0.1.0"`, `requires: researcher >= "0.5.0"`). But what happens during rolling upgrades when a skill's input/output shape changes?

**Rule: additive-only changes are safe; breaking changes require a major version bump.**

- Adding a new optional field to a skill's output → minor version. Existing callers ignore it.
- Renaming a field or changing its type → major version. The bus rejects collaborations whose version gates don't match.
- The bus validates schemas at registration time and again when a collaborating agent re-registers at a new version. If a collaboration's required version gate no longer matches, the team skill is withdrawn from Claude's catalog with a diagnostic message.

### The Interaction Matrix

The bus builds an interaction matrix from registrations:

```
                researcher(0.6.4)  developer(0.1.0)
researcher      solo: 46           collab: 3
developer       collab: 2          solo: 8

Active collaborations:
  developer.evaluate_spec_against_corpus  [researcher>=0.5 ✓]
  developer.create_fr_from_research       [researcher>=0.3 ✓]
  researcher.research_and_plan            [developer>=0.1 ✓]

Unavailable:
  (none — all dependencies met)
```

**Dynamic catalog:** when an agent goes down, its skills and collaborations disappear from the catalog. When it comes back (possibly at a new version), the bus re-validates. Claude always sees an accurate picture of what's available.

### Hierarchical Teams

Flat agent networks don't scale past ~8 agents — communication overhead grows O(N²). The registration schema supports **hierarchical teams** where a team lead routes to specialists:

```yaml
teams:
  - name: research_team
    lead: researcher        # only the lead appears in the outer catalog
    members: [summarizer, extractor, assessor, reviewer]
    description: "Full research pipeline — discovery through FR generation"
```

Specialists are internal to the team. Claude sees "research_team" with its aggregate skills; it doesn't see or call the individual summarizer, extractor, etc. The lead agent handles decomposition and delegation within the team.

This maps to natural structures: the research pipeline is a team of specialist agents coordinated by a lead. As the ecosystem grows, hierarchical teams prevent the catalog from exploding and keep communication overhead manageable. (See [review.md](review.md) §Hierarchical agent organization.)

### Lazy Skill Discovery

With 46 researcher skills + 8 developer skills + future agents, loading all tool schemas on MCP connect would fill Claude's context before any work starts. The bus MCP implements **progressive disclosure**:

1. **On connect:** Claude sees agent names + one-line descriptions only. Compact catalog, minimal tokens.
2. **On intent:** Claude calls `describe(agent, skill)` to load the full schema (parameters, types, examples) for a specific skill. Schema loaded only when needed.
3. **On call:** the bus validates arguments against the schema and routes.

This aligns with the MCP-Zero pattern (proactive toolchain construction) and the lazy tool loading FR already tracked in khonliang. (See [review.md](review.md) §The single-MCP model has a discovery problem.)

### Sessions: Stateful Agent Interactions

Agents aren't stateless functions — they accumulate context across multiple interactions within a session.

```
Claude: "I need a researcher on consensus algorithms"
Bus: creates session, assigns researcher team, loads prior context if any

Claude: session "focus on multi-agent specifically"
Bus: routes to same team, same context → they refine

Claude: session "what did you find?"
Bus: team summarizes using all accumulated context

Claude: session "done"
Bus: persists results, tears down
```

**Session state has public and private sections** (mirroring the blackboard's existing access model):

- **Public** — the output. Findings, conclusions, artifacts. Any agent can read this to inform its own work. Developer evaluating a spec finds researcher's prior session results and uses them as evidence — no cross-agent call, the work was already there.

- **Private** — working memory. LLM conversation history, failed queries, intermediate drafts. Only loaded on session resume. Not useful to other agents.

```
Session: researcher on "consensus algorithms"
├── public:
│   ├── findings: [paper summaries, key concepts]
│   ├── conclusions: "voting-based consensus outperforms..."
│   └── artifacts: [fr_draft, evidence_chain]
└── private:
    ├── conversation: [full LLM chat history]
    ├── failed_queries: ["initial search too broad"]
    └── intermediate_drafts: [v1, v2, v3]
```

**Session persistence and resume:** when a session suspends (agent restarts, process dies, user walks away), the bus persists context to its storage backend. When the session resumes — same agent instance or a different one — the bus loads the persisted context. The agent picks up where it left off, like Claude resuming a conversation.

This is validated by the LLM-based Multi-Agent Blackboard System paper (arxiv 2510.01285), which found that blackboard-mediated coordination with shared + per-agent state outperforms direct agent-to-agent messaging. (See [review.md](review.md) §Session management.)

### Context Distillation: Agent Memory Management

Agent session history grows. When it gets too large, the same distillation pipeline that compresses research papers compresses agent memory:

```
Session private context (raw):
  142 conversation turns, 84 intermediate results
  → 50KB of working memory

After distill (select_best_of_n):
  Structured summary of decisions and findings
  Key state preserved, dead ends compressed
  → 3KB
```

Session memory lives in tiers, same as the knowledge store:

```
HOT    → current conversation (full fidelity, in LLM context window)
WARM   → recent sessions (distilled, on blackboard, quick to reload)
COLD   → old sessions (heavily distilled, archived, searchable)
```

As sessions age, the distill feature promotes memory down the tiers. Hot → warm on suspend. Warm → cold after a configurable period. Cold is permanent but compact — the conclusions and artifacts are preserved even when the raw conversation is gone.

### Dynamic Model Routing

The model backing an agent isn't fixed — it scales with the session's cognitive load. The scheduler watches context size and routes to the right model:

```
Same agent, same session:
  Turn 1-5     → small context   → 3B   (fast, cheap)
  Turn 6-20    → growing context → 7B   (needs capacity)
  Turn 21-40   → complex task    → 32B  (heavy reasoning)
  [distill]    → context compacted
  Turn 41+     → small again     → 7B   (back down)
```

Distillation isn't just memory management — it's **cost control**. Compressing context lets the agent stay on smaller, faster models. The distillation threshold is: "when would we need to step up to the next model tier?"

```yaml
models:
  llama3.2:3b:
    max_context: 4096
    speed: fast
    good_for: [extraction, classification, short tasks]
  qwen2.5:7b:
    max_context: 16384
    speed: moderate
    good_for: [summarization, synthesis, idea parsing]
  qwen2.5:32b:
    max_context: 65536
    speed: slow
    good_for: [review, complex reasoning, multi-document analysis]

routing:
  strategy: smallest_sufficient
  distill_at: 0.75  # distill when context hits 75% of model max
```

The components for this exist (select_best_of_n, scheduler GPU management, distillation pipeline). The integration — scheduler watches context, triggers distillation, routes to smaller model — is novel. No paper in the corpus describes this closed loop. Worth building incrementally: manual model selection first, add monitoring, then automatic triggers. (See [review.md](review.md) §Dynamic model routing.)

### Agents Without Containers

If the bus handles routing, the scheduler handles compute, and the blackboard handles state — agents don't need to live inside a specific process:

```
Today:
  researcher MCP process
  ├── summarizer     ← trapped here
  ├── extractor      ← trapped here
  ├── assessor       ← trapped here
  └── all share process memory

Future:
  bus + blackboard + scheduler
  ├── summarizer     (own process, or shared, or another machine)
  ├── extractor      (same)
  ├── assessor       (same)
  └── connected by bus, state on blackboard, compute via scheduler
```

- An agent can serve multiple teams. The summarizer isn't "researcher's summarizer" — it's a summarizer. Developer can use it too.
- Scaling is per-agent. Distillation slow? Spin up more extractors. The bus routes to available ones.
- An agent crashes → bus reassigns to another instance → loads session context → picks up where the last one left off.

**Operational note:** shared agents need explicit resource budgeting to prevent one consumer from starving another. Keep agents process-local until there's concrete demand for sharing — don't extract the summarizer from researcher until developer actually needs it and can't just instantiate its own. This is Step 8 in the migration for a reason. (See [review.md](review.md) §Agents without containers.)

### What Claude Sees

One MCP. One catalog. Progressive disclosure:

```
=== AGENTS ===
  researcher (v0.6.4, healthy, 46 skills)
  developer (v0.1.0, healthy, 8 skills)

=== TEAMS ===
  developer.evaluate_spec_against_corpus
    — evaluate spec against research evidence [researcher>=0.5 ✓]
  researcher.research_and_plan
    — papers → distill → FRs → developer [developer>=0.1 ✓]

=== SESSIONS ===
  bus.start_session(team, task) → session_id
  bus.message(session_id, msg)  → response
  bus.status(session_id)        → progress
  bus.done(session_id)          → persist + release

Call describe(agent, skill) for full parameter schemas.
```

Claude picks the right mode. Quick lookup? Direct skill call. Multi-step research task? Start a session. Complex cross-agent operation? Call a team skill. Claude never orchestrates between agents itself — the bus handles it.

---

## Part 4: Observed Workflow Patterns

Eight real workflows were captured from actual Claude sessions (see [use-cases.md](use-cases.md) for full YAML definitions). Analysis of these workflows revealed six patterns the orchestration engine must support and a clear prioritization of bus features:

### Patterns

| Pattern | Example | Requirement |
|---|---|---|
| **DAG** (non-linear step references) | Step 4 takes output from steps 1 AND 3 | Step references `{{step.N.field}}`, not just sequential piping |
| **Parallel fan-out** | 4 research agents in parallel | `parallel` step type, wait-for-all semantics |
| **Human-in-loop breakpoint** | Spec review pause → Claude decides → continue | Session suspend/resume |
| **Nested workflow** | `evaluate_spec` inside `full_dev_cycle` | Workflow-as-step, sub-sessions inherit parent's public context |
| **Fallback on failure** | Paper fetch fails → ingest raw text instead | `on_failure` handler per step |
| **Background + notify** | Long research kicked off → notify when complete | Async execution, pub/sub completion event |

### Feature Requirements by Use Case

| Use Case | request/reply | cross-agent | session | flow engine | conditional | nested |
|---|---|---|---|---|---|---|
| 1. Research a concept | ✓ | | | | | |
| 2. Evaluate a spec | ✓ | ✓ | | | | |
| 3. Evaluate architecture doc | ✓ | ✓ | ✓ | | | |
| 4. What should we build next | ✓ | | | | | |
| 5. Batch paper ingestion | ✓ | | | | ✓ | |
| 6. Promote idea to FR | ✓ | | | | | |
| 7. Address PR review | ✓ | | ✓ | | | |
| 8. Full dev cycle | ✓ | ✓ | ✓ | ✓ | | ✓ |

**Key insight:** request/reply alone covers 4 of 8 use cases. Adding cross-agent calls covers 7 of 8. The full flow engine with nesting is only needed for the most complex workflow (use case 8 — the full spec→milestone→implement→merge cycle). This drives the migration order.

### Static Flows vs. Dynamic Orchestration

The linear flow declarations above work for simple cases but the literature warns they break for complex tasks. Microsoft's Magentic-One and Rasa's orchestrator both moved from static declarations to an **LLM-powered orchestrator agent** that plans and adapts dynamically.

The architecture supports both:
- **Static flows** (Step 6) — declared YAML, good for well-understood pipelines like "research a concept" or "batch ingest papers."
- **Orchestrator agent** (Step 6.5) — a khonliang `BaseRole` backed by a local LLM that dynamically composes flows for tasks that don't fit static declarations. It reads the skill catalog, plans a sequence, executes it, adapts if intermediate results change the optimal path.

Start with static. Add the orchestrator when real usage shows which flows need dynamic adaptation.

---

## Part 5: The Overlap

The tools that help Claude vibe toward a goal are the same tools that power a real agent platform:

| Claude's helper tool | Agent platform equivalent |
|---|---|
| researcher MCP → `find_papers` | researcher agent → `find_papers` skill |
| developer MCP → `evaluate_spec` | developer agent → `evaluate_spec` skill |
| `select_best_of_n` for paper distillation | `select_best_of_n` for agent context distillation |
| Knowledge store for research corpus | Knowledge store for agent memory (tiered, scoped) |
| Blackboard for agent coordination | Blackboard for session state (public/private) |
| ModelPool for role → model mapping | Scheduler for dynamic model routing by context size |
| AgentTeam for local multi-role tasks | Team definition for distributed multi-agent flows |
| ConsensusEngine for local deliberation | Same engine, agents are remote instead of local |

The same `BaseRole` interface works for a local LLM role (dispatches to Ollama) and a remote bus agent (dispatches to the bus). An `AgentTeam` composed of `RemoteRole` instances works identically to one composed of local roles. khonliang doesn't know or care where the agents live.

```python
# A remote agent IS a khonliang role
class RemoteRole(BaseRole):
    """A role whose backend is a bus agent instead of Ollama."""

    async def generate(self, prompt, **kwargs):
        return await self.bus.request(
            self.agent_id, kwargs["operation"], kwargs["args"]
        )
```

This isn't two separate products that happen to share code. It's one set of primitives that works at two scales. The local agent orchestration and the distributed agent platform are the same thing with different transport.

---

## Part 6: What Needs to Be Built

### In khonliang-bus (Go)

**1. Request/reply endpoint** — synchronous request routing. Claude (via bus MCP) or another agent sends a request, bus routes to the owning agent, returns the response. Prerequisite for everything else.

**2. Queue semantics** — the bus currently has stream semantics (broadcast, ordered, replayable) but needs queue semantics too:

| Feature | What it does | Why |
|---|---|---|
| NACK + retry | Configurable retry delay, max-retry count | Managed retries outperform ad-hoc (RetryGuard paper). Bus owns retry logic, not agents. |
| Dead letter topics | Per-subscription, queryable for debugging | Prevent poison messages from blocking subscriptions after N failures |
| Shared subscriptions | Competing consumers within a subscriber group | Agent pools (multiple summarizers) without caller-side load balancing |
| Key_Shared mode | Ordered per key, parallel across keys | Session affinity: all messages for session X → same agent |

These are well-understood patterns (Pulsar, NATS JetStream, RabbitMQ). The Go implementation follows established designs. (See [review.md](review.md) §Bus needs queue semantics.)

**3. Skill + collaboration registry** — agents register capabilities (skills, version gates, flow declarations, proactive triggers) alongside their identity. The bus stores them, validates version requirements, and uses them to build the MCP tool surface.

**4. Session management** — create, suspend, resume, archive sessions. Persist session context (public/private) to storage backend. Handle agent reassignment on failure. Key_Shared subscription mode ensures session affinity — messages for a session always route to the same agent instance.

**5. Flow orchestration engine** — execute declared multi-agent flows. Must support DAG step references, parallel fan-out, on_failure handlers, and nested workflows. Start with linear flows; grow as usage patterns emerge. Add an orchestrator agent (Step 6.5) when static flows prove insufficient.

**6. MCP transport** — the bus speaks MCP over stdio. Translates tool calls into bus requests/flows. Generates `tools/list` dynamically from the skill registry with progressive disclosure (descriptions only on connect, full schemas on demand). This makes the bus the single MCP that Claude connects to.

### In khonliang-scheduler (Go)

**7. Context-aware model routing** — select model size based on session context length. Expose as an API the bus can call when dispatching agent work.

**8. Integration with bus sessions** — scheduler manages the LLM context window for each active session. Notifies the bus when context hits the distillation threshold.

### In khonliang / researcher-lib (Python)

**9. `RemoteRole`** — a `BaseRole` subclass whose backend is a bus agent. Makes distributed agents composable with khonliang's existing orchestration primitives (AgentTeam, ConsensusEngine).

**10. `BusAgent` adapter** — wraps existing `@mcp.tool()` functions as bus request handlers. Bulk migration: `BusAgent.from_mcp(existing_server)` registers all tools automatically.

**11. Session context distillation** — apply `select_best_of_n` to agent conversation history, not just papers. The function already works on any text; the new part is the pipeline that triggers it when session context exceeds a threshold.

### In researcher + developer (Python)

**12. Register as bus agents** — each app starts a `BusAgent` alongside (or instead of) its MCP server. Skills are the existing tool functions. Collaborations and proactive capabilities declared in registration.

**13. Session-aware handlers** — tools that accept a `session_id` and read/write session context from the blackboard instead of starting fresh each call.

---

## Part 7: Migration

The migration is incremental. Each step delivers value independently. The use-case matrix (§Part 4) drives the prioritization.

**Step 1: Request/reply + queue semantics in the bus** — NACK/retry, dead letter, shared subscriptions, Key_Shared mode. Developer MS-02 uses request/reply for `ResearcherClient` calls. Both apps still run as standalone MCPs; the bus carries cross-app messages.
*Unlocks: use cases 1, 4, 5, 6 (half the value).*

**Step 2: Skill registry** — agents register capabilities with version gates. The bus validates and can answer "what can researcher do?" but doesn't expose MCP yet.

**Step 3: `BusAgent` adapter** — existing MCP tools become bus-callable with one line each. Both apps gain a bus-native entry point alongside their MCP entry point.
*Unlocks: use cases 2, 3 (cross-agent spec/architecture evaluation).*

**Step 4: MCP transport on the bus** — the bus becomes the master MCP with progressive disclosure. Claude's `.mcp.json` points at the bus instead of individual apps. Solo skills work end-to-end.

**Step 5: Session management** — agents maintain state across calls. Session context persists and resumes. Key_Shared subscription ensures session affinity.
*Unlocks: use cases 3, 7 (architecture evaluation, PR review).*

**Step 6: Flow orchestration** — agents declare team skills. Multi-agent operations appear in Claude's catalog as single tools. Supports DAG, parallel, on_failure, nested workflows.
*Unlocks: use case 8 (full development cycle).*

**Step 6.5: Orchestrator agent** — a `BaseRole` backed by a local LLM that dynamically composes flows for tasks that don't fit static declarations. Plans, executes, adapts.

**Step 7: Scheduler integration** — context-aware model routing. Dynamic model selection based on session context size. Distillation as cost control.

**Step 8: Agent decomposition** — agents escape their container processes. Per-agent scaling. Shared agents across teams (with resource budgeting).

Each step is independently useful. Step 1 unblocks the developer roadmap. Step 4 gives Claude a single MCP. Step 5 gives agents memory. Step 6 eliminates Claude-as-orchestrator. Steps 7-8 are the full vision.

---

## Part 8: Platform Qualities

### Resilience and Recovery

| Failure | Response |
|---|---|
| **Agent crashes mid-session** | Bus detects heartbeat timeout. Session is suspended with current state persisted. When an agent re-registers (same instance or new), bus reassigns the session and loads context. Agent resumes. |
| **Agent crashes mid-flow** | The failed step is NACKed. Bus retries with configurable backoff. After max retries, the message routes to a dead letter topic. The flow returns a partial result with the error. |
| **Bus goes down** | Agents queue locally and retry registration on reconnect. Claude sees "bus unavailable" from the MCP transport. Agents can fall back to direct Ollama calls with local model selection for purely-local operations (no cross-agent work possible). |
| **Scheduler unavailable** | Agents fall back to their default model assignment (static ModelPool config). Dynamic routing degrades to manual selection. |

The bus exposes a recovery runbook on initial handshake so Claude (or an operator) can diagnose and remediate failures without guessing.

### Observability

The bus emits **structured trace events** for every request, flow step, session lifecycle change, and agent registration. Events carry a correlation ID that threads through multi-agent flows so a 5-step flow that fails at step 3 can be traced end-to-end.

```
trace_id: t-42
  → step.1: developer.read_spec        [200ms, ok]
  → step.2: researcher.find_relevant   [1.2s, ok]
  → step.3: researcher.paper_context   [800ms, ok]
  → step.4: developer.evaluate         [3.5s, FAILED: timeout]
  → flow: evaluate_spec_against_corpus [FAILED at step 4]
```

Claude (or an operator) can query traces via `bus.trace(trace_id)`. Session history includes trace references so a session's full execution path is auditable.

### Security and Trust

For local-only deployment (current state): all agents are trusted, no auth needed.

For networked deployment (future): the bus is the auth boundary. Considerations for when this matters:

- **Per-agent tokens** — each agent authenticates with the bus using a registration token. The bus validates on every request.
- **Skill-level ACLs** — certain skills can be restricted to specific callers. E.g., `update_fr_status` is callable by developer but not by arbitrary external agents.
- **Team-internal trust** — specialists within a hierarchical team are trusted by their lead without per-call auth. Cross-team calls go through the bus with full validation.

Not needed for solo/local use. Sketch the auth layer in the bus registration protocol so it's a config change, not a rewrite, when needed.

---

## Summary

khonliang started as a library for building multi-role LLM applications. The ecosystem grew: a research pipeline, a development lifecycle tool, an event bus, an inference scheduler. Each piece was built to help Claude sessions work more effectively — better tools, less token waste, more focus on code and decisions.

The new direction recognizes that these pieces form an agent platform. The MCP servers are agents. The bus is the orchestrator. The blackboard is shared memory. The scheduler manages compute. The distillation pipeline manages agent memory. The knowledge store holds everything.

```
khonliang         = agent primitives (BaseRole, AgentTeam, Consensus)
khonliang-bus     = orchestrator (registry, routing, sessions, persistence)
khonliang-scheduler = compute (model assignment, context windows, inference)
researcher-lib    = cognitive tools (distill, decompose, summarize, evaluate)
blackboard        = shared memory (cross-agent, cross-session, public/private)
bus MCP           = presentation layer (Claude's single interface to all of it)
```

Claude goes from juggling multiple MCPs to talking to one bus. Agents go from being trapped in processes to being independent, composable, and durable. The same primitives work at both scales — local and distributed — because they were always the same abstraction.

The tools that help you vibe with Claude toward a goal are the same tools that let agents coordinate with each other. They just happen to overlap.

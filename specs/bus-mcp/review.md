# Architecture Review: From Agent Library to Agent Platform

**Reviewer:** Claude (researcher session)
**Document:** `specs/bus-mcp/architecture.md`
**Date:** 2026-04-10
**Method:** Cross-referenced against the researcher corpus (500+ distilled papers), today's conversation direction, and newly-ingested multi-agent architecture papers from Microsoft, Rasa, Yutori, arxiv, dev.to, and JavaPro.

---

## Summary

The architecture document proposes evolving khonliang from an agent library into an agent platform by making the bus the central nervous system — routing skills, managing sessions, orchestrating multi-agent flows, and serving as the single MCP that Claude connects to. The key insight is that Claude's MCP helper tools and a real distributed agent platform are the same abstraction at different transport scales.

This review evaluates the proposal against what the literature says about multi-agent orchestration, event bus design for AI agents, and the practical patterns emerging in production systems.

---

## Part 1: What the Literature Supports

### The "everything is an agent" abstraction holds

The architecture doc's core table — mapping `BaseRole` to distributed agent, `AgentTeam` to remote team, `ConsensusEngine` to distributed consensus — is well-supported by the literature:

- **Microsoft's "Designing Multi-Agent Intelligence"** describes the same pattern: agents as modular, composable units with skills that can be orchestrated into teams. Microsoft's Magentic-One framework uses an "Orchestrator" agent that coordinates specialist agents (WebSurfer, FileSurfer, Coder, ComputerTerminal) — essentially the bus MCP concept with a designated lead agent instead of a declarative flow engine.

- **Rasa's A2A + MCP integration** directly validates the "one bus, many agents" pattern. Rasa positions itself as the orchestration layer that connects to agents via Google's Agent-to-Agent (A2A) protocol while exposing MCP tools to LLMs. Their architecture: client → Rasa orchestrator → multiple A2A agents. This maps cleanly to: Claude → bus MCP → multiple bus agents. The key difference is that Rasa uses A2A as the inter-agent protocol; our architecture uses bus pub/sub + request/reply. Both achieve the same goal: Claude talks to one interface, agents coordinate behind it.

- **AgentsNet (arxiv)** provides evaluation frameworks for exactly this kind of multi-agent coordination, measuring how well agents maintain coherence when reasoning collaboratively. Their message-passing protocol for agent coordination is conceptually similar to bus-mediated request/reply.

**Verdict:** The abstraction is sound and maps to established patterns in both industry (Microsoft, Rasa) and academia (AgentsNet, AutoGen).

### Session management with public/private state has precedent

The architecture doc's session model (public findings + private working memory, with distillation across tiers) aligns with several corpus papers:

- **"LLM-based Multi-Agent Blackboard System for Information Discovery"** (arxiv 2510.01285) is the most directly relevant paper in the corpus. It describes a blackboard architecture where multiple LLM agents post findings to a shared blackboard, read each other's work, and build on prior results. The blackboard has sections (shared context, per-agent working memory) that map to our public/private split. The paper validates that blackboard-mediated coordination outperforms direct agent-to-agent messaging for information discovery tasks.

- **Yutori's "Building the Proactive Multi-Agent Architecture Powering Scouts"** describes a production multi-agent system where agents maintain session state across interactions, with proactive behavior (agents initiate actions based on accumulated context, not just user requests). Their architecture separates "scout" agents (autonomous, proactive) from "assistant" agents (reactive, user-facing) — a pattern that maps to our bus agents (autonomous) vs. Claude-facing tools (reactive).

**Verdict:** Session management with shared + private state is validated by both academic research (blackboard systems) and production systems (Yutori). khonliang's existing Blackboard primitive (with public/private sections) is already the right shape — extending it to distributed sessions via the bus is a natural progression.

### Event bus design needs more than pub/sub

The JavaPro/Pulsar analysis and the dev.to message bus article both argue that AI agents need **dual semantics** (stream + queue) from their event bus:

- **Stream semantics** (broadcast, ordered, replayable): for events like `researcher.paper_distilled` where multiple subscribers need the same data. This is what khonliang-bus currently provides well.

- **Queue semantics** (competing consumers, per-message ack, retry, dead letter): for task distribution like "evaluate this spec" where exactly one agent should handle it, with retry on failure. khonliang-bus has ack support but **lacks NACK (negative acknowledgment), dead letter queues, and competing-consumer modes**.

- **Per-message acknowledgment** is critical for agent reliability. The architecture doc's flow orchestration engine would need this: if step 3 of a 5-step flow fails, the bus needs to know *which specific message* failed, not just "the consumer is behind." khonliang-bus currently does per-message ack but not NACK/retry.

**Verdict:** The bus needs to grow beyond pure pub/sub before it can serve as a flow orchestration engine. Specifically: NACK with configurable retry delay, dead letter topics for poison messages, and a shared subscription mode for competing consumers. These are well-understood patterns (Pulsar, RabbitMQ, NATS JetStream all implement them). The Go implementation can follow established designs.

### Dynamic model routing is novel but grounded

The architecture doc's proposal — agents dynamically scale between 3B/7B/32B models based on context size, with distillation as the mechanism to drop back to cheaper models — doesn't appear in any paper in the corpus as a complete system. However, the components exist:

- **`select_best_of_n`** (researcher-lib) already does the quality-selection part
- **khonliang-scheduler** (Go) already has GPU slot management and model placement logic
- **The distillation pipeline** (researcher) already compresses documents

What's novel is connecting them: the scheduler watches context growth, triggers distillation when context hits 75% of current model's max, then routes to a smaller model. No paper in the corpus describes this closed-loop pattern. It's genuinely new ground.

**Verdict:** The components exist; the integration is novel. Worth building incrementally: start with manual model selection (which already works), add context-size monitoring, then add automatic distillation triggers. Don't try to ship the full closed loop in one milestone.

---

## Part 2: What the Literature Challenges

### Flow orchestration complexity is underestimated

The architecture doc presents collaborative skills as declarative flows:

```yaml
flow:
  - call: developer.read_spec
  - call: researcher.find_relevant
  - call: researcher.paper_context
  - call: developer.evaluate_with_evidence
```

This looks simple. The reality is harder:

- **Microsoft's Magentic-One** tried declarative multi-agent flows and found that an "Orchestrator" agent (an LLM that plans and adapts) outperformed static flow declarations for complex tasks. Static flows break when step 2 returns unexpected output, or when the optimal next step depends on intermediate results. Microsoft's solution: the orchestrator is itself an agent that plans dynamically.

- **Rasa's orchestration** similarly uses an LLM-powered router rather than static flows. The Rasa agent decides which sub-agent to call next based on conversation context, not a predefined sequence.

- **The "Federation of Agents" paper** (arxiv) describes "policy-aware coordination" where agents negotiate task decomposition dynamically rather than following a static flow graph.

**Implication for the architecture:** Simple linear flows (step 1 → step 2 → step 3) are fine for MVP. But "evaluate this spec against the corpus" is already non-linear — the set of papers to evaluate against depends on what `find_relevant` returns, which depends on the spec's topic. The flow engine should support conditional branching and dynamic step generation from the start, or acknowledge that an LLM-powered orchestrator agent is the right abstraction for complex flows.

**Recommendation:** Start with static linear flows (Step 6 in the migration). Add an "orchestrator agent" role (Step 6.5) that can dynamically compose flows for tasks that don't fit static declarations. This matches Microsoft's and Rasa's findings while keeping the initial implementation simple.

### "Agents without containers" has operational cost

The architecture doc envisions agents escaping their container processes:

> The summarizer isn't "researcher's summarizer" — it's a summarizer. Developer can use it too.

This is conceptually clean but operationally complex:

- **Yutori's production experience** shows that shared agents create resource contention. Their "scout" agents are intentionally isolated to prevent one task from starving another. Shared agents need explicit resource budgeting.

- **DMAS-Forge** (in the corpus) provides a framework for transparent distributed deployment of AI apps. Their findings: shared components need service-level objectives (SLOs), health monitoring, and graceful degradation per consumer. A summarizer serving both researcher and developer needs to prioritize one when both are busy.

**Recommendation:** The architecture doc is right that agents *can* be shared. But the migration should keep agents process-local until there's a concrete demand for sharing. Don't extract the summarizer from researcher until developer actually needs it and can't just instantiate its own. Step 8 in the migration is correctly last.

### The single-MCP model has a discovery problem

The architecture doc proposes Claude connects to one bus MCP that exposes all skills from all agents. With 46 researcher skills + 8 developer skills + future private-app skills, Claude's tool catalog could exceed 100 entries.

- **MCP-Zero** (in the corpus) and our own FR `fr_khonliang_a802360f` (Lazy MCP Tool Registration) address this: load tool schemas on-demand, not all at once. The bus MCP would need the same pattern — expose a compact catalog of agent capabilities, load full tool schemas only when Claude indicates intent to use a skill.

- **The "10 Strategies to Reduce MCP Token Bloat"** article (in the corpus) catalogs specific techniques for keeping MCP tool surfaces manageable. Several apply directly: tool grouping by agent, progressive disclosure, compact descriptions.

**Recommendation:** The bus MCP must implement lazy tool loading from day one. If Claude sees 100+ tool schemas on connect, context is already half gone before any work starts. The skill registry should expose *descriptions* (1-2 lines each) and load *schemas* (parameters, types) only on first call. This aligns with the lazy MCP FR that was already tracked for khonliang.

---

## Part 3: What the Literature Adds That the Architecture Doc Doesn't Cover

### Bus needs queue semantics: NACK, retry, dead letter (Pulsar research)

The Pulsar idea research (`bff1978874c58a24`) ran 7 queries, found 10 papers, distilled 7, and evaluated 7 claims. Results: 5 supported, 1 partially supported, 1 unaddressed.

**Key findings relevant to khonliang-bus evolution:**

1. **Managed retries outperform ad-hoc retry** (Supported — RetryGuard paper). The bus should own retry logic, not leave it to each agent. When an agent NACKs a message, the bus should handle backoff, retry count tracking, and dead-letter routing. khonliang-bus currently has per-message ack but **no NACK or retry**. This is a prerequisite for the flow orchestration engine (architecture doc Step 4+).

2. **Shared subscriptions scale better than partition-based scaling** (Supported — Kafka event streaming analysis). khonliang-bus currently supports topic-based pub/sub. For competing consumers (e.g., a pool of summarizer agents), the bus needs a **shared subscription mode** where each message goes to exactly one subscriber in the group. Without this, scaling agent pools requires the caller to do their own load balancing. Pulsar's Key_Shared mode (order-per-key, parallelism-per-group) is especially relevant for agent sessions — all messages for session X go to the same agent, but different sessions can go to different agents in parallel.

3. **Dead letter queues prevent poison messages** (Unaddressed in literature, but a well-known operational pattern). After N failed delivery attempts, the bus should route the message to a dead-letter topic instead of retrying forever. Without this, a single malformed message blocks its entire subscription. khonliang-bus should add this alongside NACK support.

4. **Protocol flexibility validates pluggable backends** (Supported — TADP-RME paper). khonliang-bus's design (memory/SQLite/Redis backends, HTTP+WebSocket transport) aligns with the literature's recommendation for flexible, adaptive bus architectures. The architecture doc's future gRPC transport fits this pattern.

5. **Unified bus reduces glue code** (Supported). The literature agrees that running separate stream (Kafka) and queue (RabbitMQ/SQS) systems creates synchronization overhead. A single bus with dual semantics (which khonliang-bus is trending toward) eliminates an entire class of integration bugs.

**Concrete bus evolution FRs implied by this research (not yet promoted — aligned with architecture doc Step 1):**
- NACK with configurable retry delay and max-retry count
- Dead letter topics (per-subscription, queryable for debugging)
- Shared subscription mode (competing consumers within a subscriber group)
- Key_Shared subscription mode (ordered per key, parallel across keys — for session affinity)

These are well-understood patterns implemented by Pulsar, NATS JetStream, and RabbitMQ. The Go implementation can follow established designs without novel research.

### A2A (Agent-to-Agent) protocol compatibility

**Rasa's blog post** shows that Google's A2A protocol is emerging as a standard for agent interoperability. A2A defines:
- Agent Cards (capability declaration — analogous to our skill registration)
- Task lifecycle (send, working, completed, failed — analogous to our session management)
- Streaming artifacts (partial results during execution)

The architecture doc's skill registration (Layer 2) and collaborative skills (Layer 3) are custom versions of A2A Agent Cards and Task lifecycle. **If the bus spoke A2A natively (or via an adapter), our agents could interoperate with external A2A agents** — any A2A-compliant agent (Google, Rasa, third-party) could join the bus without custom integration.

**Recommendation:** Don't implement A2A in the bus today, but design the skill registry schema to be A2A-compatible. When A2A stabilizes (it's still early), adding a protocol adapter should be a plugin, not a rewrite. The registration schema in §Agent Registration is already close to A2A's Agent Card format — just needs a few fields (streaming support, authentication requirements).

### Hierarchical agent organization

**"A Novel Hierarchical Multi-Agent System"** (arxiv 2602.24068) argues that flat agent networks don't scale. Their finding: agents organized into hierarchies (team leads → specialists) outperform flat peer-to-peer coordination on complex tasks. The hierarchy provides:
- Reduced communication overhead (specialists talk to their lead, not to every other agent)
- Natural task decomposition (leads decompose and delegate)
- Fault containment (a specialist failure only affects its team, not the whole network)

The architecture doc's model is flat — all agents are peers on the bus. For 2-3 agents this is fine. For private-app's 6 specialists + researcher + developer + future agents, a flat model means O(N²) potential interactions.

**Recommendation:** The bus's team concept (§Agent Registration Layer 3) is already a partial answer — teams group agents into units. Extend this to support hierarchical teams where a team lead agent routes requests to specialists. The lead is the only agent that appears in the outer catalog; specialists are internal to the team. This maps to private-app's natural structure: "private-app" as a team lead, with quant/risk/macro/sentiment/compliance/execution as specialists that the lead coordinates.

### Proactive agents (not just reactive)

**Yutori's architecture** distinguishes between:
- **Reactive agents** — respond to requests (all our current agents)
- **Proactive agents** — initiate actions based on accumulated context and observed patterns

Example: researcher today is reactive — it responds when Claude calls `find_papers`. A proactive researcher would notice "3 new arxiv papers on multi-agent consensus were published today" and automatically ingest + distill + score them, then notify developer via the bus that new evidence is available for active specs.

The architecture doc mentions bus events (`researcher.paper_distilled`) but frames them as outputs of explicit operations. **Proactive behavior** — agents that run background tasks and publish discoveries without being asked — isn't addressed.

**Recommendation:** The bus + scheduler infrastructure already supports this. An agent with a cron-like trigger (or a bus subscription to a timer topic) can run periodic background tasks. The researcher's RSS pipeline (`browse_feeds`) is already proto-proactive — it just needs to be scheduled and its outputs published to the bus automatically. Add "proactive capabilities" to the skill registration schema: `proactive: { trigger: "cron", schedule: "0 */6 * * *", description: "scan RSS feeds for new papers" }`.

---

## Part 4: Alignment with Today's Conversation

The architecture doc crystallizes several threads from today's session:

| Conversation thread | Architecture doc location | Status |
|---|---|---|
| "Bus is the agreed-upon protocol, everything else is internal" | §Design principle: independent storage + MCP-to-MCP for narrow interfaces | Decided. Spec rev 2 adopted Architecture A. |
| "Multiple services sharing the bus, not just two" | §Everything Is an Agent: bus as central nervous system | Decided. Informs all migration steps. |
| "Recovery, not bypass when bus is down" | Not in doc | **Gap.** Should add a §Resilience section covering recovery runbook exposure via the bus MCP's initial handshake. |
| "Private-app agents could migrate from monolith to bus" | §Agents Without Containers: "The summarizer isn't researcher's summarizer" | Implied. The private-app migration path should be explicit — same incremental approach as the 8-step migration, applied to private-app's 6 specialists. |
| "The spec review pipeline — local LLMs evaluate, Claude just codes" | §Context Distillation + §Dynamic Model Routing | Extends. The spec review pipeline is a specific instance of the general pattern: local LLMs handle evaluation, Claude handles implementation. The architecture doc generalizes this beyond specs to all agent work. |
| "Every idea → research → FR → spec → milestone → implement" | Not in doc | **Gap.** The research-first workflow is a process, not an architecture feature. But it should be referenced as the intended usage pattern for the developer agent's collaborative skills. |

---

## Part 5: What's Missing from the Architecture Doc

### 1. Resilience and recovery

No section on what happens when the bus is down, when an agent crashes mid-flow, or when the scheduler is unavailable. Today's conversation established the principle (recovery, not bypass), but the doc needs a §Resilience section covering:
- Bus recovery runbook exposed on initial handshake
- Agent crash → session reassignment (mentioned briefly in §Agents Without Containers but needs detail)
- Flow step failure → retry/NACK/dead-letter (depends on bus growing queue semantics)
- Scheduler unavailable → agents fall back to direct Ollama calls with local model selection

### 2. Security and trust boundaries

When agents can call each other's skills, who authorizes what? The doc mentions "auth (planned)" for the bus but doesn't discuss:
- Can developer call researcher's `update_fr_status` directly, or must it go through the bus?
- Can an arbitrary external A2A agent join the bus and call `synergize`?
- Token-based auth per agent? Per skill? Per team?

For a local-only deployment this is fine. For anything networked (even across machines in a home lab), trust boundaries matter. Add to the "planned" list with a rough sketch.

### 3. Observability

No mention of tracing, metrics, or debugging multi-agent flows. When a 5-step flow fails at step 3, how does the operator (or Claude) trace what happened? The bus should emit structured trace events that can be correlated across agents and sessions. This is standard distributed systems practice but easy to forget until it's needed.

### 4. Versioning and backward compatibility

The doc mentions version requirements in collaborative skills (`requires: researcher>=0.5.0`) but doesn't discuss what happens during rolling upgrades. If researcher upgrades to 0.7.0 and changes the shape of `find_relevant`'s output, developer's flow that depends on it breaks. Schema versioning for skill inputs/outputs is needed — even if it's just "additive-only changes are safe, breaking changes require a major version bump."

---

## Part 6: Corpus Evidence Summary

Papers most relevant to the architecture doc's claims, from the current researcher corpus:

| Paper | Relevance to architecture doc |
|---|---|
| **LLM-based Multi-Agent Blackboard System** (arxiv 2510.01285) | Directly validates blackboard-mediated agent coordination with public/private state. Strongest evidence for the session model. |
| **Microsoft: Designing Multi-Agent Intelligence** | Validates agent-as-modular-skill pattern. Challenges static flow declarations — recommends LLM-powered orchestrator for complex tasks. |
| **Rasa: Orchestrating A2A and MCP** | Validates "one bus, many agents" pattern. Introduces A2A protocol compatibility as a future consideration. |
| **Yutori: Proactive Multi-Agent Architecture** | Validates session state across interactions. Introduces proactive agents as a missing pattern. Warns about shared-agent resource contention. |
| **dev.to: Message Bus for 20+ Autonomous Agents** | Validates lightweight bus design. Practical patterns for agent discovery and health tracking. |
| **JavaPro: Protocol-Flexible Event Bus (Pulsar)** | Argues for dual stream+queue semantics, per-message ack, NACK, dead letter queues. Identifies gaps in khonliang-bus's current capabilities. |
| **Federation of Agents** (arxiv) | Supports dynamic task decomposition over static flows. Policy-aware coordination. |
| **Hierarchical Multi-Agent System** (arxiv 2602.24068) | Argues flat agent networks don't scale. Recommends hierarchical organization with team leads. |
| **DMAS-Forge** | Framework for transparent distributed AI deployment. Operational considerations for shared agents (SLOs, graceful degradation). |
| **CircuitLM** | Multi-agent design framework using natural language. Demonstrates the "agents as composable units" pattern in a domain-specific context. |
| **MCP-Zero** | Proactive toolchain construction for LLMs. Relevant to lazy tool loading in the bus MCP. |
| **RetryGuard** (via Pulsar research) | Managed retries outperform standard/adaptive retry policies in cost and resource usage. Direct evidence for bus-owned retry logic over agent-side ad-hoc retries. |
| **TADP-RME** (via Pulsar research) | Supports protocol flexibility for heterogeneous systems communicating via a single bus. Validates the pluggable-backend design of khonliang-bus. |
| **Kafka Event Streaming Analysis** (via Pulsar research) | Confirms Kafka's partitioning-based scaling creates operational complexity that shared-subscription models (Pulsar, and by extension khonliang-bus) avoid. Identifies rebalancing pauses as a real production issue. |
| **AutoGen, EvoAgent, AgentsNet, GRA, AIOS** | Academic multi-agent frameworks that validate individual components (conversation, evolution, coordination, review, scheduling). |

---

## Verdict

The architecture document is **strong on vision, well-grounded in existing primitives, and mostly validated by the literature**. The core insight (helper tools = agent platform) holds up. The migration path is incremental and each step ships value.

**What to add before building:**
1. §Resilience section (recovery runbook, crash handling, flow failure)
2. A note on A2A protocol compatibility in the skill registry design
3. Acknowledgment that complex flows need an LLM-powered orchestrator, not just static declarations
4. Hierarchical team support in the registration schema
5. Proactive agent capabilities in the skill registry
6. Observability/tracing for multi-agent flows
7. Versioning strategy for skill schemas

**What to build first (unchanged from the doc):**
Step 1 (request/reply) → Step 2 (skill registry) → Step 3 (BusAgent adapter) → Step 4 (bus becomes master MCP). These four steps are well-supported by the literature and each delivers independent value. Steps 5-8 are where the novel territory begins and where iterative design (build, learn, adjust) matters more than upfront specification.

**What the researcher pipeline revealed:**
The synergize run against the architecture doc + corpus produced FRs focused on paper-backed frameworks (AutoGen, GRA, AgentsNet) rather than the doc's own novel concepts (RemoteRole, BusAgent, flow orchestration). This confirms the pipeline's limitation: it finds evidence for *what the literature says to build* but not for *what your own design docs say to build*. The spec evaluation pipeline (developer MS-02) is the right tool for internal-doc assessment — synergize is for external-evidence discovery.

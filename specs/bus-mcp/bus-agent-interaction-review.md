# Bus ↔ Agent Interaction Protocol Review

**Reviewer:** Claude (developer session)
**Document:** `specs/bus-mcp/bus-agent-interaction.md`
**Date:** 2026-04-10

---

## TL;DR

Implementation-ready protocol spec. The request lifecycle walkthroughs are concrete enough to code from. The SQLite schema is well-designed with clean runtime/persistent separation. The 3-layer health monitoring catches every failure mode. The bus-MCP adapter section nails the Claude-facing experience: one `.mcp.json` line, dynamic tool refresh, progressive disclosure.

One required fix (private name in a payload example), four design clarifications, no structural changes.

---

## Required fix

Line 621: pub/sub event payload contains `"private-app": 0.41` — verify this was updated per the agents.md review. (If not, replace with a generic project name.)

---

## Design observations

### 1. Progressive disclosure — clarify the startup sequence

§Bus-MCP Adapter steps 3-4 say "Generate @mcp.tool() for each skill" which implies full schemas loaded on connect. §Progressive disclosure (line 469) says schemas are fetched on first call. Both describe the same thing but could confuse an implementer.

**Recommendation:** Reword steps 3-4 to: "Generate @mcp.tool() stubs for each skill (name + description only; full parameter schemas are lazy-loaded on first invocation)." Removes the ambiguity.

### 2. Sessions table — `agent_id` is singular but flows are multi-agent

`sessions.agent_id TEXT NOT NULL` binds a session to one agent. For single-agent sessions (use cases 1, 4, 5, 6, 7) this is correct. For multi-agent flows (use cases 2, 3, 8), a session spans multiple agents.

Two interpretations that both work:
- **(A)** `agent_id` is the lead/initiating agent. The flow engine tracks per-step agent routing in the `traces` table. Sessions are owned by one agent; flows are tracked separately.
- **(B)** Add a `session_agents` join table for multi-agent sessions.

**Recommendation:** (A) is simpler and sufficient. The traces table already records which agent handled each step. The session's `agent_id` is "who owns the context" (the lead), not "who participated." Note this in the schema comments so future readers don't wonder.

### 3. Flows reference agent IDs vs types

The registration example (line 587+) uses concrete IDs in flow steps: `researcher-primary.find_papers`, `developer-platform.ingest_frs`. The architecture doc uses types: `researcher.find_relevant`, `developer.evaluate_with_evidence`.

**Impact:** ID-based references break if instances are renamed or if the bus should pick a healthy instance from a pool. Type-based references let the bus resolve to an available instance at execution time.

**Recommendation:** Flows reference agent **types** (e.g., `researcher.find_papers`). The bus resolves type → healthy registered instance at each flow step. For cases where a specific instance matters, allow an optional `agent_id` override in the step definition. Document the default as type-based resolution.

### 4. Retry configuration — where does it live?

The `retryable: true` flag in error responses is smart but the retry policy (delay, max attempts, backoff strategy) isn't specified in the protocol.

**Recommendation:** Three levels, each overrides the previous:
- **Global default** in bus config (`retry: { delay: 2s, max: 3, backoff: exponential }`)
- **Per-skill** in agent registration (`"retry": { "max": 5 }` alongside the skill definition)
- **Per-flow-step** in collaboration declaration (`"retry": { "max": 1 }` on a specific step)

Document in the protocol spec so implementers know where to look.

---

## What's right (keep as-is)

- **Complete API surface** — lifecycle, registration, request/reply, flows, sessions, pub/sub, observability. Everything a developer needs to implement both sides.
- **SQLite schema** — runtime tables (registrations, skills, flows) rebuilt from re-registrations on boot. Persistent tables (installed_agents, messages, sessions, traces) survive restarts. Clean separation, no orphaned state.
- **Request lifecycle walkthroughs** — step-by-step HTTP calls for solo skills and collaborative flows, with trace IDs threaded through. An implementer could code the bus router from §Request Lifecycle alone.
- **3-layer health monitoring** — PID check (instant, no network) → heartbeat (application-level) → HTTP probe (service-level). The detection→response matrix covers crashed, hung, overloaded, and healthy states with the right action for each.
- **`retryable` flag** — agent-side intelligence about whether retry makes sense. Prevents the bus from futilely retrying permanent errors (skill not found) while allowing transient retries (timeout, Ollama overloaded).
- **Flow failure with partial results** — failed flows return completed steps + the error, not just "failed." Claude can decide what to do with partial data.
- **Bus-MCP adapter as a separate process** — it's a bus client, not part of the bus service. Clean separation. The adapter can restart without affecting the bus or agents.
- **Dynamic tool refresh via `bus.registry_changed`** — agents come and go; Claude's catalog stays current without MCP reconnect.
- **Recovery runbook in initial handshake** — Claude knows how to fix the bus, not how to bypass it. Aligns with the architecture doc's "recovery, not bypass" principle.
- **One-line `.mcp.json`** — the entire platform behind `python -m khonliang_bus.mcp --bus http://localhost:8787`. The payoff of the whole architecture in one config entry.
- **Two-endpoint agent protocol** — `POST /v1/handle` + `GET /v1/health`. That's all `BaseAgent` needs to implement. Agent authors never see HTTP.

---

## Verdict

**Approved with the required fix and design clarifications above.** This is a concrete, implementable protocol spec. The request lifecycle walkthroughs, SQLite schema, and health monitoring design are all ready to build from. The bus-MCP adapter section correctly captures the Claude-facing experience.

Together with `agents.md`, this pair covers everything needed to implement both sides of the agent platform protocol.

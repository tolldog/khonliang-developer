# Bus ↔ Agent Interaction Protocol

How the bus service and agents communicate. Covers the API surface,
message formats, request routing, health monitoring, and the full
request lifecycle from Claude to agent and back.

---

## Overview

```
Claude ──stdio──▶ bus-mcp adapter ──HTTP──▶ bus service ──HTTP──▶ agent
                  (in bus-lib)              (the platform)         (callback URL)
```

Three participants, two protocols:
- **Claude ↔ bus-mcp:** MCP over stdio (standard MCP tool calls)
- **bus-mcp ↔ bus:** HTTP (bus client API)
- **bus ↔ agent:** HTTP (bus-to-agent request/reply)

The bus is the center. Everything routes through it. The bus-mcp adapter
and agents are both clients of the bus — one upstream (translating from
Claude), one downstream (handling work).

---

## Bus Service API

The bus exposes these endpoints. All communication is JSON over HTTP.

### Agent Lifecycle

```
POST   /v1/install              — persist agent startup metadata
DELETE /v1/install/{id}         — remove agent from platform
GET    /v1/install              — list all installed agents
POST   /v1/install/{id}/start   — start a specific agent
POST   /v1/install/{id}/stop    — stop a specific agent
POST   /v1/install/{id}/restart — restart a specific agent
```

### Agent Registration (runtime)

```
POST   /v1/register             — agent calls home with port, PID, skills
POST   /v1/deregister           — agent announces clean shutdown
POST   /v1/heartbeat            — agent proves it's alive
GET    /v1/services             — list running agents + their skills
```

### Request/Reply

```
POST   /v1/request              — route a request to an agent's skill
```

### Flows

```
POST   /v1/flow                 — execute a declared multi-agent flow
GET    /v1/flows                — list available collaborative flows
```

### Pub/Sub (existing)

```
POST   /v1/publish              — publish an event to a topic
GET    /v1/subscribe            — WebSocket subscription to topics
POST   /v1/ack                  — acknowledge a received message
POST   /v1/nack                 — negative-acknowledge (trigger retry)
```

### Sessions

```
POST   /v1/session              — create a new session
GET    /v1/session/{id}         — get session state
POST   /v1/session/{id}/message — send a message within a session
POST   /v1/session/{id}/suspend — suspend (persist context)
POST   /v1/session/{id}/resume  — resume (reload context)
DELETE /v1/session/{id}         — archive and close
```

### Observability

```
GET    /v1/health               — bus health
GET    /v1/trace/{trace_id}     — execution trace for a request/flow
GET    /v1/status               — full platform status (agents, sessions, flows)
```

---

## Bus Database Schema

The bus stores everything in its own SQLite DB (`bus.db`). This is the
bus's persistent state — independent of any agent's storage.

```sql
-- What to start on boot
CREATE TABLE installed_agents (
    id           TEXT PRIMARY KEY,
    agent_type   TEXT NOT NULL,
    command      TEXT NOT NULL,
    args         TEXT NOT NULL,     -- JSON array
    cwd          TEXT NOT NULL,
    config       TEXT NOT NULL,     -- absolute path to agent config
    installed_at TEXT NOT NULL      -- ISO timestamp
);

-- Who's running now (rebuilt on each boot from re-registrations)
CREATE TABLE registrations (
    id           TEXT PRIMARY KEY,
    agent_type   TEXT NOT NULL,
    callback_url TEXT NOT NULL,
    pid          INTEGER NOT NULL,
    version      TEXT,
    registered_at TEXT NOT NULL,
    last_heartbeat TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'healthy'  -- healthy | unhealthy | dead
);

-- Skills per agent (rebuilt from registration payloads)
CREATE TABLE skills (
    agent_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    description  TEXT,
    parameters   TEXT,              -- JSON schema
    PRIMARY KEY (agent_id, name),
    FOREIGN KEY (agent_id) REFERENCES registrations(id)
);

-- Collaborative flows (declared by agents at registration)
CREATE TABLE flows (
    name         TEXT PRIMARY KEY,
    declared_by  TEXT NOT NULL,     -- agent that declared it
    description  TEXT,
    requires     TEXT,              -- JSON array of agent_type requirements
    steps        TEXT NOT NULL,     -- JSON array of flow steps
    FOREIGN KEY (declared_by) REFERENCES registrations(id)
);

-- Pub/sub messages (durable subscriptions)
CREATE TABLE messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    topic        TEXT NOT NULL,
    payload      TEXT NOT NULL,     -- JSON
    source       TEXT NOT NULL,     -- agent_id that published
    created_at   TEXT NOT NULL,
    sequence     INTEGER NOT NULL   -- per-topic monotonic
);

-- Sessions (persistent across agent restarts).
-- agent_id is the lead/owning agent — the one whose context this is.
-- For multi-agent flows, per-step agent routing is tracked in the
-- traces table, not here. The session owner is "who holds the context,"
-- not "who participated."
CREATE TABLE sessions (
    id           TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,     -- lead/owning agent (not all participants)
    status       TEXT NOT NULL,     -- active | suspended | archived
    public_ctx   TEXT,              -- JSON (shared, readable by other agents)
    private_ctx  TEXT,              -- JSON (agent-only working memory)
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Execution traces
CREATE TABLE traces (
    trace_id     TEXT NOT NULL,
    step         INTEGER NOT NULL,
    agent_id     TEXT,
    operation    TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT,              -- ok | failed | timeout
    duration_ms  INTEGER,
    error        TEXT,
    PRIMARY KEY (trace_id, step)
);
```

**Runtime tables** (`registrations`, `skills`, `flows`) are rebuilt on
every boot from agent re-registrations. They don't need to survive bus
restarts because agents re-register when they reconnect.

**Persistent tables** (`installed_agents`, `messages`, `sessions`, `traces`)
survive bus restarts. They're the bus's durable state.

---

## Request Lifecycle

### Solo skill call (Claude → agent)

```
1. Claude calls a tool:
     tools/call: { name: "researcher-primary.find_papers", arguments: { query: "consensus" } }

2. bus-mcp adapter receives the MCP call
     Parses agent_id ("researcher-primary") and skill ("find_papers")
     POST http://localhost:8787/v1/request
     {
       "agent_id": "researcher-primary",
       "operation": "find_papers",
       "args": { "query": "consensus" },
       "timeout": 30,
       "trace_id": "t-42"
     }

3. Bus receives the request
     Looks up researcher-primary in registrations table
     callback_url = "http://localhost:9247"
     Records trace step 1: agent=researcher-primary, op=find_papers, started

4. Bus forwards to agent
     POST http://localhost:9247/v1/handle
     {
       "operation": "find_papers",
       "args": { "query": "consensus" },
       "correlation_id": "req-abc-123",
       "trace_id": "t-42"
     }

5. Agent handles the request
     @handler("find_papers") dispatches to the skill function
     Returns: { "correlation_id": "req-abc-123", "result": [...papers...] }

6. Bus receives the response
     Records trace step 1: finished, status=ok, duration=1200ms
     Returns result to bus-mcp adapter

7. bus-mcp adapter returns to Claude
     tools/call response: { content: [...papers...] }
```

**Total hops:** Claude → bus-mcp → bus → agent → bus → bus-mcp → Claude
**Network cost:** 2 localhost HTTP round-trips (~2ms total)
**Token cost:** zero (coordination is local, not in Claude's context)

### Collaborative flow (Claude → multiple agents)

```
1. Claude calls a collaborative tool:
     tools/call: { name: "evaluate_spec_against_corpus", arguments: { path: "/path/to/spec.md" } }

2. bus-mcp adapter → bus
     POST /v1/flow
     {
       "flow_id": "evaluate_spec_against_corpus",
       "args": { "path": "/path/to/spec.md" },
       "trace_id": "t-43"
     }

3. Bus loads the flow definition from the flows table:
     step 1: call developer.read_spec       { path: "{{path}}" }              → output: spec
     step 2: call researcher.find_relevant  { query: "{{spec.title}}" }       → output: papers
     step 3: call researcher.paper_context  { query: "{{spec.title}}" }       → output: evidence
     step 4: call developer.evaluate        { spec: "{{spec}}", evidence: "{{evidence}}" } → output: evaluation

4. Bus executes step 1:
     POST http://localhost:9003/v1/handle  (developer)
     { "operation": "read_spec", "args": { "path": "/path/to/spec.md" } }
     → receives spec content
     Records trace: step 1, developer, read_spec, ok

5. Bus executes step 2 (uses output from step 1):
     POST http://localhost:9247/v1/handle  (researcher)
     { "operation": "find_relevant", "args": { "query": "MS-01: Developer MCP skeleton" } }
     → receives paper list
     Records trace: step 2, researcher, find_relevant, ok

6. Bus executes step 3:
     POST http://localhost:9247/v1/handle  (researcher)
     { "operation": "paper_context", "args": { "query": "MS-01: Developer MCP skeleton" } }
     → receives evidence
     Records trace: step 3, researcher, paper_context, ok

7. Bus executes step 4 (uses outputs from steps 1 AND 3 — DAG reference):
     POST http://localhost:9003/v1/handle  (developer)
     { "operation": "evaluate", "args": { "spec": "...", "evidence": "..." } }
     → receives evaluation
     Records trace: step 4, developer, evaluate, ok

8. Bus returns final result to bus-mcp adapter → Claude
     The 4-step flow appears as one tool call to Claude.
```

**If step 3 fails:**
- Bus records trace: step 3, researcher, paper_context, FAILED
- Bus NACKs the request (if retry is configured for this step)
- After max retries: flow returns partial result with error at step 3
- Dead letter: the failed request is stored for debugging

### Flow step failure with retry

```
Step 3 fails (researcher timeout):
  → Bus retries after 2s delay
  → Step 3 fails again
  → Bus retries after 4s delay (exponential backoff)
  → Step 3 succeeds
  → Flow continues to step 4

Step 3 fails 3 times (max retries):
  → Bus routes to dead letter topic: "flow.evaluate_spec.step3.dead"
  → Flow returns:
    {
      "status": "partial",
      "completed_steps": [1, 2],
      "failed_step": 3,
      "error": "researcher.paper_context timed out after 3 attempts",
      "partial_result": { "spec": "...", "papers": "..." }
    }
  → Claude sees the partial result and can decide what to do
```

### Retry configuration hierarchy

The `retryable: true` flag in error responses tells the bus whether retry
makes sense. The retry *policy* (delay, max attempts, backoff) is
configured at three levels, each overriding the previous:

**Level 1 — Global default** (bus config):

```yaml
retry:
  delay: 2s
  max_attempts: 3
  backoff: exponential    # delay doubles each attempt
```

**Level 2 — Per-skill** (agent registration, overrides global):

```json
{
  "name": "start_distillation",
  "description": "Run LLM distillation on pending papers",
  "retry": { "max_attempts": 5, "delay": "10s" }
}
```

Heavy operations get more attempts and longer delays.

**Level 3 — Per-flow-step** (collaboration declaration, overrides per-skill):

```json
{
  "call": "researcher.paper_context",
  "args": { "query": "{{spec.title}}" },
  "output": "evidence",
  "retry": { "max_attempts": 1 }
}
```

A flow step that's known to be idempotent might get more retries; a step
with side effects might get exactly one attempt.

**Resolution order:** flow-step → skill → global default. If none specify
a retry policy, the global default applies. If the agent's error response
says `retryable: false`, no retry regardless of policy.

---

## Health Monitoring

Three layers, each catches different failure modes:

### Layer 1: PID check (instant, no network)

```python
# Bus checks periodically (every 5s)
import os

def check_pid(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # signal 0 = existence check
        return True
    except ProcessLookupError:
        return False
```

If PID is dead → agent crashed → bus can restart immediately without
waiting for heartbeat timeout. Fastest detection path.

### Layer 2: Heartbeat (application-level, periodic)

```
Agent → Bus: POST /v1/heartbeat { "id": "researcher-primary" }
Every 30 seconds (configurable per agent).
```

If heartbeat misses:
- 1 miss → bus sets status to "unhealthy" (still routes, but logs warning)
- 3 misses → bus sets status to "dead" (stops routing, attempts restart)

### Layer 3: HTTP health probe (service-level, on-demand)

```
Bus → Agent: GET http://localhost:9247/v1/health
```

Used when PID is alive but heartbeat is late — the agent might be
overloaded rather than dead. The bus can check responsiveness directly.

### Detection → Response matrix

| Failure mode | PID | Heartbeat | HTTP | Bus response |
|---|---|---|---|---|
| Agent crashed | dead ✓ | stops ✓ | refused ✓ | Restart immediately |
| Agent hung | alive | stops ✓ | timeout ✓ | Restart after heartbeat timeout |
| Agent overloaded | alive | late ✓ | slow ✓ | Log warning, continue routing, consider scaling |
| Agent healthy | alive | on time | 200 ok | Normal operation |
| Port conflict | alive | on time | wrong response | PID mismatch → restart with new port |

---

## Agent-Side Protocol

What the agent process needs to implement. `BaseAgent` in bus-lib
handles all of this — agent authors don't implement the protocol
directly, they subclass `BaseAgent` and write `@handler` functions.

### Endpoints the agent serves

```
POST /v1/handle
{
  "operation": "find_papers",
  "args": { "query": "consensus algorithms" },
  "correlation_id": "req-abc-123",
  "trace_id": "t-42",
  "session_id": null          // non-null if this is a session-bound call
}

→ 200 OK
{
  "correlation_id": "req-abc-123",
  "result": { ... }
}

→ 500 Error
{
  "correlation_id": "req-abc-123",
  "error": "Ollama connection refused"
}

GET /v1/health
→ 200 OK { "status": "ok", "agent_id": "researcher-primary" }
```

That's it. Two endpoints. `BaseAgent` implements both. The agent
author never sees HTTP — they write `@handler` functions and
`BaseAgent` dispatches.

### Calls the agent makes to the bus

```
POST /v1/register      — on startup (after binding port)
POST /v1/heartbeat     — every 30s
POST /v1/deregister    — on clean shutdown
POST /v1/publish       — optional: emit events (e.g., "paper_distilled")
```

All handled by `BaseAgent` internals. The heartbeat loop runs in a
background asyncio task. Publish is exposed as `self.publish(topic, payload)`
for agent authors who want to emit events.

---

## Bus-MCP Adapter

The bridge between Claude (MCP over stdio) and the bus (HTTP). Lives
in `khonliang-bus-lib` as `khonliang_bus.mcp`. It's a bus client, not
part of the bus service.

### What it does on startup

```
1. Connect to bus: GET /v1/services
2. Fetch all registered skills and flows
3. Generate @mcp.tool() stubs for each skill:
     - Tool name: "{agent_id}.{skill_name}"
     - Description: from skill registration
     - Parameters: NOT loaded yet (lazy — fetched on first invocation)
4. Generate @mcp.tool() stubs for each flow:
     - Tool name: "{flow_name}"
     - Description: from flow registration
     - Parameters: NOT loaded yet (lazy — fetched on first invocation)
5. Register bus management tools:
     - bus.services() — list agents
     - bus.status() — platform status
     - bus.trace(trace_id) — query execution traces
6. Start FastMCP server on stdio
```

**Progressive disclosure:** steps 3-4 generate tool stubs with name +
description only. Full parameter schemas are lazy-loaded on first
invocation of each tool. This keeps Claude's initial context compact
when the platform has 50+ skills registered.

### What it does on tool call

```
Claude calls: tools/call { name: "researcher-primary.find_papers", ... }

Adapter:
  1. Parse agent_id + skill from tool name
  2. POST /v1/request { agent_id, operation, args, trace_id }
  3. Wait for response
  4. Return result to Claude as MCP tool response
```

### Dynamic tool refresh

When agents register or deregister, the bus publishes an event on
`bus.registry_changed`. The adapter subscribes to this topic and
regenerates its tool list. Claude sees updated capabilities without
reconnecting.

**Progressive disclosure:** on initial connect, the adapter generates
tools with descriptions only (name + 1-line description). Full parameter
schemas are fetched on first call to each tool. This keeps the initial
MCP tool list compact for Claude's context window.

### Invocation

```json
// .mcp.json
{
  "mcpServers": {
    "khonliang": {
      "command": "python",
      "args": ["-m", "khonliang_bus.mcp", "--bus", "http://localhost:8787"]
    }
  }
}
```

One line. Claude connects to the bus through the adapter. The adapter
discovers everything else dynamically.

---

## Bus Recovery

When the bus recovers from a crash or restart:

```
1. Bus starts, opens bus.db
2. Reads installed_agents table
3. For each installed agent:
     - Starts the process (command + args + --id + --bus + --config)
     - Waits for registration callback (with timeout)
     - If timeout → marks as failed, logs error, continues with others
4. Reads sessions table
     - Active sessions are marked "suspended" (agents need to re-register
       before sessions can resume)
5. Reads messages table
     - Unacknowledged messages from before the crash are re-queued
       for delivery when their subscriber agents come back
6. Bus is ready
     - Agents re-register with new PIDs and ports
     - Suspended sessions resume when their assigned agent is back
     - Unacknowledged messages are redelivered
```

**The recovery runbook** is exposed to Claude via the bus-mcp adapter's
initial handshake:

```json
// bus-mcp sends this as part of its startup context
{
  "recovery": {
    "bus_pid_file": "/path/to/bus.pid",
    "bus_logs": "/path/to/logs/bus.log",
    "bus_start_command": "python -m khonliang_bus.server --config /path/to/bus-config.yaml",
    "bus_health_check": "curl http://localhost:8787/v1/health"
  }
}
```

If the bus is down, Claude tells the operator how to fix it —
not how to work around it.

---

## Message Format Summary

### Bus → Agent (request)

```json
{
  "operation": "find_papers",
  "args": { "query": "multi-agent consensus" },
  "correlation_id": "req-abc-123",
  "trace_id": "t-42",
  "session_id": null
}
```

### Agent → Bus (response)

```json
{
  "correlation_id": "req-abc-123",
  "result": { "papers": [...] }
}
```

### Agent → Bus (error)

```json
{
  "correlation_id": "req-abc-123",
  "error": "Ollama connection refused",
  "retryable": true
}
```

The `retryable` flag tells the bus whether NACK + retry makes sense.
A timeout is retryable. A "skill not found" is not.

### Agent → Bus (registration)

```json
{
  "id": "researcher-primary",
  "callback": "http://localhost:9247",
  "pid": 48291,
  "version": "0.6.4",
  "skills": [
    {
      "name": "find_papers",
      "description": "Search arxiv + semantic scholar",
      "parameters": {
        "query": { "type": "string", "required": true },
        "engines": { "type": "string", "default": "arxiv,semantic_scholar" }
      }
    }
  ],
  "collaborations": [
    {
      "name": "research_and_plan",
      "description": "Papers → distill → FRs → developer",
      "requires": ["developer"],
      "steps": [
        { "call": "researcher.find_papers", "args": { "query": "{{query}}" }, "output": "papers" },
        { "call": "researcher.start_distillation", "output": "distilled" },
        { "call": "researcher.synergize", "output": "frs" },
        { "call": "developer.ingest_frs", "args": { "frs": "{{frs}}" }, "output": "placed" }
      ]
    }
  ]
}
```

**Flow step resolution:** Steps reference agent **types** (e.g.,
`researcher.find_papers`), not agent IDs. The bus resolves type → healthy
registered instance at each flow step. When multiple instances of the
same type exist (e.g., `researcher-primary` and `researcher-domain`),
the bus picks one based on health status and routing rules. For cases
where a specific instance matters, steps support an optional `agent_id`
override: `{ "call": "researcher.find_papers", "agent_id": "researcher-domain", ... }`.

### Agent → Bus (heartbeat)

```json
{ "id": "researcher-primary" }
```

### Bus event (pub/sub)

```json
{
  "event_id": "evt-789",
  "topic": "researcher.paper_distilled",
  "source": "researcher-primary",
  "timestamp_ms": 1712700000000,
  "sequence": 42,
  "payload": {
    "entry_id": "abc123",
    "title": "Multi-Agent Consensus via LLMs",
    "relevance_scores": { "khonliang": 0.82, "private-app": 0.41 }
  }
}
```

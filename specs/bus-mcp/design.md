# Bus MCP: Master Orchestration Layer

## Problem

Claude sessions run 2-3 MCP servers simultaneously. When a task spans services (e.g., "evaluate this spec against research"), Claude becomes the bus — shuttling data between MCPs, holding intermediate state, burning tokens on coordination instead of decisions. Every cross-service round-trip costs context tokens. The services could coordinate directly, but today they can't.

## Vision

The bus becomes the single MCP that Claude talks to. Services register with the bus, declare what they can do alone and together, and the bus orchestrates multi-service operations behind the scenes. Claude makes one high-level call; the coordination happens at local-message cost.

```
┌─────────┐
│  Claude  │  ← only talks to one MCP
└────┬─────┘
     │ stdio
     ▼
┌──────────────────────────────────────────┐
│              bus MCP                      │
│                                          │
│  tool registry ─── orchestration engine  │
│       │                    │             │
│  ┌────┴────┐          ┌───┴───┐         │
│  │ service │          │ flow  │         │
│  │ catalog │          │ runner│         │
│  └─────────┘          └───────┘         │
└─────┬──────────────┬──────────────┬─────┘
      │              │              │
      ▼              ▼              ▼
 ┌──────────┐  ┌──────────┐  ┌──────────┐
 │researcher│  │developer │  │ future   │
 │ service  │  │ service  │  │ service  │
 └──────────┘  └──────────┘  └──────────┘
```

Claude is a client. Services are backends. The bus is the API layer.

## Service registration

Each service registers with the bus on startup. Registration includes three things:

### 1. Identity + health

What it is, how to reach it, whether it's alive.

```yaml
id: researcher
name: "Research Pipeline"
status: healthy
heartbeat_interval: 30s
```

This already exists in khonliang-bus's service registry.

### 2. Solo capabilities

What this service can do on its own. Each capability is an operation the bus can expose as an MCP tool.

```yaml
# researcher
capabilities:
  - name: find_papers
    description: "Search for research papers across arxiv and semantic scholar"
    parameters:
      query: { type: string, required: true }
      engines: { type: string, default: "arxiv,semantic_scholar" }
    returns: "list of paper summaries"

  - name: distill_paper
    description: "Run LLM distillation on a stored paper"
    parameters:
      entry_id: { type: string, required: true }

  - name: feature_requests
    description: "List feature requests for a project"
    parameters:
      target: { type: string, required: true }
      detail: { type: string, default: "brief" }

  # ... 40+ more for researcher
```

```yaml
# developer
capabilities:
  - name: read_spec
    description: "Parse a spec file's metadata, sections, and FR references"
    parameters:
      path: { type: string, required: true }
      detail: { type: string, default: "brief" }

  - name: evaluate_spec
    description: "Evaluate a spec against the research corpus"
    parameters:
      path: { type: string, required: true }

  - name: list_specs
    description: "Discover spec files for a project"
    parameters:
      project: { type: string, required: true }

  # ...
```

### 3. Collaborative capabilities (the interesting part)

What this service can do **in coordination with other services**. These are multi-step operations that span service boundaries. The service declares the flow; the bus executes it.

```yaml
# developer declares what it can do WITH researcher
collaborations:
  - name: evaluate_spec_against_corpus
    description: >
      Evaluate a spec's decisions against the research corpus.
      Fetches relevant papers from researcher, runs best-of-N
      evaluation locally, tags the spec with reasoning.
    requires: [researcher]
    parameters:
      path: { type: string, required: true }
    flow:
      - call: developer.read_spec
        args: { path: "{{path}}" }
        output: spec

      - call: researcher.find_relevant
        args: { query: "{{spec.title}}", project: "{{spec.fr_target}}" }
        output: papers

      - call: researcher.paper_context
        args: { query: "{{spec.title}}" }
        output: evidence

      - call: developer.evaluate_with_evidence
        args: { path: "{{path}}", evidence: "{{evidence}}", papers: "{{papers}}" }
        output: evaluation

    returns: evaluation

  - name: create_fr_from_research
    description: >
      Researcher finds a synergy, developer turns it into a
      tracked FR with spec backing and milestone placement.
    requires: [researcher]
    flow:
      - call: researcher.synergize
        output: concepts

      - call: developer.draft_fr
        args: { concepts: "{{concepts}}" }
        output: draft

      - call: researcher.score_relevance
        args: { entry_id: "{{draft.backing_paper}}" }
        output: scores

      - call: developer.finalize_fr
        args: { draft: "{{draft}}", scores: "{{scores}}" }
        output: fr

    returns: fr
```

```yaml
# researcher declares what it can do WITH developer
collaborations:
  - name: research_and_plan
    description: >
      Full pipeline: find papers on a topic, distill them,
      generate FRs, hand them to developer for spec placement.
    requires: [developer]
    flow:
      - call: researcher.find_papers
        args: { query: "{{query}}" }
        output: papers

      - call: researcher.start_distillation
        output: distilled

      - call: researcher.synergize
        output: frs

      - call: developer.ingest_frs
        args: { frs: "{{frs}}" }
        output: placed

    returns: placed
```

## How the bus exposes tools to Claude

The bus MCP builds its tool list dynamically from registered services:

### Solo capabilities → direct tools

```
researcher.find_papers(query, engines)
researcher.feature_requests(target, detail)
developer.read_spec(path, detail)
developer.list_specs(project)
...
```

Namespaced by service. Claude sees them in `catalog()`. Each call routes to the owning service via bus request/reply.

### Collaborative capabilities → orchestrated tools

```
developer.evaluate_spec_against_corpus(path)
developer.create_fr_from_research()
researcher.research_and_plan(query)
```

These also appear as tools in the catalog. When Claude calls one, the bus runs the declared flow — stepping through each call, passing outputs as inputs to the next step, handling errors. Claude gets back the final result.

### Auto-discovered vs declared

Some combinations might emerge that no service explicitly declared. The bus could also expose a generic orchestration tool:

```
bus.run_flow(steps=[
  { service: "researcher", operation: "find_papers", args: {...} },
  { service: "developer", operation: "evaluate_spec", args: {...} },
])
```

But the declared collaborations are better — they're vetted, tested, and the descriptions tell Claude when to use them. The generic tool is the escape hatch.

## What Claude sees

One MCP. One `catalog()`. Tools organized by service and by collaboration:

```
=== SERVICES ===
  researcher (healthy, 42 tools)
  developer (healthy, 8 tools)

=== SOLO TOOLS ===
  researcher.find_papers — search for papers
  researcher.distill_paper — run LLM distillation
  researcher.feature_requests — list FRs
  developer.read_spec — parse a spec file
  developer.list_specs — discover specs
  developer.health_check — verify DB and workspace
  ...

=== COLLABORATIVE TOOLS ===
  developer.evaluate_spec_against_corpus — evaluate spec against research
  developer.create_fr_from_research — synergy → tracked FR
  researcher.research_and_plan — papers → distill → FRs → developer
  ...

=== BUS TOOLS ===
  bus.services — list registered services
  bus.run_flow — generic multi-step orchestration
```

Claude picks the right tool. If it's solo, the bus routes. If it's collaborative, the bus orchestrates. Claude never coordinates between services itself.

## What services see

Each service is a bus client. It:

1. **Registers** on startup (identity + capabilities + collaborations)
2. **Handles requests** — the bus sends it `{ operation: "find_papers", args: {...}, correlation_id: "..." }` and it responds with `{ correlation_id: "...", result: {...} }`
3. **Does NOT speak MCP** — it speaks bus protocol only. The MCP translation lives in the bus.

A service's code looks like:

```python
from khonliang_bus import BusService

service = BusService(
    bus_url="http://localhost:8787",
    service_id="researcher",
    capabilities=[...],       # solo tool schemas
    collaborations=[...],     # multi-service flow declarations
)

@service.handler("find_papers")
async def handle_find_papers(args):
    results = await pipeline.search(args["query"], args.get("engines", "arxiv,semantic_scholar"))
    return results

@service.handler("distill_paper")
async def handle_distill_paper(args):
    ...

service.run()  # registers, starts handling requests
```

The existing MCP tool implementations (40+ in researcher, 8+ in developer) become handler functions. The `@mcp.tool()` decorator becomes `@service.handler()`. The function body stays the same.

## What the bus does

The khonliang-bus Go server gains three new responsibilities:

### 1. Tool registry (new)

Services register capabilities and collaborations. The bus stores them and uses them to build the MCP tool surface.

```
POST /v1/register
{
  "id": "researcher",
  "capabilities": [...],
  "collaborations": [...]
}
```

### 2. Request/reply (new)

Synchronous request routing. Claude calls a tool → bus sends a request to the owning service → service responds → bus returns the result to Claude.

```
POST /v1/request
{
  "service": "researcher",
  "operation": "find_papers",
  "args": { "query": "multi-agent consensus" },
  "timeout": 30
}
→ { "result": [...] }
```

### 3. Flow orchestration (new)

For collaborative tools, the bus executes the declared flow:

```
POST /v1/flow
{
  "flow_id": "developer.evaluate_spec_against_corpus",
  "args": { "path": "/path/to/spec.md" }
}
```

The bus steps through the flow definition, calling each service in turn, threading outputs through `{{template}}` variables, and returning the final result. If a step fails, the flow short-circuits with the error.

### 4. MCP transport (new)

The bus speaks MCP over stdio so Claude can connect to it directly. It translates MCP tool calls into bus requests/flows and MCP tool results back into responses.

This is a thin adapter layer — the bus already knows the tool schemas (from registrations) and can generate `tools/list` and handle `tools/call` by routing to the right service or flow.

### Existing (unchanged)

- Pub/sub messaging (for async events like `researcher.paper_distilled`)
- Service registry with heartbeat
- Durable subscriptions
- Storage backends (memory, sqlite, redis)

## Migration path

### Phase 1: Request/reply + tool registry in the bus

Add `/v1/request` and `/v1/register` (with capabilities) to the Go server. Services can register and handle sync requests. No MCP transport yet — just the bus-side plumbing.

**Bus work:** Go server changes.
**Service work:** none yet. Existing services keep running as MCPs.

### Phase 2: BusService Python adapter

Build `khonliang_bus.BusService` — the Python class that wraps an existing MCP server's tools as bus request handlers. One line per tool to migrate:

```python
service.handler("find_papers")(existing_find_papers_fn)
```

Or a bulk adapter that introspects the existing FastMCP instance and registers all its tools automatically:

```python
service = BusService.from_mcp(existing_mcp_server, bus_url="...")
service.run()
```

**Bus work:** Python client library extension.
**Service work:** minimal — one adapter call per service.

### Phase 3: MCP transport on the bus

Add stdio MCP transport to the Go server. The bus becomes the master MCP. Claude's `.mcp.json` points at the bus instead of individual services.

**Bus work:** Go MCP transport layer.
**Service work:** none — services are already registered from phase 2.
**Claude work:** change `.mcp.json` to point at the bus.

### Phase 4: Collaborative flows

Services start declaring `collaborations` in their registrations. The bus gains the flow orchestration engine. Claude sees collaborative tools alongside solo tools.

**Bus work:** flow engine in Go.
**Service work:** declare flows in registration YAML/JSON.

### Phase 5: Retire individual MCP servers

Once all services are bus-native and the bus MCP is the primary interface, the individual MCP server entry points (`python -m researcher.server`, `python -m developer.server`) become optional. They can stay as a fallback for debugging or local development, but Claude's production path goes through the bus.

## Open questions

1. **Flow language** — the `{{template}}` variables in flow definitions need a small expression language for accessing nested fields, filtering, etc. Keep it minimal (jsonpath? jmespath? just dot-access?) to avoid a DSL maintenance burden.

2. **Error handling in flows** — short-circuit on error, or continue-on-error with partial results? Probably configurable per step.

3. **Streaming** — some operations (distillation, synthesis) take a long time. Should the bus support streaming responses back to Claude, or just long timeouts?

4. **Auth** — if the bus is the single entry point, it's also the auth boundary. Service-to-service calls through the bus could be trusted (internal), while Claude → bus calls could require a token. Not needed for solo/local use but matters if the bus ever serves multiple users.

5. **Tool naming** — `researcher.find_papers` or just `find_papers`? Namespacing is clearer when services have overlapping concepts (both could have `health_check`). But it's verbose. Maybe namespace in the tool name, short alias in the description.

6. **Capability discovery for collaborations** — does each service know about its siblings' capabilities at registration time, or does the bus broker that? If researcher wants to declare a flow that calls `developer.ingest_frs`, does it need to know developer exists? Or does the bus validate flows against its registry and reject missing dependencies?

7. **Local-only operations** — `read_spec` only touches developer's local DB. Going through the bus adds a network hop. Accept the latency (it's localhost, ~1ms), or let services mark certain operations as "local-only" that the bus shortcuts?

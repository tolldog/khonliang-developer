# khonliang-developer

Development lifecycle MCP server. Inverse of researcher: where researcher ingests external knowledge into the corpus, developer consumes the corpus to produce internal artifacts (specs, milestones, FRs, code, worktrees). Lets Claude focus purely on code and code review by handling all upstream evaluation, planning, and dispatch.

## Status

Not built yet. Tracked under FR `fr_developer_28a11ce2` (high priority). Pick up via `feature_requests(target='developer')` from the researcher MCP.

## Stack

- Python, async throughout
- Local LLMs via Ollama (best-of-N evaluation, same models as researcher)
- SQLite-backed stores: KnowledgeStore, TripleStore, DigestStore (from khonliang) — shared with researcher
- MCP server extending khonliang's KhonliangMCPServer
- Cross-app communication via khonliang-bus client (HTTP + WebSocket)

## Ecosystem position

```
INFRASTRUCTURE (services)
├─ khonliang-scheduler  — LLM inference scheduling (Go)
└─ khonliang-bus        — event bus + service registry (Go, HTTP+WS)

LIBRARIES (Python)
├─ khonliang            — agents, MCP transport, stores, consensus
└─ researcher-lib       — relevance, graph, synthesis, best-of-N, idea parsing

APPS (Python MCP servers)
├─ researcher  — ingest world: papers, OSS, RSS → corpus → FR ideas
└─ developer   — consume corpus → specs, FRs, worktrees, code dispatch  ← THIS REPO
```

## Architecture boundary

- **khonliang** = library. Agent primitives. Don't reimplement.
- **khonliang-bus** = service. Use the Python client for cross-app pub/sub. Don't reinvent messaging.
- **researcher-lib** = library. Use its evaluation primitives (best-of-N, idea parsing, relevance, graph). Don't duplicate.
- **researcher** = sibling app. Ingestion authority. Call its MCP tools (or talk via bus) for on-demand corpus operations. Don't ingest papers yourself.
- **developer** = this repo. Owns dev cycle: spec/milestone management, FR lifecycle, worktree orchestration, work dispatch to Claude.

When in doubt: if it's about producing artifacts (specs, code, FRs, worktrees), it's developer. If it's about ingesting the world, it's researcher.

## Core capabilities to build

### Spec/milestone management
- Ingest spec/milestone files (markdown with FR## references)
- Backward traversal: milestone → specs → FRs → backing evidence
- Section extraction, frontmatter parsing

### Evaluation pipeline
- Cross-reference spec decisions against research corpus
- Best-of-N self-distillation (3 parallel 7B runs)
- Tag spec docs with reasoning so it's MCP-accessible to Claude later

### FR lifecycle
- Read FRs from researcher (via shared store or MCP-to-MCP)
- Manage status progression (planned → in_progress → review → completed)
- Detect overlaps, bundle related FRs into work units

### Worktree orchestration
- Spin up git worktrees per FR or work bundle
- Pre-load context: relevant files, spec, evidence, tagged reasoning
- Track parallel work, detect file conflicts, clean up on completion

### Work dispatch
- Bundle related FRs into work units
- Generate ready-to-execute briefing prompts for Claude
- Claude receives a complete package, no discovery or research needed

### Cross-app integration
- Subscribe to `researcher.paper_distilled` to refresh active FR evaluations
- Publish `developer.fr_status_changed`, `developer.spec_evaluated`, `developer.worktree_ready`
- Request on-demand ingestion from researcher when evaluating new specs
- Register capabilities with khonliang-bus service registry

## Researcher-lib precondition FRs

Before developer can be cleanly built, three primitives need to land in researcher-lib:

1. `BaseIdeaParser` — promote `IdeaParserRole` from researcher app to lib (developer reuses for spec/PR text decomposition)
2. `select_best_of_n()` — extract self-distillation pattern from `synthesizer._generate()` (both apps need it)
3. `LocalDocReader` — lightweight non-persistent file read primitive (developer needs spec/milestone reads without going through ingestion pipeline)

These should be tracked as researcher FRs and completed before serious developer work begins.

## MCP tool response convention

Same as researcher: token-efficient, no preamble, data-only, default to brief.

External agents pay per token. Every word must earn its place.

## Claude's role

Pure code + code review. Developer hands Claude pre-evaluated work packages with full evidence chains. Claude doesn't research, doesn't plan, doesn't evaluate. Reads context, writes code, handles PR workflow.

## Running

```
.venv/bin/python -m developer.server --config /mnt/dev/ttoll/dev/khonliang-developer/config.yaml
```

Config path must be absolute for cross-session MCP launches.

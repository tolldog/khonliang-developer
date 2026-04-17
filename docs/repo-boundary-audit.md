# Repo Boundary Audit

Date: 2026-04-17
FR: `fr_developer_97203f4c`

## Purpose

Make repo ownership explicit across the local khonliang workspace so reusable
code lives in libraries, process wiring lives in apps/agents, and large context
is exchanged through the bus and artifacts rather than cross-importing sibling
apps.

## Repo Inventory

| Repo | Type | Boundary |
|---|---|---|
| `ollama-khonliang` | library | Generic LLM, roles, stores, MCP, knowledge, research, consensus primitives. |
| `khonliang-bus` | service | Python bus server, MCP adapter, registry, sessions, flows, artifacts, webhooks. |
| `khonliang-bus-lib` | library | Agent SDK: `BaseAgent`, `Skill`, `Collaboration`, `BusClient`, test harness. |
| `khonliang-researcher-lib` | library | Reusable research primitives: graph, relevance, synthesis, best-of-N, doc reader, vector index, base research agent. |
| `khonliang-researcher` | app/agent | Paper ingestion, fetch/search engines, distillation pipeline, prompts, MCP/CLI surfaces. |
| `khonliang-developer` | app/agent | FR lifecycle authority, specs, milestones, work units, git/GitHub/test support, development handoff. |
| `khonliang-scheduler` | service | LLM inference scheduling. |
| `genealogy` | domain app | Genealogy-specific GEDCOM/tree/research workflows on top of khonliang. |
| `autostock` | domain app | Trading-specific application and agents. |

## Boundary Rules

- Library repos expose importable primitives and stable public APIs.
- App/agent repos own process wiring, CLI/MCP tools, prompts, local config,
  domain workflows, and persistence policy.
- Services own long-running infrastructure and protocols.
- Sibling app-to-app imports are a smell; use a library or the bus.
- Heavy or machine-local config belongs in ignored local files, with example
  files tracked.

## Findings

1. `khonliang-bus` still had retired Go implementation and Go-era review docs.
   PR `tolldog/khonliang-bus#17` removes those artifacts and refreshes the
   README around the current Python bus/MCP/artifact workflow.

2. `khonliang-developer` top-level guidance still described developer as not
   built and pointed FR ownership at researcher. PR `tolldog/khonliang-developer#23`
   updates that guidance after the FR ownership merge.

3. `khonliang-researcher` has app-local modules that appear to duplicate
   `khonliang-researcher-lib` primitives:
   - `researcher/graph.py`
   - `researcher/relevance.py`

   Current production references use `khonliang_researcher` for graph and
   relevance helpers. This should be removed in a focused researcher cleanup
   after the open graph suggestion PRs settle.

4. `khonliang-researcher` still owns app-specific workflow modules that should
   stay in the app:
   - `fetcher.py`, `parser.py`, `rss.py`, `search_engines.py`
   - `pipeline.py`, `queue.py`, `roles.py`, `worker.py`
   - `cli.py`, `server.py`, `agent.py`, `generic_agent.py`

   Those modules include process wiring, prompts, configured external sources,
   and ingestion/distillation behavior.

5. Domain apps have config hygiene to review separately:
   - `autostock` tracks `config.yaml` and currently has local modified files.
   - `genealogy` tracks `config.yaml`; its `.gitignore` ignores `CLAUDE.md`
     even though the file is tracked.

   Do not edit these opportunistically while user changes are present. Convert
   machine-local config to examples only in a dedicated branch per repo.

6. Cross-repo production imports are mostly clean in the khonliang core set:
   - `developer` imports `khonliang_researcher` library helpers, not the
     researcher app.
   - `researcher` imports `khonliang_researcher` library helpers and its own app
     modules.
   - `bus` does not import app repos.
   - `bus-lib` does not import bus server internals.

## Follow-Up FRs

- `fr_researcher_f7e3eda6`: remove stale app-local researcher primitives now
  provided by researcher-lib.
- `fr_developer_9c7b1cf2`: track repo config and `CLAUDE.md` hygiene across
  domain apps.

## Recommended Next Cleanup Order

1. Merge bus PR `#17` and developer PR `#23` after Copilot approval.
2. Land researcher-lib PR `#8`, then researcher draft PR `#15`.
3. Clean researcher duplicate primitive modules and update researcher guidance.
4. Audit `khonliang-bus-lib` README/CLAUDE for current service/library boundary.
5. Handle domain app config hygiene in isolated branches, avoiding active user
   changes.

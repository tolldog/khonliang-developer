# Developer Pipeline Guide

khonliang-developer is the **inverse of researcher**. Where researcher
ingests external knowledge into the corpus, developer consumes the
corpus to produce internal artifacts (specs, milestones, FRs, code,
worktrees) and hands Claude pre-evaluated work packages.

External Claudes call developer's MCP tools to read spec/milestone
state. Code lives in workspace files; nothing is ingested here.

## Quick start
1. `list_specs(project)` — discover spec files for a project
2. `read_spec(path)` — parse a spec's metadata, sections, and FR refs
3. `traverse_milestone(path)` — backward-walk a milestone to its specs and FRs
4. `health_check()` — verify DB, workspace, and the researcher seam

## Reading specs
- `read_spec(path, detail="brief")` — frontmatter, section index, FR references
- `read_spec(path, detail="full")` — same plus the raw markdown body
- `read_spec(path, detail="compact")` — pipe-delimited summary for agent loops

Specs use a bold-line metadata convention at the top of the file:

```
# MS-01: Title

**FR:** `fr_developer_28a11ce2`
**Priority:** high
**Status:** approved
```

`read_spec` parses this into a `SpecSummary` with typed fields (`fr`,
`priority`, `class_`, `status`) plus an `extras` dict for any other
bold lines. The summary is returned alongside the section index.

## Traversing milestones
`traverse_milestone(path)` walks backward from a milestone document:

1. Reads the milestone file
2. Follows markdown links to `*.md` files (filtered to existing files)
3. Reads each linked spec
4. Collects every FR reference matching `fr_<target>_<8 hex chars>`
5. Resolves each FR via `ResearcherClient.get_fr` (stubbed in MS-01)

In MS-01 every FR comes back **(unresolved)** because the
`ResearcherClient` seam is stubbed. MS-02 wires real lookups against
researcher's MCP server. MS-03 adds `update_fr_status` for lifecycle
writes.

## Listing specs
`list_specs(project)` discovers `spec.md` files under
`projects[project].specs_dir` (configured in `config.yaml`). It returns
a list of `SpecSummary` objects with FR IDs, statuses, and titles
extracted from the bold-line metadata.

## Configuration
The server is launched with `--config /abs/path/config.yaml`. The
config file contains:

- `db_path` — developer's own SQLite file (NOT shared with researcher)
- `workspace_root` — parent directory of project repos
- `projects` — per-project repo and specs_dir paths
- `models` — placeholder, used in MS-02 for spec evaluation
- `bus` — placeholder, used in MS-06 for cross-app events
- `researcher_mcp` — connection details for the ResearcherClient seam

All relative path fields are resolved against the config file's
directory at load time. The server refuses to start if any store
points at a file other than the configured `developer.db`
(architectural isolation per spec rev 2).

## What MS-01 does NOT do
- No LLM calls (Ollama integration lands in MS-02)
- No FR status writes (lands in MS-03)
- No worktree management (lands in MS-04)
- No work bundling or briefing prompt generation (lands in MS-05)
- No khonliang-bus subscriptions or publishes (lands in MS-06)
- No file handles shared with researcher's database

## Roadmap preview
- **MS-02** Evaluation pipeline: best-of-N spec evaluation against the
  research corpus, tagged reasoning written back to spec docs.
- **MS-03** FR lifecycle: status progression, overlap detection, work
  bundling. First real `ResearcherClient.update_fr_status` calls.
- **MS-04** Worktree orchestration: git worktree per FR, conflict
  detection, cleanup.
- **MS-05** Work dispatch: bundle FRs into work units, generate
  briefing prompts so external Claudes can execute without rediscovery.
- **MS-06** Cross-app integration: subscribe `researcher.paper_distilled`,
  publish `developer.fr_status_changed` and friends, register with the
  khonliang-bus service registry.

## detail parameter
Most tools accept `detail="compact|brief|full"` (per `response_modes`):
- **compact** — pipe-delimited key=value, for agent loops
- **brief** — structured one-line-per-item, default
- **full** — rich detail with body content

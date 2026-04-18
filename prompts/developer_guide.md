# Developer Pipeline Guide

khonliang-developer is the development lifecycle authority for the khonliang
workspace. It turns FRs into work units, work units into milestones/specs, and
implementation progress into durable state.

Researcher is the evidence and ingestion authority. Developer asks researcher
for paper/context evidence through the bus, but active FR state lives in
developer.

## Quick Start

- `next_work_unit(target="", max_frs=5)` — return the highest-ranked FR bundle.
- `prepare_development_handoff(...)` — produce a compact bundle, milestone,
  draft spec, and scope review for implementation.
- `run_tests(project, target="", detail="brief")` — run pytest and return a
  distilled digest.
- `pr_ready(repo, pr_number)` — summarize GitHub review and merge readiness.
- `update_fr_status(fr_id, status, branch="", notes="")` — record lifecycle
  progress.
- `developer_guide()` — return this guide.

Use `detail="compact"` for tight agent loops, `detail="brief"` for normal
work, and `detail="full"` only when the caller explicitly needs body content.

### Runtime Availability

This guide is shared by two runtimes:

- Bus-native agent runtime: supports the broader developer workflow skills
  described in this guide, including `next_work_unit`,
  `prepare_development_handoff`, `run_tests`, `pr_ready`, `update_fr_status`,
  and the FR/milestone lifecycle operations below.
- Direct MCP compatibility server: supports only the compatibility tool subset:
  `read_spec`, `traverse_milestone`, `list_specs`, `health_check`, and
  `developer_guide`.

If you are connected through the direct MCP compatibility server, do not call
bus-native workflow skills unless that server explicitly registers them.

## FR Lifecycle

Developer owns FR storage and dependency state.

- `promote_fr` creates FRs in developer's store.
- `get_fr_local` and `list_frs_local` read developer-owned FRs.
- `update_fr`, `update_fr_status`, `set_fr_dependency`, and `merge_frs` mutate
  developer state.
- `migrate_frs_from_researcher` performs one-way migration of old researcher
  FRs while preserving IDs and redirects.

Old researcher FR IDs may still appear in papers, specs, or prior notes. Do
not discard those IDs. Migrate them into developer before executing against
them so references remain stable.

## Work Units And Milestones

Developer clusters ready FRs into implementation bundles:

- `work_units` lists ranked bundles.
- `next_work_unit` picks the highest-priority ready bundle.
- `propose_milestone_from_work_unit` persists a milestone for a bundle.
- `draft_spec_from_milestone` returns the deterministic draft spec.
- `review_milestone_scope` flags duplicate scope or review-only FRs.
- `prepare_development_handoff` combines those steps for a running session.

The target shape is:

```text
FR bundle -> milestone unit -> draft spec -> implementation -> tests -> PR -> completed FRs
```

Developer should give the coding session enough context to implement without
rediscovering the problem.

## Spec And Milestone Reading

- `list_specs(project)` discovers spec files under the configured project repo.
- `read_spec(path, detail="brief")` parses metadata, sections, and FR refs.
- `traverse_milestone(path)` walks milestone docs back to specs and FRs.

Specs use lightweight markdown metadata:

```markdown
# MS-01: Title

**FR:** `fr_developer_28a11ce2`
**Priority:** high
**Status:** approved
```

## Researcher Boundary

Developer uses researcher for evidence/context only:

- `get_paper_context(query)` asks researcher through the bus.
- Spec evaluation can compare decisions against corpus context.
- Researcher should not be the active FR lifecycle owner.

If a task requires ingestion, paper fetching, RSS, or idea distillation, route
that to researcher. If a task requires FR status, work planning, Git/GitHub, or
implementation handoff, keep it in developer.

## Git, GitHub, And Tests

Developer exposes native Git skills for status, diff, branches, staging,
commits, and checkout operations. It also exposes PR readiness checks and pytest
digests.

Prefer `run_tests` over raw pytest output in long LLM sessions. Raw logs should
be stored as artifacts or files; the prompt should receive a compact failure
summary and a reference.

PR review policy for this workspace:

- Always tag `@copilot` in a PR comment when requesting review.
- Read Copilot review/comments before merging.
- Address review findings before merge.
- If branch policy blocks despite a clear Copilot pass, use the repository's
  normal admin path only after confirming the review text is clear.

## Configuration

Launch developer with an absolute config path:

```bash
.venv/bin/python -m developer.agent --id developer-primary --bus http://localhost:8787 --config /abs/path/config.yaml
```

`config.yaml` contains:

- `db_path` — developer's own SQLite file.
- `workspace_root` — parent directory for local repos.
- `projects` — repo and spec directory paths.
- `models` — model names reserved for evaluation and review workflows.
- `bus` — bus endpoint used by the native agent and researcher client.
- `researcher_mcp` — compatibility connection block for direct MCP paths.

All relative paths are resolved against the config file directory. The pipeline
refuses to start if stores point at a database other than developer's configured
database.

## Context Economy

Developer should reduce context pressure, not add to it.

- Return compact structured summaries by default.
- Put large command output, test logs, specs, diffs, and handoffs in artifacts
  or durable files.
- Return artifact IDs, file paths, or short digests to the LLM session.
- Keep external LLM sessions disposable: they should start, request the current
  bundle/state, work, publish progress, and exit.

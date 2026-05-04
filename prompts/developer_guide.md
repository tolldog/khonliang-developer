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
- `create_session_checkpoint(fr_id, cwd, ...)` — capture durable work state
  before idle, exit, or handoff.
- `resume_session_checkpoint(checkpoint, cwd, ...)` — rebuild launch context
  and detect stale git/PR state.
- `audit_repo_hygiene(repo_path)` — inspect docs drift, stale paths, config
  hygiene, and test commands.
- `apply_repo_hygiene_plan(repo_path)` — write the generated audit artifact.
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
  `prepare_development_handoff`, `run_tests`, `create_session_checkpoint`,
  `resume_session_checkpoint`, `audit_repo_hygiene`,
  `apply_repo_hygiene_plan`, `pr_ready`, `update_fr_status`, and the
  FR/milestone lifecycle operations below.
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

## PR Review Iteration Loop

Captured 2026-05-03 from a 13+9-pass cross-vendor (Claude → Copilot)
dogfood on khonliang-bus#28 + khonliang-researcher#37. Six gotchas
must be true or each pass wastes round-trips.

1. **Filter comments by `created_at`, not `commit_id`.** GitHub
   re-anchors prior threads to the new HEAD when the target lines
   are unchanged, so `commit_id == HEAD` returns every still-active
   thread (~20 on a deep PR), not the 3-5 new ones from the latest
   review. Use `select(.created_at > "<prior-push-time>")`.
2. **Two API calls per pass, not one.** `pulls/N/comments` returns
   inline-diff threads only. The PR-thread (where Copilot posts a
   "nothing more to say" saturation message) lives at
   `issues/N/comments`. Skipping the second call misses
   saturation events entirely.
3. **`gh api .../comments` does NOT paginate by default** — pass
   `--paginate`. The default is the GitHub API's per-page limit
   (30 items); a deep PR with 30+ comments silently returns only
   the first page otherwise.
4. **Saturation has two shapes.** (a) The literal "nothing more to
   say" issue-thread comment; (b) the same findings restated across
   passes with no genuinely new ones (post-defer-receipt
   restatements). Both signal admin-merge — but (b) requires that
   you've actually filed the deferral receipts, not just promised to.
5. **Verify deferral claims with file-evidence before merge.** If
   you write "FR for X is already filed" in a PR comment, check
   first. Use `file_bug` (NOT `draft_fr_from_request` — that
   hallucinates scope at the time of writing) to capture a
   verifiable `bug_id` at deferral time, then paste the id in the
   PR thread response. Real-world failure: a pass-9 PR comment
   claimed "FRs already filed" — none existed; cross-check at
   pass-10 caught it; four `bug_*` ids filed retroactively before
   admin-merge.
6. **Bus-event filter axis** for `bus_wait_for_event`:
   `response.event.payload.summary.{repo, pr_number, review_state,
   review_author, delivery_id}`. Three topics needed:
   `github.pull_request_review.submitted`,
   `github.pull_request_review_comment.created`,
   `github.issue_comment.created`. Skipping the third misses
   saturation events. `cursor=now` (`fr_bus_3db58f0b`) skips
   backlog replay; an older bus build silently no-ops the field
   so a fresh subscriber still replays the full history.

Bus-API call shape gotchas surfaced by the same dogfood:
- `/v1/request` ``operation`` field is the SKILL NAME itself,
  not `"skill"` / `"call"` / `"invoke"`. The first three return
  `unknown operation: <x>`; only the actual skill name dispatches.
- `list_frs` only filters on `status`, `target`, and
  `include_all`. The richer filter set (including `project`)
  lives on `list_frs_local`. Neither variant takes a free-text
  / keyword filter (`q=...`, `keyword=...`) — those are silently
  dropped and the full result set comes back. Filter locally on
  the returned FR list when the supported fields aren't enough.
- `draft_fr_from_request` hallucinates scope (e.g. produced six
  identical "Touches typing_extensions.py" lines). Use `file_bug`
  for deferral receipts; promote to FR only via a separate path
  once the receipt is honest.

## Session Checkpoints

Use checkpoints to make external LLM sessions disposable.

- `create_session_checkpoint` captures FR/work-unit metadata, branch/head,
  changed files, optional PR readiness, token/cache hygiene findings, agent
  state, and next actions.
- `resume_session_checkpoint` compares that checkpoint with current git/PR
  state and returns a compact launch briefing plus stale-state reasons.

Checkpoint before a long idle break, handoff, or context clear. Resume from the
checkpoint instead of rebuilding from raw conversation history.

## Repo Hygiene

Use repo hygiene skills before broad cleanup or documentation refresh work.

- `audit_repo_hygiene` returns compact sections: `repo_inventory`,
  `deprecated_paths`, `docs_drift`, `cleanup_plan`, `test_plan`, and
  `artifact_hints`.
- `apply_repo_hygiene_plan` writes a durable markdown audit artifact into the
  target repo. It intentionally does not delete files or rewrite README/code by
  itself.

Treat the generated audit as the implementation plan. Apply code deletion,
README edits, or config changes in focused commits with tests and review.

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
- Checkpoint long-running sessions before idle breaks and resume from the
  checkpoint in a fresh external LLM process.
- Return artifact IDs, file paths, or short digests to the LLM session.
- Keep external LLM sessions disposable: they should start, request the current
  bundle/state, work, publish progress, and exit.

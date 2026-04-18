# khonliang-developer

Developer is the lifecycle agent for the khonliang workspace. It owns feature
requests, dependencies, work-unit bundling, milestone/spec handoff, test
digests, Git/GitHub workflow support, and implementation progress tracking.

Researcher owns ingestion and evidence. Developer asks researcher for corpus
context through the bus, but developer owns active FR state.

## Current Mode

The primary runtime is the bus-native developer agent:

```bash
.venv/bin/python -m developer.agent \
  --id developer-primary \
  --bus http://localhost:8787 \
  --config /abs/path/config.yaml
```

The direct MCP server remains available for compatibility with direct Claude
connections:

```bash
.venv/bin/python -m developer.server \
  --config /abs/path/config.yaml
```

Prefer the bus-native agent for day-to-day development. It lets the bus route
requests, expose skills, hold artifacts, and coordinate with researcher without
loading large sibling-agent context into the LLM session.

## Responsibilities

- Maintain the developer-owned FR store and lifecycle.
- Rank FR bundles into work units.
- Produce milestone and draft-spec handoffs.
- Retrieve evidence/context from researcher through the bus.
- Run tests and return compact digests.
- Provide Git and GitHub workflow skills.
- Audit repos for docs drift, stale paths, config hygiene, and test plans.
- Track progress from `open` through `completed`.

Developer should return concise, executable state. Large logs, diffs, specs,
or handoffs should live as artifacts or durable files with compact references.

## Typical Flow

1. Ask developer for the next work unit with `next_work_unit`.
2. Turn the bundle into implementation context with `prepare_development_handoff`.
3. Create or switch to a branch, then implement the scoped change.
4. Use `run_tests` for a distilled pytest result instead of pasting raw logs.
5. Use `create_session_checkpoint` before idle breaks or handoff.
6. Use `resume_session_checkpoint` to relaunch from durable state.
7. Use Git/GitHub skills for status, commit, PR readiness, and review checks.
8. Tag `@copilot` for PR review before merge.
9. Mark the FR or milestone complete after the PR is merged.

This keeps Claude focused on code and review while developer preserves the
long-lived planning state.

## Session Checkpoints

`create_session_checkpoint` captures the current FR, optional work unit,
branch/head, changed files, PR readiness, token/cache hygiene signals, agent
state, and next actions in a compact JSON payload. Store that payload as a bus
artifact or durable note before leaving a large LLM session idle.

`resume_session_checkpoint` compares the checkpoint with current git and PR
state, reports stale reasons, and returns a launch briefing for a fresh session.
Use it when relaunching after a cache TTL cliff, branch update, or review loop.

## Repo Hygiene

`audit_repo_hygiene` inspects a repository and returns compact sections for
repo inventory, stale/deprecated paths, docs drift, cleanup plan, and test plan.
Use it before broad cleanup so the session does not need to keep raw file reads
in context.

`apply_repo_hygiene_plan` is conservative: it writes the generated audit
markdown artifact into the target repo, usually `docs/repo-hygiene-audit.md`.
It does not delete code or rewrite README content automatically; those edits
should happen in scoped follow-up commits with tests and review.

## Configuration

Copy `config.example.yaml` to `config.yaml` and edit local paths. The local
config is ignored because it contains machine-specific absolute paths.

Important boundaries:

- `db_path` must point at developer's own SQLite file.
- `bus.url` is the bus endpoint used for researcher evidence/context calls.
- `researcher_mcp` remains a compatibility block for direct MCP wiring.
- Project entries point developer at sibling repos and their spec directories.

The pipeline refuses to start if its stores point anywhere other than
developer's configured database.

## Repo Boundary

- `khonliang-bus-lib` provides the agent SDK and skill contracts.
- `khonliang-bus` provides the long-running bus service and artifact routing.
- `khonliang-researcher-lib` provides reusable research primitives.
- `khonliang-researcher` ingests papers, feeds, repos, and ideas.
- `khonliang-developer` owns FRs, work units, specs, tests, Git/GitHub, and
  implementation handoff.

Sibling app imports should be treated as a smell. Promote reusable code into a
library or communicate through the bus.

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
- Track progress from `open` through `completed`.

Developer should return concise, executable state. Large logs, diffs, specs,
or handoffs should live as artifacts or durable files with compact references.

## Typical Flow

1. Ask developer for the next work unit with `next_work_unit`.
2. Turn the bundle into implementation context with `prepare_development_handoff`.
3. Create or switch to a branch, then implement the scoped change.
4. Use `run_tests` for a distilled pytest result instead of pasting raw logs.
5. Use Git/GitHub skills for status, commit, PR readiness, and review checks.
6. Tag `@copilot` for PR review before merge.
7. Mark the FR or milestone complete after the PR is merged.

This keeps Claude focused on code and review while developer preserves the
long-lived planning state.

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

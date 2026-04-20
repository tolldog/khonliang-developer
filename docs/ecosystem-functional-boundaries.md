# Khonliang Ecosystem Functional Boundaries

This document defines the khonliang ecosystem by function first and repository
second. Repositories are implementation locations; functional splits are the
stable ownership model used for audits, migrations, agents, and cleanup.

The current reset is internal. The only active clients are khonliang tools,
genealogy, and autostock, all owned and developed in the same workspace. We do
not preserve old client behavior for compatibility. We migrate clients to the
new model, prove the new path works, and remove old code last.

## Functional Splits

| Split | Purpose | Owns | Does Not Own |
| --- | --- | --- | --- |
| Core Intelligence | Generic LLM and orchestration primitives. | model clients, roles, routing, consensus, knowledge stores, blackboards, generic compression, generic model-routing primitives | app workflows, repo-specific policy, bus runtime |
| Communication Fabric | Runtime communication between agents, tools, and external sessions. | service registry, skill routing, request/reply, pub/sub, sessions, artifacts, MCP adapter | domain workflow logic, FR lifecycle, paper ingestion |
| Agent Contract | Shared contract for things that run on the fabric. | `BaseAgent`, skill schemas, service metadata, response envelopes, registration protocol, test harnesses | bus server runtime, app-specific skills |
| Developer Workflow | Turning ideas into implemented, reviewed code. | FR lifecycle, bundles, milestones, specs, git/GitHub workflow, repo hygiene orchestration, review workflow coordination | research ingestion, generic graph/vector algorithms |
| Research & Evidence | Turning sources into structured evidence. | source ingestion, distillation queues, evidence briefs, concept graph population, researcher skills | active FR lifecycle, git/PR operations |
| Research Primitives | Reusable research interfaces and algorithms. | vector indexes, graph/entity primitives, ranking/fusion, taxonomy structures, local document readers, base research contracts | app databases, live ingestion queues, app server startup |
| Model Runtime & Scheduling | Choosing and running models efficiently. | model profiles, scheduling strategies, batching, runtime metrics, scheduler design references | developer/researcher workflows, bus service ownership |
| Knowledge Library | Durable organization of the corpus. | paper taxonomy, neighborhoods, classification, librarian/reindex workflows, curated evidence organization | transient investigations, FR execution |
| Investigation Workspace | Temporary exploratory research threads. | scoped investigations, one-way evidence links to corpus, hypotheses, comparison summaries | permanent corpus taxonomy unless promoted |
| External Interface | How humans and external LLM sessions enter the ecosystem. | MCP exposure, CLI surfaces, compact tool responses, docs/guides | internal workflow ownership |

## Repository Mapping

| Repository | Primary Functional Split | Role |
| --- | --- | --- |
| `ollama-khonliang` | Core Intelligence | Importable core library for LLM clients, roles, routing, stores, consensus, and generic MCP/agent primitives. |
| `khonliang-bus` | Communication Fabric | Running bus service, service registry, artifact store, lifecycle management, and MCP adapter. |
| `khonliang-bus-lib` | Agent Contract | Shared importable bus interface: agent base, bus client, skill contracts, schemas, response envelopes, and test harnesses. |
| `khonliang-developer` | Developer Workflow | App/agent for FRs, bundles, milestones, specs, git/GitHub, repo hygiene, reviews, and implementation handoff. |
| `khonliang-researcher` | Research & Evidence | App/agent for ingestion, distillation, evidence briefs, concept graph population, and researcher workflows. |
| `khonliang-researcher-lib` | Research Primitives | Shared importable research interface and primitives used by researcher, developer, and clients. |
| `khonliang-scheduler` | Model Runtime & Scheduling | Dormant scheduler service and design reference. Not active runtime today, but not abandoned. |
| `genealogy` | Client Application | Owned application client that must use packaged imports and bus skills. |
| `autostock` | Client Application | Owned application client that must use packaged imports and bus skills. |

## `X` And `X-lib` Rule

For a functional domain `X`:

- `X` is the application or runtime owner.
- `X-lib` is the shared importable interface, contracts, and primitives for `X`.

`X-lib` is not a smaller version of the application and is not a dumping ground
for implementation leftovers. It is the package other ecosystem components may
import when they need shared types, base classes, schemas, pure algorithms, or
stable client interfaces.

Examples:

| Domain | Runtime/App | Shared Interface |
| --- | --- | --- |
| Bus | `khonliang-bus` | `khonliang-bus-lib` |
| Research | `khonliang-researcher` | `khonliang-researcher-lib` |
| Developer | `khonliang-developer` | future `khonliang-developer-lib` only if shared developer primitives emerge |
| Scheduler | dormant `khonliang-scheduler` | future `khonliang-scheduler-lib` only if reusable scheduler interfaces are split out |

## Import And Dependency Rules

All cross-repo imports must resolve from installed Python packages or
GitHub-pinned dependencies. Local editable installs are allowed for
development, but code must still import through the installed Python module
path, not through sibling-directory imports or `sys.path` hacks. The
dependency must be declared in project metadata using its distribution name.

Allowed:

```toml
dependencies = [
  "khonliang-bus-lib @ git+https://github.com/tolldog/khonliang-bus-lib.git@<commit>",
  "khonliang-researcher-lib @ git+https://github.com/tolldog/khonliang-researcher-lib.git@<commit>",
]
```

Allowed for local development:

```bash
python -m pip install -e ../khonliang-bus-lib
python -m pip install -e .
```

Not allowed:

- sibling-path imports
- `sys.path` mutation to reach another repo
- app-to-app imports for runtime collaboration
- `X-lib` importing from `X`
- undeclared cross-repo dependencies
- filesystem path dependencies as architecture

Direction rules:

- `X` may import `X-lib`.
- Other repos may import `X-lib`.
- Other repos should not import `X` unless `X` exposes an intentional packaged client API.
- `X-lib` must not import `X`.
- Apps collaborate through bus skills, not direct app imports.

## Active Integration Path

The supported active path is:

```text
external session
  -> khonliang-bus MCP adapter
    -> bus skill registry
      -> app agents
        -> app or library code
          -> compact result or artifact reference
```

Direct app MCP servers are compatibility or debug surfaces during migration.
They are not the primary architecture. New active workflows must be bus-native.

## Skill Registration Rules

All active agents register skills through the bus-native contract. During the
boundary reset, the bus-lib skill contract should grow to make the following
target metadata available for every active skill:

- stable skill id
- owning agent id
- functional split
- input schema
- output schema
- output mode: `inline`, `artifact_ref`, or `stream`
- lifecycle state: `active`, `deprecated`, or `disabled`
- version
- timeout or budget metadata
- compact usage hints where useful

Agent lifecycle must be testable:

- start registers service and skills
- stop unloads or disables skills
- restart replaces stale definitions instead of duplicating them
- crash or heartbeat expiry marks skills unavailable
- MCP adapter refresh reflects start, stop, and restart changes

## Classification Vocabulary

Boundary audits classify code paths with these labels:

| Classification | Meaning |
| --- | --- |
| `correct_owner` | Code is in the correct functional split and repo. |
| `wrong_owner` | Code belongs to another functional split or repo. |
| `shared_primitive` | Code should move to an `X-lib` or lower-level shared library. |
| `app_policy` | Code should stay in an app/runtime repo because it owns workflow state or policy. |
| `migration_bridge` | Temporary compatibility path used during migration. |
| `dormant_reference` | Inactive but valuable design/source reference. Do not delete by default. |
| `dead_code` | Unused code with no replacement value after reference checks. |
| `docs_drift` | Documentation implies an old or unsupported architecture. |

Integration modes:

- `bus_native`
- `compatibility_mcp`
- `cli_only`
- `library_only`
- `dormant_service`

Actions:

- `keep`
- `migrate`
- `extract`
- `document`
- `deprecate`
- `remove_later`

## Version Reset Policy

The boundary reset is a coordinated internal major-version reset. We own all
active clients, so we update clients rather than preserving old behavior.

Use a major version bump when a repo changes:

- public import paths
- package ownership of a public type, function, class, or schema
- bus skill names or schemas
- direct MCP support status
- persisted artifact/schema shape
- CLI command behavior
- package dependency source
- deprecated code availability
- active integration path from direct MCP to bus-native agents

Patch and minor versions remain available for normal non-boundary work, but the
ecosystem boundary reset itself is major-version work.

## Migration Order

Removal is last.

1. Define boundaries and package rules.
2. Create a boundary audit matrix.
3. Define and test bus-native skill registration and lifecycle.
4. Convert active agents to bus-native skills.
5. Update owned clients: khonliang tools, genealogy, and autostock.
6. Bump touched packages to the next major version and update GitHub pins.
7. Run cross-ecosystem validation through the bus.
8. Remove old code only after replacements are proven and clients are updated.

Cross-ecosystem validation passes only when all of these checks are true:

- `khonliang-bus` starts with the expected local config.
- Active agents start through bus lifecycle controls.
- Each active agent registers its expected service metadata and skills.
- Every active skill has input and output schema metadata.
- `bus_skills` or equivalent registry inspection shows no stale duplicate skills.
- A representative skill invocation succeeds for developer and researcher.
- Large outputs return compact responses with artifact references where expected.
- Artifact head, tail, excerpt, and distill operations work on produced artifacts.
- Agent stop/unload removes or disables its skills.
- Agent restart refreshes changed skill definitions without duplicate registrations.
- MCP adapter refresh exposes the current bus skill set.
- Owned clients (`genealogy`, `autostock`, and khonliang tools) smoke-test through packaged imports or bus skills.
- Boundary audit reports no unresolved high-priority migration, import, or registration violations.

Code is removed only when:

- a bus-native or packaged replacement exists,
- tests prove the replacement works,
- owned clients are updated,
- the boundary audit no longer reports unresolved high-priority violations, and
- the deletion is reviewed as a scoped cleanup.

## Scheduler Status

`khonliang-scheduler` is dormant, not abandoned. It is not part of the active
runtime today, and new workflow integration should not target it directly.
Keep its source, protocol experiments, and benchmarks as model-runtime design
reference unless a later audit identifies generated artifacts or truly dead
files.

If scheduling is revived, expose it through a bus-native scheduler/model-router
agent contract. Do not wire active clients directly to the old service shape.

## Boundary Matrix Shape

Future audit output should use this shape:

```yaml
- path: khonliang-researcher/researcher/agent.py
  functional_split: Research & Evidence
  current_repo: khonliang-researcher
  target_repo: khonliang-researcher
  package_role: app_runtime
  classification: migration_bridge
  integration_mode: compatibility_mcp
  dependency_source: github_package
  import_boundary: allowed_bus_contract
  action: migrate
  priority: high
  note: wraps MCP tools via BaseAgent.from_mcp; replace with native bus skill registration
```

Package roles:

- `app_runtime`
- `shared_interface`
- `shared_primitive`
- `dormant_service`
- `compatibility_bridge`

Dependency sources:

- `stdlib`
- `third_party_package`
- `github_package`
- `local_editable_dev`
- `sibling_path_violation`
- `undeclared_violation`

Import boundaries:

- `allowed_shared_lib`
- `allowed_core_lib`
- `allowed_bus_contract`
- `app_to_app_violation`
- `lib_to_app_violation`
- `local_path_violation`

## Open Decisions

- Whether `khonliang-developer-lib` is needed, or whether developer remains an
  app-only domain for now.
- Whether the knowledge librarian is a separate agent in researcher or a
  distinct application later.
- Whether the scheduler is revived as a Python bus agent, a wrapped dormant Go
  service, or remains only a design reference.
- Which direct MCP servers survive as debug-only entrypoints after bus-native
  conversion.

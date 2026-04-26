# khonliang-developer

Development lifecycle agent. Researcher ingests external knowledge into the
corpus; developer consumes the corpus to produce internal artifacts: FRs, specs,
milestones, work units, code handoffs, git/PR operations, and implementation
progress tracking. Claude should focus on code and review while developer
handles upstream planning, evidence lookup, and dispatch.

## Status

Active. Developer is the authoritative owner for FR lifecycle, dependencies,
work-unit bundling, milestone/spec handoff, and development progress. It is
served through the khonliang bus as a registered agent in the primary
bus-native mode, while direct MCP usage remains supported for direct Claude
connections.

## Stack

- Python, async throughout
- Local LLMs via Ollama (best-of-N evaluation, same models as researcher)
- SQLite-backed stores: KnowledgeStore, TripleStore, DigestStore (from khonliang) pointed at developer's own database
- Native khonliang-bus agent, plus direct MCP server compatibility
- Cross-app communication via khonliang-bus client (HTTP + WebSocket)

## Ecosystem position

```
INFRASTRUCTURE (services)
├─ khonliang-scheduler  — LLM inference scheduling
└─ khonliang-bus        — agent bus service, service registry, artifacts, MCP adapter

LIBRARIES (Python)
├─ khonliang            — agents, MCP transport, stores, consensus
├─ khonliang-bus-lib    — agent base/client for bus registration and requests
└─ researcher-lib       — relevance, graph, synthesis, best-of-N, idea parsing

AGENTS/APPS
├─ researcher  — ingest world: papers, OSS, RSS → corpus and evidence
└─ developer   — own dev lifecycle: FRs, specs, work units, git/PRs  ← THIS REPO
```

## Architecture boundary

- **khonliang** = library. Agent primitives. Don't reimplement.
- **khonliang-bus** = service. Use bus-lib and bus tools for registration,
  request/reply, artifacts, and cross-agent coordination. Don't reinvent
  messaging.
- **researcher-lib** = library. Use its evaluation primitives (best-of-N, idea parsing, relevance, graph). Don't duplicate.
- **researcher** = sibling agent/app. Ingestion authority. Talk through the bus
  for on-demand corpus operations. Don't ingest papers yourself.
- **developer** = this repo. Owns dev cycle: FR lifecycle, dependency tracking,
  spec/milestone management, work-unit orchestration, git/PR support, and
  dispatch to Claude.

When in doubt: if it's about producing artifacts (specs, code, FRs, worktrees), it's developer. If it's about ingesting the world, it's researcher.

## Core Capabilities

### Spec/milestone management
- Ingest spec/milestone files (markdown with FR## references)
- Backward traversal: milestone → specs → FRs → backing evidence
- Section extraction, frontmatter parsing

### Evaluation pipeline
- Cross-reference spec decisions against research corpus
- Best-of-N self-distillation (3 parallel 7B runs)
- Tag spec docs with reasoning so it's MCP-accessible to Claude later

### FR lifecycle
- Own FR storage and lifecycle. Researcher may suggest/promote ideas, but active
  FR state lives in developer.
- Manage status progression (open → planned → in_progress → completed)
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
- Register skills with khonliang-bus.
- Use bus artifacts for large outputs, test logs, diffs, specs, and handoffs.
- Publish developer lifecycle events.
- Request on-demand evidence from researcher when evaluating specs or work
  units.
- Audit repositories for docs drift, stale/deprecated paths, config hygiene,
  cleanup plans, and test plans.

## Current Hygiene Direction

- Keep developer as the FR lifecycle authority. Researcher should not retain
  active FR ownership paths.
- Prefer bus-mediated agent skills over direct sibling MCP calls.
- Store large command/test/git outputs as artifacts and return compact refs.
- Use `create_session_checkpoint` before long idle breaks or handoff, then
  `resume_session_checkpoint` to relaunch from durable state.
- Use `audit_repo_hygiene` before cleanup work and `apply_repo_hygiene_plan`
  to persist the audit artifact before editing code/docs.
- Build repo-directed cleanup/documentation workflows so developer can audit a
  repo, propose cleanup, apply scoped edits, and leave distilled artifacts.

## Git mutation primitives (fr_developer_44fc7dde)

Prefer `developer.git_pr_commit_push(cwd, branch, message, paths)` over
chained bash `git add/commit/push` for any mutating sequence. The composite
fails fast on branch mismatch (cwd-vs-declared), refuses wildcard staging,
and refuses protected-branch targets — closing the wrong-cwd trap that
landed Episode 19's mis-routed direct-to-main push.

`git_push`, `git_commit`, and `git_stage` ship with the same guards in
isolation: `allow_main=False` for protected-branch refusal, `branch_hint`
for commit-vs-cwd verification, `allow_all=False` for wildcard refusal.
Pass the opt-in only for the rare cases where bypass is intentional
(release tagging, intentional bulk add, etc.).

When bash is unavoidable, always pin the cwd in the same invocation:
`cd /abs/path && git ...`. Never compose a chained pipeline that relies on
the ambient shell cwd.

## MCP tool response convention

Same as researcher: token-efficient, no preamble, data-only, default to brief.

External agents pay per token. Every word must earn its place.

## Claude's role

Pure code + code review. Developer hands Claude pre-evaluated work packages with full evidence chains. Claude doesn't research, doesn't plan, doesn't evaluate. Reads context, writes code, handles PR workflow.

## Running

Preferred bus-native agent:

```bash
.venv/bin/python -m developer.agent --id developer-primary --bus http://localhost:8787 --config /abs/path/config.yaml
```

Direct MCP compatibility path:

```bash
.venv/bin/python -m developer.server --config /abs/path/config.yaml
```

For dogfooding, start and restart developer through khonliang-bus lifecycle
tools when the bus is running. Config paths must be absolute for cross-session
launches.

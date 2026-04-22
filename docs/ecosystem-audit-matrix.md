# Ecosystem Audit Matrix

Date: 2026-04-21
Source of truth: `docs/ecosystem-functional-boundaries.md`

## Purpose

This matrix turns the functional-boundary document into an executable audit
checklist. It is intentionally coarse-grained at the repo level: the next pass
should use this file to drive scoped repo audits and migration PRs, not to
replace them.

## Current Runtime Snapshot

Observed on the live bus at audit time:

- `researcher-primary` registered and healthy
- `developer-primary` registered and healthy
- `reviewer-primary` registered and healthy

Current active agents are bus-native. Developer and researcher still retain
direct MCP/debug surfaces, which should be treated as compatibility paths
rather than the target architecture.

## Repo Matrix

| Repo | Functional Split | Repo Role | Integration Mode | Config / Local-State Hygiene | Boundary Status | Recommended Action | Priority |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `khonliang-bus` | Communication Fabric | service | `bus_native` | Good: local `.mcp.json` ignored; runtime config stays local/CLI-driven | `correct_owner` | `keep`; continue as single MCP surface and artifact/runtime owner | medium |
| `khonliang-bus-lib` | Agent Contract | library | `library_only` | Good: local `config.yaml` and `.mcp.json` ignored | `correct_owner` | `keep`; extend skill/registry metadata to match boundary-reset target contract | high |
| `khonliang-developer` | Developer Workflow | app / agent | `bus_native` + `compatibility_mcp` | Good: tracked examples only (`config.example.yaml`, `.mcp.example.json`) | `migration_bridge` | `keep` active bus path; later deprecate direct MCP once all callers move to bus-native flow | high |
| `khonliang-researcher` | Research & Evidence | app / agent | `bus_native` + `compatibility_mcp` | Good: tracked examples only (`config.example.yaml`, `.mcp.example.json`) | `migration_bridge` | `keep` active bus path; plan removal of direct MCP-first assumptions after client migration | high |
| `khonliang-researcher-lib` | Research Primitives | library | `library_only` | Acceptable: no tracked local config files | `correct_owner` | `keep`; continue pulling shared research primitives out of app-level workflow code | medium |
| `khonliang-reviewer` | Evaluation Layer | app / agent | `bus_native` | Good: tracked `config.example.yaml`; local `config.yaml` ignored | `correct_owner` | `keep`; dogfood `review_text` / `review_diff` / `review_pr` across ecosystem repos | medium |
| `khonliang-reviewer-lib` | Review Primitives | library | `library_only` | Acceptable: no tracked local config files | `correct_owner` | `keep`; stabilize shared review contracts before broader client adoption | medium |
| `khonliang-scheduler` | Model Runtime & Scheduling | dormant service / reference | `dormant_service` | Acceptable for dormant state | `dormant_reference` | `document`; keep as design reference, do not wire active clients to it | low |
| `ollama-khonliang` | Core Intelligence | shared library | `library_only` + legacy direct surfaces in docs | Mixed: no tracked local config, but README still presents older MCP/gateway/orchestration surfaces prominently | `docs_drift` | `document` and `audit`; clarify this repo's role as shared intelligence primitives under the new bus/agent architecture | high |
| `autostock` | Client Application | domain app | `cli_only` / app-local runtime | Needs work: `config.yaml` is tracked; active local edits present; local runtime/MCP state still app-local | `wrong_owner` for active integration path, `docs_drift` for config shape | `migrate` to packaged imports and bus skills where appropriate; split tracked config into example + local files in a dedicated branch | high |
| `genealogy` | Client Application | domain app | `compatibility_mcp` / app-local runtime | Needs work: `config.yaml` is tracked; active local edits present | `wrong_owner` for target integration path, `docs_drift` for config hygiene | `migrate` to packaged imports and bus skills where appropriate; convert tracked machine-local config to example/local split in a dedicated branch | high |

## Cross-Cutting Findings

1. The core khonliang repos are now mostly aligned on the target shape:
   bus service + bus lib + app agents + shared libs.

2. The highest-leverage remaining boundary work is not in the core agent repos.
   It is in:
   - `ollama-khonliang` docs and scope framing
   - client-app migration (`autostock`, `genealogy`)
   - bus-lib metadata growth for richer skill contracts

3. Config hygiene is now good across the core agent repos:
   - `khonliang-developer`
   - `khonliang-researcher`
   - `khonliang-reviewer`

   Each tracks sanitized example config and ignores local `config.yaml`.

4. Config hygiene is still wrong in client apps:
   - `autostock` tracks `config.yaml`
   - `genealogy` tracks `config.yaml`

   These should be fixed in isolated cleanup branches because both repos have
   local work in progress.

5. The bus runtime snapshot shows the intended live topology:
   - developer, researcher, and reviewer all register through the bus
   - reviewer now exposes `review_text`, `review_diff`, `review_pr`, and
     `usage_summary`

   This means the next cleanup wave can treat bus-native agent lifecycle as the
   default, not a future target.

## Ordered Next Actions

1. `khonliang-bus-lib`
   - extend skill registration metadata toward the target boundary contract
   - make output schema / output mode / lifecycle state easier to audit

2. `ollama-khonliang`
   - audit README and docs for old orchestration claims that now belong to the
     bus/agent ecosystem
   - make the repo's role as shared intelligence primitives explicit

3. `autostock`
   - create a dedicated cleanup branch
   - replace tracked `config.yaml` with example/local split
   - map current app-local workflows to packaged imports vs future bus skills

4. `genealogy`
   - create a dedicated cleanup branch
   - replace tracked `config.yaml` with example/local split
   - map current app-local MCP/runtime behavior to packaged imports vs future
     bus skills

5. `khonliang-developer` and `khonliang-researcher`
   - after client migration, remove or sharply narrow direct MCP compatibility
     surfaces so bus-native integration is unambiguous

## Out Of Scope For This Matrix

- Path-level code movement inside a repo
- Version bump execution details
- Per-skill schema diffs
- Review-provider benchmarking

Those belong in scoped follow-up audits and FRs.

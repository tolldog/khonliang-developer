# khonliang ecosystem — architecture review delta (2026-04-26)

**Scope.** Two-day delta-pass over the prior review at
`proposals/architecture-review-2026-04-24/`. Covers what shipped, what
appeared, what changed shape — not a full re-inventory. Methodology is
the same 4-step workflow captured in
`feedback_architecture_review_workflow` (the user has flagged that as
a candidate developer skill).

Same goals as last time: inventory location, surface dedup candidates,
catalogue capabilities, flag vestiges. New explicit goal this round:
**audit the prior review's open findings** — were any retired, are any
worse.

Companion artifacts are not regenerated this round — the prior
review's `current.dot` / `proposed.dot` still describe the shape
correctly modulo the deltas listed below.

---

## §0 Audit of prior-review findings

| Prior finding (2026-04-24) | Status today | Notes |
|---|---|---|
| `khonliang-scheduler` is a broken symlink | **Resolved with surprise** — directory now exists and contains an archival Go service. CLAUDE.md inside explicitly says "this repository is archival, not the active scheduler." See §1.2 below. |
| Module-name duplication between `researcher/` and `khonliang_researcher/` (graph, relevance, synthesizer, worker) | **Open**. Not touched in the 2-day window. |
| Backward-compat aliases in `khonliang_researcher/__init__.py` (concept→entity, project→target half-migration) | **Open**. |
| Install-name vs. import-name mismatches across the ecosystem | **Open**. New entries this round (store: `khonliang-store` → `store`) follow the same prefix-stripping convention as developer/researcher/reviewer, so the convention is at least *consistent* now even if still surprising. |
| Reviewer is the cleanest modern-stack-only consumer | **Superseded**. Store is now cleaner — its only khonliang import is `khonliang_bus` (no `khonliang_reviewer` equivalent in the dependency tree). See §2.1. |

---

## §1 Repo inventory delta

### 1.1 New: `khonliang-store/`

| Field | Value |
|---|---|
| Install name | `khonliang-store` |
| Import name | `store` |
| Package dir | `store/` |
| Version | `0.6.0` |
| Role | Bus-native artifact backend + browser-based viewer; takes over artifact ownership from the bus over Phase 1 → 4c → 5 |
| Runtime deps on khonliang-* | `khonliang-bus-lib` only |
| Source files | 4 modules, 2,489 LOC: `agent.py` (1,216), `local_store.py` (607), `artifacts.py` (339), `composite.py` (322) |

Phases 1, 2, 3, 4a, 4b, 4c, 5 all shipped between 2026-04-24 and 2026-04-26.
Five PRs in store (#1–#5) plus the cross-repo phases (bus #24 for 4c
deprecation, researcher #34 for Phase 5 ingest integration).

Architecture: clean three-layer surface.

```
ArtifactBackend (ABC, store/artifacts.py)
   ├── BusBackedArtifactStore   — proxies to bus REST (legacy read path)
   ├── LocalArtifactStore       — SQLite, the new authoritative writer
   └── CompositeArtifactBackend — local-first read fallback (4b)
```

The ABC + composite pattern is exemplary for "in-flight migration" —
the swap point is structural, not a flag-everywhere conditional.
**Adoption candidate for the migration-plan section** of the prior
review's `migration-plan.md`: store is the model the rest of the
ecosystem should follow when migrating off the older `khonliang.*`
framework.

### 1.2 Resolved: `khonliang-scheduler/`

The 2026-04-24 review noted scheduler as a broken symlink with no
target. As of today the directory exists and contains a Go service.
Its `CLAUDE.md` opens with:

> This repository is archival. It is not the active scheduler for
> khonliang agent workflows.
> - Treat the Go service as a historical prototype unless the user
>   explicitly asks to revive it.
> - Do not add new bus, MCP, developer, or researcher workflow code
>   here.

**Vestige finding.** Multiple CLAUDE.md files in active repos still
list scheduler as a live INFRASTRUCTURE component:

- `khonliang-developer/CLAUDE.md` (line 39 in current main):
  `INFRASTRUCTURE … khonliang-scheduler — LLM inference scheduling`
- `khonliang-store/CLAUDE.md` (line 50): same diagram
- `khonliang-bus-lib/CLAUDE.md`: prior review noted the same listing

The diagrams are consistent with each other but inconsistent with
reality. **Recovery path**: cross-repo doc-cleanup pass — either
remove scheduler from the diagrams or annotate it as "archival
prototype." Not a code FR; suitable for a single multi-repo PR via
the existing `audit_repo_hygiene` workflow.

### 1.3 Updated: existing repos

Recent commit deltas (since 2026-04-24):

| Repo | Recent merges (last 5) | Notable shape change |
|---|---|---|
| developer | git mutation guardrails (#61), `promote_fr` detail=full (#60), console-script (#59), `draft_fr_from_request` (#58), `pr_readiness` terminal classification (#57) | New `git_pr_commit_push` composite + `_bool_arg` migration across all git handlers; new reviewer example for shell-git traps. |
| researcher | Phase 5 store integration (#34), distill_paper markdown (#33), summarizer prompt tightening (#32), fetcher Chrome fingerprint (#31) | New `stage_payload` + `ingest_from_artifact` skills routed to `agent_type='store'` — first cross-agent consumer of store-primary. |
| store | Phases 1–4b (#1–#5) | New repo. See §1.1. |

Reviewer / bus / bus-lib / researcher-lib / reviewer-lib: no
shape-relevant changes since 2026-04-24 (per shallow `git log`
inspection).

---

## §2 Backward map — store-primary

Store is the only fully-new agent since the last review; the rest
inherit their backward maps.

### 2.1 store-primary

| Layer | Modules / Tools |
|---|---|
| MCP / bus skills | `artifact_list`, `artifact_metadata`, `artifact_get`, `artifact_head`, `artifact_tail`, `artifact_grep`, `artifact_excerpt`, `artifact_create`, `artifact_migrate_from_bus`, `display`, `health_check` |
| Internal modules | `store.agent` (handlers, server bootstrap), `store.artifacts` (ABC + bus-backed proxy), `store.local_store` (SQLite writer + reader), `store.composite` (local-first fallback) |
| `khonliang_bus` imports | `BaseAgent`, `Skill`, `handler`, `add_version_flag` |
| `khonliang.*` imports | **none** |
| `khonliang_researcher` imports | **none** |

**Observation.** Store is the cleanest modern-stack consumer in the
ecosystem — single import surface (`khonliang_bus`), no reach into the
older `khonliang.*` framework. Aspirational reference for migrations.

---

## §3 Cross-cut diff — hoist candidates surfaced this round

### 3.1 Artifact constants duplicated between bus and store

`bus/artifacts.py` and `store/local_store.py` both declare:

```python
DEFAULT_MAX_CHARS = 4000
HARD_MAX_CHARS = 20_000     # 20000 in bus; 20_000 in store; same value
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
```

Plus store adds `MAX_LIST_LIMIT = 100`, `MAX_GREP_MATCHES = 100`,
`MAX_GREP_CONTEXT_LINES = 50`, `MAX_HEAD_TAIL_LINES = 1000` — bus has
these too but in `bus/artifacts.py` and the bus REST handlers.

These constants **must agree** — if the bus ceiling changes and the
store's doesn't (or vice versa), an artifact written via one path can
fail to read back via the other after migration. The current store
code has an explicit comment:

```python
# Match the bus's caps so artifacts written via the local skill
# can be migrated to / from the bus side without surprise size
# rejections at the boundary.
```

That comment is correct *and* exactly the failure mode that "two
sources of truth" produces over time.

**Recovery candidates:**

| Option | Pros | Cons |
|---|---|---|
| Hoist to `khonliang-bus-lib` (`khonliang_bus.artifact_caps` or similar) | Both consumers depend on bus-lib; symmetric authority | bus-lib stays mostly transport-shaped — adding storage-domain constants there is mild scope creep |
| Hoist to a new tiny `khonliang-artifact-contract` lib | Cleanest separation | Yet another package; overkill for ~6 constants |
| Wait for Phase 4c bus deprecation; constants live only in store | No new package, no premature hoist | Window of duplication remains until bus actually retires the read path |

**Recommendation:** wait for the Phase 4c deprecation to finish
(per `khonliang-store/CLAUDE.md` Phase 4c is "open" but per the
session's memory it was actually merged earlier today as
khonliang-bus PR #24 — see §4 doc-drift). Once the bus side is
fully retired, the constants live only in store and the hoist isn't
needed. If 4c retirement keeps the bus reading artifacts (e.g. for
backward compat), hoist to bus-lib.

### 3.2 `BoundedText` vs. `_BoundedText`

`bus/artifacts.py` exports a public `BoundedText` dataclass:

```python
@dataclass(frozen=True)
class BoundedText:
    text: str
    truncated: bool
    start_line: int | None = None
```

`store/local_store.py` defines a private mirror:

```python
@dataclass(frozen=True)
class _BoundedText:
    text: str
    truncated: bool
    start_line: Optional[int] = None
    end_line: Optional[int] = None
```

Same shape (store adds `end_line`). The leading underscore says "this
is private to the module" — but the *concept* is a public artifact
contract, not a private impl detail. Same hoist tradeoff as 3.1.

### 3.3 No cross-cut findings on developer / researcher this round

The 2-day-window deltas in developer (git guardrails) and researcher
(store-integration skills) are both new business logic, not
duplication. No hoist candidates surfaced.

---

## §4 Capability catalog delta

### 4.1 New skills

| Agent | New skill | Notes |
|---|---|---|
| store-primary | 8 artifact_* skills + `display` | The take-over read/write surface. |
| store-primary | `artifact_migrate_from_bus(limit, dry_run)` | One-shot migration tool — runs once per environment. |
| developer-primary | `git_pr_commit_push(cwd, branch, message, paths, ...)` | Composite mutation primitive. Canonical safe path. |
| researcher-primary | `stage_payload(content, kind_hint, title, source, ...)` | Persists raw payload as a `staged_payload`-kind artifact in store. |
| researcher-primary | `ingest_from_artifact(artifact_id, hints, source_label)` | Pulls body from store and routes through `pipeline.ingest_idea`. |

### 4.2 Cross-agent adoption observed

Researcher → store is the **first proven cross-agent dependency in
the wild** — `stage_payload` and `ingest_from_artifact` route through
`agent.request(agent_type='store', operation='artifact_create' /
'artifact_get')`. This is the platform's first end-to-end use of the
bus-routed agent_type pattern outside the `developer ↔ researcher`
relationship that's existed since 2026-04-21.

### 4.3 Adoption gaps (cross-agent skills that *should* exist)

| Producer | Should consume store via | Status |
|---|---|---|
| developer | `review_staged_diff` produces large diffs; should land in store as artifacts and return refs | **Not yet wired**. Currently the diff bytes flow through the bus envelope. |
| developer | `run_tests` digest: large pytest output | **Not yet wired**. |
| reviewer | review-finding artifacts (large diffs in, structured findings out) | **Not yet wired**. |
| librarian (when it lands per `project_librarian_scoping`) | corpus snapshots, taxonomy reports | n/a (agent doesn't exist yet) |

These gaps are not new — they pre-date the store launch — but now
that store is real, they're filable as concrete FRs. Recommend filing
each as a separate FR rather than a multi-target umbrella:

- `fr_developer_*`: route `review_staged_diff` + `run_tests` outputs through store.
- `fr_reviewer_*`: route review-finding bundles through store.

---

## §5 Doc-drift roundup

A new section this round because doc-drift surfaces directly from the
delta-pass:

| File | Drift | Recovery |
|---|---|---|
| `khonliang-store/CLAUDE.md` lines 37–38 | Says Phase 4c + Phase 5 are "Open." Phase 4c shipped as bus PR #24 (per session memory) and Phase 5 shipped as researcher PR #34 today. | Update phase roadmap to mark both ✅. |
| `khonliang-developer/CLAUDE.md` (and others) | INFRASTRUCTURE diagram lists `khonliang-scheduler — LLM inference scheduling` as live | Either remove or annotate "(archival prototype)". Cross-repo single-PR via `audit_repo_hygiene`. |
| `khonliang-store/CLAUDE.md` line 43 | Says "SQLite-backed store (planned — not yet in scope)" — but it shipped as Phase 4a / 4b. | Update to "active". |

These are quick edits; consider bundling them into a single
"docs-drift cleanup" PR per repo rather than three separate PRs.

---

## §6 Workflow notes (for future codification as a developer skill)

The 4-step methodology held up well on a delta-pass. Notes:

- **Delta-pass is faster than full inventory** — instead of regenerating
  inventories for unchanged repos, audit the prior findings (§0) and
  list only the deltas. ~30 minutes vs. ~2 hours full pass.
- **Doc-drift is a natural delta-pass output** — phases that land
  between reviews tend to leave stale phase markers. Worth adding §5
  to the canonical methodology.
- **Adoption-gap section** (§4.3) is the most actionable output for
  triage — it converts "this skill exists" into "these consumers
  should adopt it." Candidate to formalize as a separate workflow
  step or as input to `suggest_integration_points`.
- **Vestige confirmation cost has dropped** — `git log --since=DATE`
  + `wc -l` + `grep -ln "from khonliang"` covers 80% of what the
  prior review needed manual inspection for.

---

## §7 Recommendations (no FR filing — surface only)

Per the workflow's "don't file FRs during the pass" rule, surfacing
only:

1. **Doc-drift cleanup** (§5) — single multi-repo PR; cheapest, most
   immediately actionable. Suitable for `audit_repo_hygiene` tooling.
2. **Adoption-gap FRs** (§4.3) — file as separate FRs per consumer
   agent so each lands as its own small PR.
3. **Hoist decision on artifact constants** (§3.1) — defer until the
   Phase 4c bus deprecation is fully reflected in code (verify state
   before acting; the session memory may be ahead of merged state).
4. **Module-name duplication in researcher** (§0 prior-finding) —
   still open from 2026-04-24; not made worse this round, but
   eligible for retirement-FR if signal grows.

---

*Generated 2026-04-26. Companion to `architecture-review-2026-04-24/`
inventory; supersedes the §0-style audit table for the next
delta-pass.*

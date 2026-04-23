# Researcher Librarian MVP

**FR:** `fr_researcher_9cbe1e54`
**Priority:** medium
**Class:** agent
**Status:** proposed
**Milestone:** `ms_researcher_b99b97ea`
**Target:** `researcher` (Phase 1 in-repo; Phase 2 potential split)
**Seed source:** `khonliang-developer/docs/researcher-librarian-spec-seed.md`

## Summary

Introduce a librarian agent role responsible for **durable library
work** — paper catalog + stable classification, taxonomy rebuilds,
concept-neighborhood rebuilds, audience-lens views, duplicate /
ambiguity detection, coverage-gap reports, and research-gap
delegation back to researcher. Researcher's scope narrows to
**evidence-pipeline work** (ingest, distill, search, synthesize,
temporary investigation, on-demand topic briefs).

Phase 1 implements the librarian inside `khonliang-researcher` as a
new bus agent role, reusing researcher's stores and existing
taxonomy primitives from `khonliang-researcher-lib`. Phase 2 splits
to a dedicated repo only if the workflow surface grows enough to
justify it.

---

## Why This Split Exists

`researcher` currently owns two lifecycles that don't actually share
much:

1. **Evidence pipeline** — transient, throughput-oriented:
   ingest → distill → search → synthesize → temporary investigation.
2. **Durable library** — stateful, organization-oriented:
   concept neighborhoods, taxonomy, stable classification, audience
   lenses, coverage detection.

These evolve at different rates and under different constraints.
The evidence pipeline churns continuously (new papers, new
distillations, transient investigation workspaces). The durable
library changes more deliberately (periodic rebuilds, classification
assignments, audience-lens refresh, gap reports).

Mixing them in one agent conflates concerns: a heavy ingest burst
competes with audience-lens query latency; a taxonomy rebuild
blocks distillation; classification drift is invisible because it
lives in the same state surface as in-flight work.

This is the same separation pattern that previously pulled FR
lifecycle work out of researcher into developer — recognizing that
what *looks like one concern* ("the researcher does research stuff")
is actually two concerns running at different cadences.

---

## Scope Boundary

### Researcher owns (kept)

- **Source ingestion** — `fetch_paper`, `fetch_paper_list`,
  `fetch_papers_batch`, `find_papers`, `ingest_file`, `ingest_github`,
  `ingest_idea`, `browse_feeds`.
- **Distillation pipeline** — `distill_paper`, `distill_pending`,
  `start_distillation`, `worker_status`.
- **Search over the evidence corpus** — `find_relevant`,
  `knowledge_search`, `paper_context`, `reading_list`, `paper_digest`.
- **Synthesis** — `synthesize_topic`, `synthesize_landscape`,
  `synthesize_project`.
- **Temporary investigation workspaces** —
  `investigation_workspace` (branchable exploratory surface; does not
  pollute the durable library graph).
- **On-demand topic briefs** — `brief_on` (shipped under
  `fr_researcher_5ad96ffe` / `tolldog/khonliang-researcher#27`;
  composes multi-query search + distill pointers).
- **Project landscape + capability tracking** —
  `project_landscape`, `project_capabilities`, `research_capabilities`.
- **Idea workflow** — `brief_idea`, `research_idea`.
- **Triples (raw relationship graph)** — `triple_add`,
  `triple_context`, `triple_query`.
- **Relevance / scoring** — `score_relevance`, `evaluate_capability`.

### Librarian owns (new)

- **Paper catalog + stable classification** — assign library-style
  taxonomy codes to papers; distinguish universal patterns from
  domain-specific specializations.
- **Taxonomy rebuilds** — deterministic rebuild of the library
  taxonomy index from current corpus state.
- **Concept-neighborhood rebuilds** — deterministic neighborhood
  graph refresh (neighbors of concept X, ranked).
- **Audience-lens views** — developer-researcher, generic researcher,
  bus/platform, project-specific, cross-project/system lenses over
  the organized library.
- **Duplicate / near-duplicate / ambiguity detection** — identify
  concept or classification ambiguity; flag for review rather than
  silently guess.
- **Coverage + health reports** — which branches are
  under-covered, which classifications are stale, which papers are
  uncataloged.
- **Research-gap delegation** — emit structured research requests
  back to researcher when the organized library reveals missing
  evidence.
- **Promotion of investigation output** — one-way promotion of
  vetted insights from temporary investigation workspaces into the
  durable library structure.

### Shared primitives (stay in `khonliang-researcher-lib`)

- `TaxonomyGroup`, `TaxonomyRelationship` — deterministic taxonomy
  data structures.
- `build_concept_taxonomy(...)` — build deterministic taxonomy from
  knowledge store input.
- `suggest_entities(...)` — missing-node candidate suggestion
  primitive (enables `fr_researcher_d443f633`'s `suggest_missing_nodes`).
- `build_investigation_workspace(...)` — workspace construction
  helper.
- Neighborhood ranking helpers.
- Reusable artifact formatting helpers.

Both agents import these; neither duplicates them.

### Ambiguous / not resolved in MVP

- `knowledge_ingest` — raw content insertion into the knowledge
  store. In Phase 1 stays where it is (researcher's path). Phase 2
  may migrate to librarian if durable-layer writes centralize there.
- Embedding model (nomic-embed-text via Ollama) — Phase 1 stays in
  researcher's config; librarian reads embeddings librarian needs
  through the shared store. Phase 2 may relocate if classification
  needs dedicated embedding runs.

---

## MVP Skill Surface

Seven operational skills for the Phase 1 librarian. Narrow by
design — avoids premature abstraction while the workflow settles.

### 1. `library_health(detail="brief")`

Summary of library state: classification coverage percentages,
stale-neighborhood count, ambiguous-node count, uncataloged paper
count, age-of-last-rebuild timestamps. `detail=compact|brief|full`
audience-aware density per the existing researcher response-mode
vocabulary.

### 2. `rebuild_neighborhoods(audience="", reason="")`

Deterministic rebuild of concept-neighborhood and taxonomy artifacts
from current corpus state. `audience` optionally scopes the rebuild
to a specific lens. `reason` is a short provenance string recorded
with the resulting artifact so later audits see why the rebuild
was triggered.

### 3. `taxonomy_report(audience="", branch="", detail="brief")`

Browse the durable library taxonomy, optionally scoped by audience
lens or a specific branch. Returns a structured tree view (compact)
or full detail per node. Does not include ambiguous nodes by
default — those surface through `identify_gaps`.

### 4. `suggest_missing_nodes(query, audience="", detail="brief")`

When a lookup misses, return ranked existing nodes/groups that are
semantically close + candidate normalized concept names the caller
might have meant. **Does not hallucinate new graph facts** — only
returns candidates; adding them to the graph is a deliberate
follow-up via the triple / classification surface.

Depends on `fr_researcher_d443f633` (currently in_progress).

### 5. `classify_paper(paper_id, audience="", detail="brief")`

Assign or refresh the stable library-style classification code /
category for a paper. Writes the classification to the durable
catalog. When ambiguous, emits a `library.classification_ambiguous`
event (see Event + Workflow Integration below for the full shape)
and returns the top-k candidates for operator resolution instead
of guessing.

### 6. `promote_investigation(workspace_id, target_branch="", reason="")`

One-way promotion of vetted insights from an investigation
workspace into the durable library. Reads the workspace's output,
maps it to library taxonomy groups / neighborhoods, and writes
the durable artifacts. Source workspace is not modified (one-way);
if additional work is needed on the same topic, a new workspace
starts from the promoted library state.

### 7. `identify_gaps(audience="", branch="", detail="brief")`

Coverage report identifying under-covered concepts or branches.
Emits one `library.gap_identified` bus event per gap — each with a
**structured research request** (see Event + Workflow Integration
for the full payload shape). The event is the durable artifact;
researcher picks up the request asynchronously via its own
consumer skill (see note below — new researcher-side FR required).

This is the delegation primitive from librarian back to researcher.
**The librarian does not write directly to researcher's ingest
queue** — it publishes the research-request event and researcher
translates intent into concrete searches / URL fetches via a new
`consume_research_request` skill (TBD FR, adjacent to this
milestone — see "Cross-cluster dependencies" below).

---

## Data + Storage Plan

Phase 1 (in-repo): librarian reuses researcher's existing
`KnowledgeStore`, `TripleStore`, `DigestStore` (the same SQLite
databases under researcher's config). The librarian adds new
storage for library-specific artifacts:

- **Paper catalog table** — maps `paper_id` → `classification_code`,
  `audience_tags`, `classification_at`, `classification_version`.
- **Neighborhood snapshots** — artifact-style blobs keyed by
  `(audience, rebuilt_at)`. Each rebuild produces a new snapshot;
  snapshots are immutable. Old snapshots are eligible for pruning
  after a configurable retention window.
- **Ambiguity log** — append-only record of ambiguous classifications
  + candidate options, linking to the operator-resolution workflow.
- **Gap reports** — append-only record of `identify_gaps` outputs
  so historical delegation to researcher is auditable.

Where these physically live:
- **Paper catalog + ambiguity log + gap reports** — new tables in
  researcher's SQLite DB. Librarian reads/writes; other agents
  read only via librarian skills.
- **Neighborhood snapshots** — bus artifacts (for compact
  cross-session delivery) with pointers held in a small index
  table. This uses the existing bus artifact pattern from
  `khonliang-bus`.

Phase 2 (if needed): extract the librarian's tables + artifact
pointers into a dedicated DB under `khonliang-librarian`'s own
data directory. Migration is a copy + cutover, not a schema change.

---

## Event + Workflow Integration

The librarian is an event-driven agent. It subscribes to researcher
lifecycle events and emits its own.

### Subscriptions (from researcher)

Event names MUST match the researcher-side emitter, which is
`fr_researcher_2bdb5632`'s `watch_ingest_queue`. That FR defines
the `ingest.*` event surface explicitly — the librarian
subscribes to those names, not invented ones. Relevant subset:

- `ingest.url_distilled` — a single URL finished the full
  fetch+distill pipeline. Payload: `{paper_id, url, distilled_at,
  summary_preview}`. Librarian may trigger a `classify_paper` pass
  on the new `paper_id`.
- `ingest.queue_drained` — distillation queue transitioned
  non-empty → empty. Payload: `{drained_at, total_items_processed}`.
  Librarian may trigger `rebuild_neighborhoods`, refresh
  `library_health`, or run a coverage-gap scan at this natural
  batch boundary.
- `ingest.url_failed` — a URL failed fetch or distill. Payload:
  `{paper_id, url, stage, error_kind, error_message}`. Librarian
  cross-references against open gap reports (was this URL
  requested by librarian?) and adjusts gap-report state so
  repeated failures don't silently re-request the same source.

**Identifier naming**: the `ingest.*` event payloads use `paper_id`
to match the skill surface (`classify_paper(paper_id, ...)`) and the
paper-catalog data model (`paper_id → classification_code`). Earlier
draft notes called this `entry_id`; those two names refer to the same
stable identifier. `fr_researcher_2bdb5632` (`watch_ingest_queue`)
emits with `paper_id` as the canonical field; if its current
implementation still uses `entry_id`, the FR should rename on merge
so producer and consumer agree on one name across the surface.

Librarian does NOT subscribe to `ingest.url_queued`,
`ingest.url_fetching`, `ingest.url_fetched`, or
`ingest.url_distilling` — those are in-flight progress events
for queue-monitoring, not library-organization triggers. Staying
off the in-flight channels keeps librarian decoupled from
transient pipeline state.

### Emissions (librarian → bus)

- `library.rebuilt` — neighborhoods / taxonomy rebuilt. Payload:
  `{audience, artifact_id, reason, rebuilt_at, changes_summary}`.
- `library.classification_assigned` — paper newly classified.
  Payload: `{paper_id, classification_code, audience_tags,
  classified_at, confidence}`.
- `library.classification_ambiguous` — classification could not be
  determined cleanly. Payload: `{paper_id, candidates: [{code,
  score}], reason, logged_at}`. Caller surfaces to operator; no
  durable write to the catalog until resolved.
- `library.gap_identified` — coverage gap found. Payload is a
  **structured research request** (see shape below); researcher's
  `consume_research_request` skill (new, cross-cluster dep) reads
  this and translates intent into concrete searches / fetches.
- `library.coverage_report` — periodic health snapshot. Payload:
  `{audience, branch, coverage_pct, stale_count, ambiguous_count,
  uncataloged_count, reported_at}`.

#### `library.gap_identified` payload shape (contract)

```
{
  "request_id": "<short stable id>",           // dedupe key for researcher
  "topic": "<concept or topic phrase>",        // what to research
  "audience": "developer-researcher" | "...",  // audience lens
  "branch": "<optional taxonomy branch>",      // scoping hint
  "priority": "low" | "medium" | "high",       // urgency
  "rationale": "<short reason>",               // why this gap was flagged
  "suggested_sources": ["arxiv", ...],         // optional source hints
  "detail": "brief"
}
```

Researcher's `consume_research_request` is responsible for
translating `topic` / `audience` into its existing `find_papers`
+ `fetch_paper` pipeline. Librarian does not call researcher's
fetch surface directly.

This integrates with `fr_researcher_2bdb5632` (`watch_ingest_queue`)
— the librarian subscribes to its emitted ingest.* events, and
emits its own library.* events symmetrically on the same bus
substrate.

---

## Phase 1 Implementation Shape

**Location:** new module path inside `khonliang-researcher` —
`researcher/librarian/` (or similar) exposing a separate bus agent
role via `khonliang-researcher`'s existing agent-registration
infrastructure. Launched as a sibling agent to `researcher-primary`:
`librarian-primary` with its own skill registry.

**Rationale:** starting in-repo avoids premature split. The
shared-store pattern means no data migration. The librarian's
skills register cleanly via the same bus-lib primitives. If Phase 2
splits to a dedicated repo, the module boundary makes extraction
clean: `researcher/librarian/` becomes `khonliang-librarian/librarian/`
with minimal rewiring.

**Agent registration:**
- New command-line entry: `python -m researcher.librarian.agent`
- Registers with the bus as `agent_type=librarian`, `id=librarian-primary`.
- Exposes the 7 MVP skills above.
- Uses a separate config path from researcher-primary (permits
  independent tuning, e.g. different evaluation cadences).

**Skill implementation:**
- Each skill in its own module under `researcher/librarian/skills/`.
- Taxonomy / neighborhood rebuilds use the existing
  `build_concept_taxonomy` / `suggest_entities` primitives from
  `khonliang-researcher-lib` — no reimplementation.
- Classification assignment leverages `TaxonomyGroup` +
  embedding-similarity scoring already present.
- Investigation promotion uses `build_investigation_workspace`
  for the input-side read, then writes to the new paper catalog
  / neighborhood snapshot tables.

**Tests:** per skill, cover deterministic-rebuild idempotency,
ambiguity detection, cross-agent event emission/consumption via
a test bus fixture.

---

## Acceptance Criteria

1. A `librarian-primary` agent registers through the bus
   (`bus_services` shows it with 8 registered skills: the 7 MVP
   skills plus `health_check` as per-agent hygiene).
2. `rebuild_neighborhoods` persists:
   - taxonomy index
   - concept neighborhoods

   The rebuild additionally triggers a cascade of per-paper
   `classify_paper` calls (for papers whose classification is
   stale relative to the new taxonomy), which in turn write:
   - stable paper classification records (via `classify_paper`)
   - compact classification-change artifacts (via `classify_paper`)

   Persisting classification records and classification-change
   artifacts is `classify_paper`'s responsibility — not
   `rebuild_neighborhoods`'. `rebuild_neighborhoods` owns the
   taxonomy/neighborhood artifacts and kicks off the classify
   cascade; it does not write to the paper catalog directly.

   All writes (taxonomy index, neighborhood snapshots, and the
   per-paper classification records produced by the triggered
   `classify_paper` cascade) are idempotent: running
   `rebuild_neighborhoods` twice with an unchanged corpus
   produces identical artifact content end-to-end.
3. `taxonomy_report(audience="developer-researcher")` returns a
   developer-focused taxonomy view that does not drown in generic
   groups. Similar per other audience lenses.
4. `suggest_missing_nodes(query="...")` returns ranked candidate
   nodes + normalized concept names from the existing graph; does
   NOT create new graph nodes.
5. After an `ingest.url_distilled` event (per-paper) and/or an
   `ingest.queue_drained` event (batch boundary — typically the
   natural rebuild trigger), the librarian refreshes durable
   organization artifacts deterministically (i.e. an automated
   rebuild on queue drain, not a lazy on-query rebuild). Event
   names must match those emitted by `fr_researcher_2bdb5632`'s
   `watch_ingest_queue` — see the Event + Workflow Integration
   section for the full subscribed-events list.
6. `identify_gaps(audience="reviewer", branch="llm-code-review")`
   emits one or more `library.gap_identified` bus events whose
   payloads conform to the structured-research-request shape
   specified in the Event + Workflow Integration section
   (`request_id`, `topic`, `audience`, `branch`, `priority`,
   `rationale`, `suggested_sources`, `detail`). All fields listed
   are required on the wire; `detail` defaults to `"brief"` at
   the caller when unspecified but the payload must still carry
   it explicitly so the shape is self-describing. Researcher-side
   consumption is a cross-cluster dep (new FR — see below); for
   this spec's acceptance, librarian's responsibility ends at
   the event emission with a well-formed payload.
7. When a paper can't be cleanly classified, `classify_paper` emits
   `library.classification_ambiguous` with the top-k candidates and
   does NOT write a guess to the durable catalog.
8. Tests cover: deterministic rebuild idempotency, classification
   ambiguity reporting, gap-identification event shape, investigation
   promotion one-way semantics, and cross-agent event emission/
   subscription via a test bus.

---

## Non-Goals (Phase 1)

- Dedicated `khonliang-librarian` repo — deferred to Phase 2.
- Dedicated `khonliang-librarian-lib` companion library — deferred;
  shared primitives stay in `khonliang-researcher-lib`.
- Embedding model relocation — stays in researcher's config.
- Cross-library federation (multiple named libraries in one deploy)
  — out of scope; single library for Phase 1.
- Auto-migration of existing research from the current
  unstructured corpus into classified catalog — handled as a
  batch backfill script post-MVP, not part of the agent's
  first-cut surface.
- Bi-directional sync with external library systems (Zotero,
  Mendeley, etc.) — not in scope.

---

## Open Questions (from seed, unresolved)

1. **Runs continuously or on-demand?** Event-driven subscription
   suggests "continuously running process subscribed to bus events";
   but periodic queue-drain refresh could also be a cron-style
   invocation. Final answer likely depends on load characteristics
   observed once Phase 1 is running.
2. **Durable artifacts: researcher DB or bus artifacts or both?**
   Current plan is "both, per artifact type" (tabular data in DB,
   large snapshots as bus artifacts). Revisit if this boundary
   becomes awkward in practice.
3. **Paper classification storage shape?** Current plan is a new
   table in researcher's SQLite. Alternative: parallel index keyed
   to paper_id, separable later. Evaluate when writing the migration.
4. **Multi-library model?** Explicitly out of MVP. When real need
   surfaces (e.g. project-specific libraries + a platform-wide one),
   revisit with a concrete requirements note.
5. **Which current researcher skills become librarian wrappers vs
   stay researcher-owned?** Phase 1 keeps search/synthesis clearly
   researcher-owned per the scope boundary. Wrappers (researcher
   skill forwards to librarian) are an option only if observed usage
   shows duplication.

---

## FR Cluster + Milestone Reference

This spec ties the following FRs (per `ms_researcher_b99b97ea`):

| FR | Role | Priority | Status |
|---|---|---|---|
| `fr_researcher_9cbe1e54` | Primary milestone — librarian agent | medium | open |
| `fr_researcher_d443f633` | Enabling — suggest_missing_nodes primitive | medium | in_progress |
| `fr_researcher_3b991fa9` | Adjacent — evidence-source catalog distinct from dev-repos | medium | open |
| `fr_researcher_2bdb5632` | Adjacent — watch_ingest_queue (ingest.* bus events librarian subscribes to) | medium | open |
| `fr_researcher_fa450606` | **Cross-cluster (blocking ACC-6 loop closure)** — consume_research_request on researcher (consumer for `library.gap_identified`) | medium | open |
| `fr_researcher-lib_aabc86bc` | Optional — session context distillation for agent memory | low | open |

Dependencies into this spec from outside the cluster:
- `fr_developer_6c8ec260` (watch_pr_fleet) — **merged**; provides
  the bus-event substrate (shared pattern for `ingest.*` /
  `library.*` events).

### Cross-cluster blocking dependency: `fr_researcher_fa450606`

`library.gap_identified` requires a researcher-side consumer to
translate the structured research request into concrete searches /
URL fetches. Researcher's current ingest surface is URL-oriented
(`fetch_paper`, `fetch_paper_list`, `find_papers`), not
research-request-oriented — the handoff would be missing without
an explicit consumer skill.

**Tracked as** `fr_researcher_fa450606`
(`consume_research_request` on researcher). Filed 2026-04-22.
Medium priority. Promoted + linked here so the milestone doesn't
stall on an unnamed dependency.

**Relationship to this milestone's acceptance criterion 6**: ACC-6
requires that `identify_gaps` emit well-formed events. That is
bounded by librarian-side work and does NOT require
`fr_researcher_fa450606` to merge first. BUT the
**delegation-closes-the-loop property** (gap identified →
researcher actually consumes → new papers enter ingest pipeline →
coverage improves) is only realized once `fr_researcher_fa450606`
lands. Options:

- **Option A (recommended)**: ship librarian MVP with ACC-6 as
  "event emission" only; `fr_researcher_fa450606` lands as a
  follow-up that completes the loop. Librarian's events become
  useful telemetry immediately and close the loop when the
  consumer merges.
- **Option B**: bring `fr_researcher_fa450606` into
  `ms_researcher_b99b97ea` scope and require loop-closure for MVP
  acceptance. Larger initial ship but cleaner story.

This spec defaults to Option A. Operator choosing Option B should
update the milestone's `fr_ids` to include `fr_researcher_fa450606`
and tighten ACC-6 to require end-to-end delegation-consumed path.

---

This spec supersedes the minimal auto-generated `draft_spec` on
`ms_researcher_b99b97ea`. The milestone's draft_spec remains as a
structural scaffold; this document is the design-intent source of
truth for implementation.

---

## Provenance

- **Seed doc** (user-authored 2026-04-22):
  `khonliang-developer/docs/researcher-librarian-spec-seed.md`.
- **Scoping memory** (session-side running notes, local to the
  authoring session): produced during the 2026-04-22 working session;
  not committed to the repo. Key decisions from those notes are
  reflected in this spec and in the dogfooding-log entries cited
  below.
- **Dogfooding log Episode 15**: records the milestone-workflow
  friction that surfaced the need for this hand-written spec rather
  than relying on the auto-generated draft.
- **Charter alignment**: Platform tier (core) — `proposals/true-north.md`.
  Reuses existing agent primitives (charter Tier 1 discipline); no
  new repos until workflow stabilizes (Phase 2 gate).

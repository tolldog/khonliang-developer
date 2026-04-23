# Researcher-Librarian Spec Seed

Date: 2026-04-22
Status: draft seed
Source FR cluster:

- `fr_researcher_9cbe1e54` — Add researcher-librarian agent for corpus organization
- `fr_researcher_d443f633` — Suggest missing nodes from concept neighborhoods
- `fr_researcher_3b991fa9` — Reframe researcher repo registry as evidence-source catalog distinct from dev repos
- `fr_researcher_2bdb5632` — `watch_ingest_queue` — researcher watcher publishing ingest.* bus events
- `fr_researcher-lib_aabc86bc` — Session context distillation for agent memory

## Why This Exists

`researcher` currently owns two different kinds of responsibility:

1. evidence pipeline work:
   - ingest
   - distill
   - search
   - synthesize
   - temporary investigation

2. durable library work:
   - concept neighborhoods
   - taxonomy and stable classification
   - audience-lens browsing
   - corpus organization
   - ambiguity and gap detection

The first group is properly `researcher`. The second group has a different
lifecycle and should move behind a distinct `librarian` boundary, just as
developer lifecycle work was previously pulled out of researcher into
`developer`.

## Distilled Problem Statement

The ecosystem needs a `researcher-librarian` agent that owns the durable,
organized view of the corpus after ingestion and distillation complete.

The librarian should:

- maintain deterministic concept neighborhoods
- assign stable library-style taxonomy codes to papers and concept groups
- distinguish universal patterns from domain-specific specializations
- expose audience lenses such as developer-researcher, generic researcher,
  bus/platform, project-specific, and cross-project/system
- detect duplicate, near-duplicate, missing, and ambiguously classified nodes
- produce compact artifacts for downstream sessions instead of forcing them to
  reconstruct context from raw papers and triples
- identify coverage gaps and delegate evidence gathering back to `researcher`

The librarian should not replace `researcher`. It should sit on top of
researcher's ingest/distill outputs and direct further research when the
organized library reveals missing coverage.

## Current Code Anchors

These code surfaces already exist and define the starting point:

- `khonliang-researcher-lib` deterministic taxonomy primitives:
  - `TaxonomyGroup` / `TaxonomyRelationship`
  - `build_concept_taxonomy(...)`
  - `suggest_entities(...)`
  - `build_investigation_workspace(...)`

- `khonliang-researcher` current app surfaces:
  - `concept_taxonomy(...)`
  - `investigation_workspace(...)`
  - `brief_on(...)`
  - `register_repo(...)`
  - `list_repos(...)`

These are the likely migration seams:

1. keep reusable graph/taxonomy/workspace primitives in `researcher-lib`
2. move durable corpus-organization workflow into a new librarian agent role
3. leave ingest/distill/search/synthesis in researcher

## Boundary

### Researcher owns

- source ingestion
- queue execution
- fetch/distill pipelines
- raw and distilled evidence
- search and synthesis
- temporary investigation workspaces
- on-demand topic briefs over the corpus

### Librarian owns

- paper catalog and stable classification
- taxonomy rebuilds
- concept neighborhood rebuilds
- audience-lens views
- duplicate / overlap / ambiguity detection
- coverage reports and library-health summaries
- promotion of durable organization artifacts
- research-gap identification and delegation requests back to researcher

### Shared primitives live in researcher-lib

- deterministic taxonomy/group structures
- neighborhood ranking helpers
- investigation-workspace helpers
- reusable artifact formatting helpers where appropriate

## MVP Skill Surface

First-pass librarian skills should be narrow and operational:

1. `library_health(detail="brief")`
   - summarize classification coverage, stale neighborhoods, ambiguous nodes,
     and uncataloged papers

2. `rebuild_neighborhoods(audience="", reason="")`
   - rebuild deterministic neighborhood and taxonomy artifacts from current
     corpus state

3. `taxonomy_report(audience="", branch="", detail="brief")`
   - browse the durable library taxonomy, scoped by audience lens

4. `suggest_missing_nodes(query, audience="", detail="brief")`
   - when a lookup misses, return ranked existing nodes/groups and candidate
     normalized concepts without hallucinating new graph facts

5. `classify_paper(paper_id, audience="", detail="brief")`
   - assign or refresh stable library-style classification code/category

6. `promote_investigation(workspace_id, target_branch="", reason="")`
   - move vetted insights from temporary investigation outputs into the durable
     library structure using one-way promotion

7. `identify_gaps(audience="", branch="", detail="brief")`
   - report under-covered concepts / branches and prepare structured research
     requests for `researcher`

## Event / Workflow Integration

The librarian should integrate with the existing event-driven model:

- consume ingest/distill completion signals
- run or be invoked after queue drain
- emit compact artifacts describing:
  - rebuilt taxonomy
  - classification changes
  - gap reports
  - ambiguous classifications

This aligns directly with:

- `fr_researcher_2bdb5632` — `watch_ingest_queue`
- existing bus artifact patterns
- the new local review / event-driven agent workflow

## Evidence-Source Catalog Direction

`fr_researcher_3b991fa9` should be treated as part of the same boundary move.

The current `register_repo` / `list_repos` surface is overloaded. Librarian
needs a stable understanding of what corpus sources exist, while developer owns
where work happens. The evidence-source catalog should stay on the research /
library side, distinct from developer's dev-repo registry.

This matters because multi-library and external-source scenarios will break the
current assumption that every registered repo is both:

- a research source
- a development location

Those are different lifecycles and should remain separate.

## Corpus Signals

The local corpus already points in this direction:

- concept taxonomy and audience-lens work is present
- investigation workspaces already distinguish temporary exploration from the
  long-lived graph
- corpus queries for librarian-oriented framing surface:
  - concept-map freshness
  - evidence-subset / audience-lens work
  - milestone planning over concept neighborhoods

This is enough signal to proceed with a librarian design without inventing a
new abstraction from scratch.

## Proposed First Implementation Shape

Phase 1:

- implement `librarian` inside `khonliang-researcher` as a new bus agent role
  or role-like agent surface
- reuse current researcher stores and researcher-lib primitives
- keep a single repo while the workflow stabilizes

Phase 2:

- if the workflow/state surface grows enough, split to a dedicated
  `khonliang-librarian` repo and, if needed, `khonliang-librarian-lib`

Starting inside researcher keeps the first move cheap and avoids premature repo
creation while the boundary is still settling.

## Suggested Acceptance Criteria

1. A librarian agent registers through the bus and exposes library-facing
   skills distinct from ingest/distill/search skills.

2. Librarian can rebuild and persist:
   - taxonomy index
   - concept neighborhoods
   - stable paper classification records
   - compact classification-change artifacts

3. Librarian can answer audience-lens queries without generic groups drowning
   out domain-specific ones.

4. Missing-node lookups return ranked suggestions and candidate normalized
   concepts without hallucinating graph nodes.

5. After ingest/distill completion, librarian can refresh durable organization
   artifacts deterministically.

6. Librarian can identify a coverage gap and emit a structured research request
   that `researcher` can act on.

## Open Questions

1. Should librarian run continuously, or primarily as an on-demand / queue-drain
   worker?
2. Where should durable library artifacts live: researcher DB, bus artifacts,
   or both?
3. Should paper classification be stored as knowledge metadata, a dedicated
   catalog, or a parallel index?
4. What is the minimum viable cross-library model once multiple libraries exist?
5. Which current `researcher` skills become wrappers over librarian, and which
   remain purely researcher-owned?

## Immediate Next Step

Create a real milestone/spec from this FR cluster, using:

- `fr_researcher_9cbe1e54` as the primary milestone FR
- `fr_researcher_d443f633` as the key enabling FR
- `fr_researcher_3b991fa9` and `fr_researcher_2bdb5632` as adjacent workflow
  and catalog integration work

Treat `fr_researcher-lib_aabc86bc` as optional future support work, not a hard
MVP dependency.

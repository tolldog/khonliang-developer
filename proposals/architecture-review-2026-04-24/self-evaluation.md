# khonliang self-evaluation against the agent-split rules (2026-04-24)

Companion to `inventory.md`. Takes the three rules surfaced in the
architecture-review session and scores the current live agents.

## Rules recap

1. **Work semantics vs. transport.** Business logic defines *what* work
   means (descriptors); transport defines *how* it runs. Agent-ification
   is a per-work-type transport choice.
2. **Reusability test.** Reusable *code* → library. Reusable *work* →
   agent. Unique logic → app.
3. **Agent taxonomy.** Transport-only (no LLM) vs. LLM-augmented (model
   call in the loop). Different design pressures per category.

Plus a framing refinement: **agents are abstraction layers whose contracts
are deliverables, not code.** A module exports callable code; an agent
exports work-with-a-deliverable. Deliverable cohesion is the cheap sniff
test for "is this agent too big?"

## Live-agent scoreboard

### developer-primary — 69 skills, v0.2.0

- **Intended scope:** the khonliang software-engineering workflow (FR /
  milestone / spec / bug / dogfood / PR / session / hygiene lifecycle
  across every khonliang-* repo). Scoped, not universal, and that is the
  correct choice — not every agent needs to be cross-domain like
  reviewer.
- **Reusability (scope-qualified):** High. Serves ≥8 khonliang-* repos
  today (bus-lib, bus, developer, researcher, researcher-lib, reviewer,
  reviewer-lib, ollama-khonliang, plus the live librarian). Plenty of
  callers within the intended scope.
- **Transport profile:** Mixed. Most skills are transport-only (file
  accessors, list/get/update). A handful are LLM-augmented
  (`draft_spec_from_milestone`, `fr_candidates_from_concepts`,
  `prepare_development_handoff`). Git primitives (`git_*`) are
  subprocess-transport wrappers.
- **Deliverable cohesion (scope-qualified):** Cohesive within scope. All
  10+ deliverable types (FR, milestone, spec, bug, dogfood, PR state,
  session checkpoint, hygiene report, work unit, git result) are facets
  of the same scope — khonliang software-engineering lifecycle. Not
  unrelated; just many-faceted.
- **Surface-area cost:** Real regardless of cohesion. 69 skills is 69
  interfaces to document, test, keep in sync. Surface-area pressure is
  a maintenance tax even when everything is on-scope.
- **Helpful vs. extra work:** Strongly positive for the intended scope.
  The pressure point is surface area, not sprawl.

**Natural sub-axes visible in the skill list:**
- `fr-store` — promote_fr, update_fr, list_frs, merge_frs, next_fr,
  fr_candidates_from_concepts (+ milestone + spec lifecycle)
- `bug-triage` — file_bug, list_bugs, triage_bug, log_dogfood,
  triage_dogfood, dogfood_triage_queue, report_gap, close_bug, link_bug_*
- `pr-ops` — watch_pr_fleet, pr_fleet_status, list_pr_watchers,
  stop_pr_watcher, pr_ready
- `git` — git_status, git_log, git_diff, git_branches, git_commit,
  git_stage, git_unstage, git_checkout, git_create_branch, git_delete_branch,
  git_fetch, git_pull, git_push, git_show, git_rev_parse
- `session` — create_session_checkpoint, resume_session_checkpoint,
  prepare_development_handoff
- `hygiene` — audit_repo_hygiene, apply_repo_hygiene_plan, run_tests
- `research-handoff` — get_paper_context, fr_candidates_from_concepts,
  migrate_frs_from_researcher

**Structural observations:**
- `git_*` is the clearest hoist candidate, but not because developer is
  sprawling — because git operations have a **broader intended scope**
  than developer itself. Reviewer, autostock, any Python app needs git.
  The 15 `git_*` skills have a natural scope (universal) wider than
  their current home (khonliang software-engineering). Hoisting them
  into bus-lib (or a `git-lib`) gives every agent git operations
  without routing through developer, and correctly matches scope to
  location.
- `research-handoff` skills could potentially consume from researcher
  directly rather than re-expose, but this is a minor simplification.
- The 69-skill surface is a maintenance tax. Surface-area reduction
  (consolidation, better sub-axis grouping, or eventual split along
  deliverable facets if pressure keeps rising) is a periodic hygiene
  concern — not an urgent structural fix.

---

### researcher-primary — 57 skills, v0.1.0

- **Reusability:** High. `BaseResearchAgent` is already subclassed by
  `DeveloperResearcher` (defined, even if not running). The pattern
  accepts `DomainConfig` per caller domain. Genuinely domain-neutral
  design.
- **Transport profile:** Primarily LLM-augmented (distillation,
  synthesis, brief_on). Some transport-only (register_repo, list_repos,
  knowledge_search, reading_list). Good mix.
- **Deliverable cohesion:** Mostly cohesive. Deliverable types cluster
  into: papers (entries + distillations), concepts (graph, matrix,
  taxonomy), ideas (ingest → research → brief), briefs / syntheses.
- **Helpful vs. extra work:** Very positive. The knowledge layer is a
  genuine cross-cutting asset. Surface is big but internally consistent.

**Visible internal axes worth naming (for future cohesion pressure):**
- `paper-*` — fetch, fetch_list, fetch_batch, ingest_file, ingest_github,
  distill, distill_pending, paper_context, paper_digest, reading_list,
  worker_status, start_distillation
- `concept-*` — concept_matrix, concept_tree, concept_path,
  concept_taxonomy, synergize_concepts, synergize, synergize_compare,
  concept_map_freshness, concepts_for_project
- `idea-*` — ingest_idea, research_idea, brief_idea
- `search-*` — find_papers, find_relevant, knowledge_search,
  knowledge_ingest, knowledge_context, triple_add, triple_query,
  triple_context, score_relevance, register_evidence_source,
  list_evidence_sources, register_repo, list_repos
- `synthesis-*` — brief_on, synthesize_topic, synthesize_project,
  synthesize_landscape, evaluate_capability, research_capabilities,
  project_capabilities, project_landscape, scan_codebase
- `watcher-*` — watch_ingest_queue, list_ingest_watchers, stop_ingest_watcher
- `guide-*` (doc skills, not work) — coding_guide, research_guide,
  response_modes, catalog
- `lifecycle` — consume_research_request, browse_feeds

**Observation:** guide-* skills (coding_guide, research_guide, etc.) are
documentation endpoints, not work with deliverables. They could
reasonably live on every agent as a standard "what do I do" probe; they
don't earn their places as unique skills on researcher.

---

### librarian-primary — 8 skills, v0.2.0

- **Reusability:** High. Classification, gap identification, taxonomy —
  all corpus-general.
- **Transport profile:** LLM-augmented (classify_paper) + transport
  (library_health, taxonomy_report, suggest_missing_nodes,
  rebuild_neighborhoods, promote_investigation, identify_gaps).
- **Deliverable cohesion:** Cohesive. All outputs describe library state
  or produce library-change events.
- **Helpful vs. extra work:** **Payoff blocked on a missing subscriber.**
  `library.gap_identified` is published into a void — no agent consumes
  it. The feedback loop that would justify the agent's existence (gap →
  research-request → ingest) is not yet live. Until it is, librarian is
  producing deliverables that nobody acts on.

**Observation:** This is the clearest "agent is fine, ecosystem wiring
is incomplete" case in the fleet. Closing the subscriber loop is the
smallest unit of work with the biggest payoff here.

---

### reviewer-primary — 5 skills, v0.3.0

- **Reusability:** High. `ReviewRequest.kind` is free-form; intended from
  day one to support non-code reviews.
- **Transport profile:** LLM-augmented with exemplary provider
  abstraction (`ReviewProvider` implementations for Ollama + Claude CLI).
- **Deliverable cohesion:** Tight. Skills return `ReviewResult` or
  aggregated usage stats. One deliverable family.
- **Helpful vs. extra work:** **Best ratio in the fleet.** Small surface,
  cross-vendor abstraction done right, clean separation between agent
  and contracts (reviewer-lib has zero runtime deps). Reference for
  future LLM-augmented agents.

---

### developer-researcher — defined, not running

A `BaseResearchAgent` subclass in developer's repo that filters the
generic research skill set down to 13 evidence-focused skills for the
developer domain.

- **Rule verdict:** This is not itself an agent in the canonical sense —
  it is a *concrete instance* of a pattern. The pattern (`BaseDomainAgent
  with a SkillFilter`) is reusable code; the concrete instance is app-
  specific. Per the rules, the pattern belongs in bus-lib / researcher-
  lib, and each concrete instance belongs in its owning app.
- **Not running today.** Between-session drift or deliberate dormancy
  isn't clear from runtime alone. Needs confirmation.

---

## Summary table

| agent | skills | reusability | transport | cohesion | grade |
|---|---|---|---|---|---|
| reviewer-primary | 5 | high | LLM+provider | tight (ReviewResult) | A (reference) |
| librarian-primary | 8 | high | mixed | cohesive | B (blocked on subscriber) |
| researcher-primary | 57 | high | mixed | mostly cohesive | B+ (internal cohesion pressure building) |
| developer-primary | 69 | medium | mixed | poor (10+ types) | C (agent-sprawl) |
| developer-researcher | (defined) | pattern-instance | LLM+filter | inherits researcher | not-an-agent |

## Recommended structural moves (not FRs — triage candidates)

- **Hoist git_* into bus-lib.** 15 skills leave developer; every agent
  gets git operations for free. Single biggest shed.
- **Consider the pattern primitive: `BaseDomainAgent` with SkillFilter**
  → bus-lib. `DeveloperResearcher` becomes a thin app-side config, not a
  subclass. Same for any future domain-filtered agents.
- **Close the librarian subscriber loop.** researcher or a minimal
  auto-ingest worker consumes `library.gap_identified`.
- **Start cohesion pressure on developer.** Not a split today, but agree
  the internal axes (`fr-store`, `bug-triage`, `pr-ops`, `session`,
  `hygiene`, `research-handoff`). When a sub-axis grows further or
  starts publishing its own deliverable type, that's the moment to
  extract.
- **Move doc skills (guide_*, response_modes, catalog) into a built-in
  baseline available on every agent.** They're not unique deliverables;
  they describe the agent's own interface.

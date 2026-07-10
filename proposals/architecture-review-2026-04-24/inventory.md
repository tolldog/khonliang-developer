# khonliang ecosystem — architecture inventory (2026-04-24)

**Scope.** Shallow, breadth-first pass across all khonliang-* repos to map **what code lives where** and **how it's used**. Deliberately not about efficiency. Goals:

1. Inventory code location + actual cross-repo usage.
2. Surface deduplication candidates (code in multiple places that could hoist to a shared lib).
3. Catalogue functions/methods available to consumers (underutilized surface).
4. Flag unused vestiges — dead code, stale docs, abandoned symlinks, backward-compat aliases nobody imports.

Companion artifacts:

- `current.dot` — present state as a dependency graph.
- `proposed.dot` — suggested shape, colour-coded (existing / proposed add / proposed move / proposed retire).

This pass is **shallow**: module layout, public `__init__.py` exports, top-level imports across repos. Deeper module-body scans come later if sections show signal.

---

## §1 Repo inventory

Eight discoverable units (seven live repos + one framework package pulled via git).

| Repo (on disk) | Install name | Import name | Package dir | Role | Declared deps on other khonliang units |
|---|---|---|---|---|---|
| `khonliang-bus/` | `khonliang-bus` | `bus` | `bus/` | Bus service (FastAPI + WS + MCP adapter) | **none** in runtime deps; `agents` extra pulls `khonliang-bus-lib` |
| `khonliang-bus-lib/` | `khonliang-bus-lib` | `khonliang_bus` | `khonliang_bus/` | Agent SDK (BaseAgent, BusClient, registry enums, versioning, testing) | none |
| `khonliang-developer/` | `khonliang-developer` | `developer` | `developer/` | Developer agent + FR/milestone/spec lifecycle | `khonliang-bus-lib`, `khonliang-researcher-lib`, `ollama-khonliang` |
| `khonliang-researcher/` | `khonliang-researcher` | `researcher` | `researcher/` | Researcher agent + ingest/distill/worker | `khonliang-bus-lib`, `khonliang-researcher-lib` (git-pinned), `ollama-khonliang` |
| `khonliang-researcher-lib/` | `khonliang-researcher-lib` | `khonliang_researcher` | `khonliang_researcher/` | Research-primitives SDK (BaseResearchAgent, graph, synth, relevance, librarian primitives) | `khonliang-bus-lib`, `ollama-khonliang` |
| `khonliang-reviewer/` | `khonliang-reviewer` | `reviewer` | `reviewer/` | Reviewer agent + providers + rules | `khonliang-bus-lib`, `khonliang-reviewer-lib` (git main) |
| `khonliang-reviewer-lib/` | `khonliang-reviewer-lib` | `khonliang_reviewer` | `khonliang_reviewer/` | Content-agnostic review contracts + pricing | **none** (stdlib only) |
| `../ollama-khonliang/` (symlinked) | `ollama-khonliang` | `khonliang` | `src/khonliang/` | Foundational multi-agent framework (knowledge store, triples, MCP compact helpers, model pool, blackboard, consensus, debate, digest, research base) | — (pulled as git dep) |

Also on disk but unresolved:

- `/mnt/dev/ttoll/dev/khonliang-scheduler` → symlink to `../khonliang-scheduler` (target does **not exist**). Listed in bus-lib's ecosystem diagram under "INFRASTRUCTURE." Either abandoned, relocated, or never checked out on this machine. Flag.
- `khonliang-bus-wt/`, `khonliang-bus-lib-wt/`, `khonliang-developer-wt/`, `khonliang-researcher-wt/`, `khonliang-researcher-lib-wt/`, `khonliang-reviewer-wt/`, `khonliang-reviewer-lib-wt/` — git worktree roots for parallel-branch work. Not sources.

### 1.1 Install-name vs. import-name mismatches

| Install name | Import name | Notes |
|---|---|---|
| `khonliang-bus` | `bus` | Service repo exports bare `bus` namespace. |
| `khonliang-bus-lib` | `khonliang_bus` | Lib exports `khonliang_bus` — same prefix as service suggests but uses underscore. |
| `khonliang-researcher-lib` | `khonliang_researcher` | Lib imports as `khonliang_researcher`, **not** `khonliang_researcher_lib`. |
| `khonliang-reviewer-lib` | `khonliang_reviewer` | Same: lib imports as `khonliang_reviewer`, not `..._lib`. |
| **`ollama-khonliang`** | **`khonliang`** | Most surprising: package name has nothing to do with its import name, which shadows the ecosystem prefix. |

No intrinsic problem, but `khonliang_bus` (lib) vs. `bus` (service) vs. `khonliang` (ollama-khonliang) is a jungle. Documentation payoff for consolidating this naming convention in a single README section. Not load-bearing for the review itself, just worth noting.

---

## §2 Dependency graph (runtime, from `[project].dependencies`)

```
ollama-khonliang (khonliang)
   ↑
   │
   ├── khonliang-researcher-lib ──→ khonliang-bus-lib
   │       ↑
   │       │
   │       ├── khonliang-researcher ──→ khonliang-bus-lib
   │       └── khonliang-developer  ──→ khonliang-bus-lib
   │
   └── khonliang-developer (direct)

khonliang-reviewer-lib        (no deps)
   ↑
   │
   └── khonliang-reviewer ──→ khonliang-bus-lib

khonliang-bus (service)        (no khonliang-* runtime deps;
                                pulls khonliang-bus-lib only via `agents` extra)
```

Observations:

- `khonliang-bus-lib` is a true leaf of the ecosystem (no khonliang deps). Correct — it's the transport/SDK substrate.
- `ollama-khonliang` (`khonliang`) is also a leaf of the ecosystem (its own deps are external — httpx, ollama, etc. — not khonliang-*). It predates the bus-lib split and was not designed under it.
- `khonliang-reviewer-lib` is a leaf too, but for a different reason: it's intentionally content-agnostic (`CLAUDE.md` says so — the `ReviewRequest.kind` field is free-form to support non-code reviews). Notably **doesn't** depend on bus-lib — reviewer-lib is just types + contracts, no agent plumbing.
- `khonliang-developer` depends on **both** `khonliang-researcher-lib` and `ollama-khonliang`. The researcher-lib dep is narrow (three imports, see §4). Worth evaluating whether any of those three belong in bus-lib or its own neutral lib.
- `khonliang-researcher` pins `khonliang-researcher-lib` to a specific commit SHA while other agents pin to `main`. Drift signal — may or may not be intentional.
- The bus service itself does not share any runtime code with its SDK. Clean, defensible client/server split — agent code and transport-server code never commingle.

---

## §3 Public API per shared lib (what each lib exports)

Sourced from each lib's `__init__.py` (`__all__` and re-exports).

### 3.1 `khonliang-bus-lib` (import as `khonliang_bus`)

**Agent scaffolding:** `BaseAgent`, `Skill`, `Collaboration`, `@handler`
**Client:** `BusClient`, `Message`
**Versioning:** `add_version_flag`, `resolve_version`
**Registry enums (21 symbols):** `AggregationMethod`, `CapabilityRoute`, `ContextNeed`, `CostLevel`, `ExecutionMode`, `ExecutionProfile`, `ExecutionRun`, `LatencyClass`, `Locality`, `ModelSize`, `OutputContract`, `OutputMode`, `ProviderDescriptor`, `ProviderStatus`, `ProviderType`, `ReasoningLevel`, `RegistryValue`, `RunTier`, `RuntimeProfile`, `SkillAuthority`, `SkillDescriptor`, `SkillStatus`

Surface is heavily weighted toward registry enums (~21 of ~29 exports). Verify in §5 whether consumers actually import any of them; if not, this is shipping a lot of dead surface area.

### 3.2 `khonliang-researcher-lib` (import as `khonliang_researcher`)

Three stated layers in the docstring:

- **Layer 1 — Primitives:** `RelevanceScorer`, `cosine_similarity`, graph primitives (`EntityNode`, `build_entity_graph`, `build_entity_matrix`, `build_concept_taxonomy`, `InvestigationWorkspace`, `trace_chain`, `find_paths`, `resolve_entity`, `suggest_entities`, ...), `BaseQueueWorker`, `BaseSynthesizer`/`SynthesisResult`, `BaseIdeaParser`, `select_best_of_n`/`serialize_candidates`, `LocalDocReader`/`DocContent`, `VectorIndex`/`reciprocal_rank_fusion`
- **Layer 2 — Research-agent framework:** `BaseResearchAgent`, `DomainConfig`, `EngineRegistry`, `BaseSearchEngine`, `WebFetchEngine`, `WebSearchEngine`, `SearchResult`
- **Librarian primitives** (new, 2026-04-22-ish): `AmbiguityRecord`, `GapReport`, `LibrarianStore`, `NeighborhoodSnapshot`, `PaperClassification`, `classify_paper_from_triples`, `identify_gap_candidates`
- **Backward-compat aliases:** `ConceptNode` = `EntityNode`, `build_project_scores` = `build_target_scores`, `build_concept_matrix` = `build_entity_matrix`, `build_concept_graph` = `build_entity_graph`, `format_project_tags` = `format_target_tags`

Aliases trace a recent rename: concept→entity and project→target. See §5 for who still uses the old names.

### 3.3 `khonliang-reviewer-lib` (import as `khonliang_reviewer`)

Small and intentional (13 exports):

- **Contracts:** `ArtifactRef`, `Disposition`, `ErrorCategory`, `ReviewFinding`, `ReviewRequest`, `ReviewResult`, `Severity`, `SEVERITY_ORDER`, `severity_rank`, `UsageEvent`
- **Pricing:** `ModelPricing`, `estimate_api_cost`
- **Provider contract:** `ReviewProvider`

Deliberately content-agnostic; `ReviewRequest.kind` is free-form string to permit non-code review workflows.

### 3.4 `ollama-khonliang` (import as `khonliang`)

Much richer than the others — it's a full framework, not a narrow SDK.

Top-level modules: `client`, `cli`, `errors`, `health`, `openai_client`, `_json_utils`
Subpackages:

- `knowledge/` — `store` (`KnowledgeStore`, `KnowledgeEntry`, `EntryStatus`, `Tier`), `triples` (`TripleStore`), `librarian`, `ingestion`, `reports`
- `mcp/` — `artifacts`, `budget`, `compact` (e.g. `compact_list`, `compact_entry`, `truncate`, `brief_or_full`, `format_response`), `compress`, `server` (`KhonliangMCPServer`)
- `research/` — `base` (`BaseResearcher`), `engine` (`BaseEngine`, `EngineResult`), `models` (`ResearchTask`, `ResearchResult`), `composite`, `http_engine`, `pool` (`ModelPool`), `trigger`
- `digest/` — `store` (`DigestStore`), `middleware`, `synthesizer`
- `gateway/` — `blackboard` (`Blackboard`), `gateway`, `messages`, `observer`, `sessions`
- Plus: `agents`, `consensus`, `conversation`, `debate`, `discovery`, `integrations`, `llm`, `parsing`

This is the **pre-bus multi-agent framework**. The newer khonliang-bus ecosystem coexists with it; agent code imports from both.

---

## §4 Backward map per agent (what each consumer actually imports)

Derived from grep of `from X` across each agent package.

### 4.1 `developer/` — imports from shared libs

**From `khonliang_bus` (bus-lib):**
- `BaseAgent, Skill, Collaboration, @handler` (scaffolding)
- `add_version_flag, resolve_version` (CLI/version)

**From `khonliang_researcher` (researcher-lib):**
- `BaseResearchAgent` — used to… instantiate a researcher? Need to check why developer does this. Suspicious.
- `DomainConfig`
- `LocalDocReader`, `DocContent` (from `khonliang_researcher.doc_reader`)

**From `khonliang` (ollama-khonliang):** need to re-grep in §5; known candidates include `KnowledgeStore`, MCP compact helpers.

Only 3 symbols pulled from researcher-lib. `LocalDocReader`/`DocContent` are structure-aware local-doc readers — arguably agent-general, not researcher-specific. **Candidate hoist** to bus-lib or a new neutral doc-utils lib. `BaseResearchAgent` and `DomainConfig` being in developer is weirder — worth a deeper look.

### 4.2 `researcher/` — imports from shared libs

**From `khonliang_bus` (bus-lib):**
- `BaseAgent, Skill, @handler`, `BusConnector` (from `khonliang_bus.connector`)
- `add_version_flag`

**From `khonliang_researcher` (researcher-lib):**
- Layer 1+2 primitives: `BaseResearchAgent`, `BaseIdeaParser`, `BaseQueueWorker`, `RelevanceScorer`, graph stack (`build_concept_graph` alias, `build_concept_taxonomy`, `build_concept_matrix` alias, `trace_chain`, `build_project_scores` alias, `cosine_similarity`, `find_paths`, `format_entity_suggestions`, `suggest_entities`)
- Librarian primitives: `AmbiguityRecord`, `GapReport`, `LibrarianStore`, `NeighborhoodSnapshot`, `PaperClassification`, `classify_paper_from_triples`, `identify_gap_candidates`
- Workspace primitives: `build_investigation_workspace`, `format_investigation_workspace`
- Best-of-N: `select_best_of_n`, `serialize_candidates`

**From `khonliang` (ollama-khonliang):**
- `khonliang.knowledge.store` — `KnowledgeStore`, `KnowledgeEntry`, `Tier`, `EntryStatus`
- `khonliang.knowledge.triples` — `TripleStore`
- `khonliang.pool` — `ModelPool`
- `khonliang.mcp` — `KhonliangMCPServer`, `compact_list`, `compact_entry`, `truncate`, `brief_or_full`, `format_response`
- `khonliang.digest.store` — `DigestStore`
- `khonliang.gateway.blackboard` — `Blackboard`
- `khonliang.research.base` — `BaseResearcher`
- `khonliang.research.engine` — `BaseEngine`, `EngineResult`
- `khonliang.research.models` — `ResearchTask`, `ResearchResult`

Researcher pulls the *widest* slice of `khonliang` by far. A lot of the research/ module inside researcher may be a thin adapter between `khonliang.research.*` (legacy) and `khonliang_researcher.*` (new). Strong candidate for focused deep-scan in a second pass.

### 4.3 `reviewer/` — imports from shared libs

**From `khonliang_bus` (bus-lib):**
- `BaseAgent, Skill, @handler`
- `add_version_flag`

**From `khonliang_reviewer` (reviewer-lib):**
- Providers (`claude_cli.py`, `ollama.py`): `ReviewFinding`, `ReviewProvider`, `ReviewRequest`, `ReviewResult`, `UsageEvent`
- Agent (`agent.py`): `SEVERITY_ORDER`, `ReviewFinding`, `ReviewRequest`, `ReviewResult`, `severity_rank`
- Plus: `ModelPricing`, `estimate_api_cost`

**From `khonliang` (ollama-khonliang):** not observed in the grep. Reviewer appears to **not** pull ollama-khonliang at all — its config shows `openai>=1.40` directly instead (probably talking to Ollama via its OpenAI-compat endpoint).

Reviewer is the cleanest consumer — imports only from bus-lib + reviewer-lib. Confirms reviewer-lib's intentional minimalism and reviewer's design as a kind-agnostic agent built on the modern stack, free of the legacy `khonliang` framework.

### 4.4 `bus/` (service) — imports from shared libs

No imports from `khonliang_bus` (lib). No imports from `khonliang` (ollama-khonliang). Bus is a standalone FastAPI app that *defines* the contract; the lib reflects it. Clean.

---

## §5 Cross-cut observations + vestige candidates

### 5.1 Module-name duplication between `researcher/` and `khonliang_researcher/`

Both packages contain files with identical names:

| Filename | In `researcher/` (agent) | In `khonliang_researcher/` (lib) |
|---|---|---|
| `agent.py` | agent bootstrap; subclasses `BaseResearchAgent` | `BaseResearchAgent` definition |
| `graph.py` | uses backward-compat names (`ConceptNode`, `build_concept_matrix`, `build_project_scores`, `format_project_tags`) | new names (`EntityNode`, `build_entity_matrix`, `build_target_scores`, `format_target_tags`) |
| `relevance.py` | embedding scorer via aiohttp + local Ollama | `RelevanceScorer` class |
| `synthesizer.py` | cross-paper synth using `khonliang.knowledge.store` + `khonliang.pool` | `BaseSynthesizer` |
| `worker.py` | background distillation worker | `BaseQueueWorker` |

Strong smell: researcher has **local implementations sitting next to lib versions with the same name**. Either the local versions are thin adapters that still carry weight, or they're pre-extraction fossils that nobody noticed. A deep pass (file-body diff, are the local ones still referenced?) will tell. **Highest-signal vestige-hunt target.**

### 5.2 Backward-compat aliases in researcher-lib

Aliases `ConceptNode`, `build_project_scores`, `build_concept_matrix`, `build_concept_graph`, `format_project_tags` are used **only by researcher's own code** (`researcher/graph.py`, `researcher/cli.py`, `researcher/server.py`, `researcher/synthesizer.py`, `researcher/librarian_agent.py`). No consumer outside researcher uses them. Finishing the rename in-place in researcher lets us drop the aliases entirely.

### 5.3 Stale ecosystem diagram in `khonliang-bus-lib/CLAUDE.md`

The "Ecosystem position" section says:

```
LIBRARIES
├─ khonliang          — agent primitives, stores, MCP transport
├─ khonliang-bus-lib  — agent SDK for bus interaction  ← THIS REPO
└─ researcher-lib     — evaluation primitives

APPS (agents)
├─ researcher         — ingests the world → corpus → FR ideas
└─ developer          — consumes corpus → specs, milestones, dispatch

INFRASTRUCTURE
├─ khonliang-bus      — agent orchestration platform ...
└─ khonliang-scheduler— LLM inference scheduling
```

Omits `khonliang-reviewer` + `khonliang-reviewer-lib` (added to the ecosystem after this doc was written). Also references `khonliang-scheduler`, which is a broken symlink on this machine. Refresh candidate.

### 5.4 `khonliang-scheduler` symlink is broken

`/mnt/dev/ttoll/dev/khonliang-scheduler` → `../khonliang-scheduler` → no such directory. Memory records don't reference it recently; bus-lib's diagram still mentions it. Three possibilities: (a) deliberately not checked out locally, (b) relocated and symlink not updated, (c) retired. **Open question — ask the user before removing any reference.**

### 5.5 `developer` borrows `BaseResearchAgent` + `DomainConfig` from researcher-lib

`BaseResearchAgent` is, per the researcher-lib docstring, "a bus agent with standard research skills." Why does `developer/` import it? Two guesses worth checking: (a) developer scaffolds an embedded research sub-agent for some skill (e.g. `brief_on`, `suggest_integration_points`), (b) legacy. If (a), the import is correct but the symbol name is confusing — "developer uses a BaseResearchAgent" invites double-takes. If (b), vestige.

`DomainConfig` is researcher-specific. Same question.

`LocalDocReader` / `DocContent` are the cleaner import — reading structure-aware local docs is a general primitive, not a research-specific one. **Hoist candidate:** move to bus-lib or a new neutral doc-utils lib.

### 5.6 bus-lib registry enum surface (21 symbols) — consumer usage?

Deferred to deeper pass. If few consumers import these and they're purely documentation of routing concepts, they might belong in a `khonliang_bus.registry` submodule (already do — they're re-exported through `__init__`) with a narrower default import surface. Low priority.

### 5.7 HTTP client sprawl

Inventoried clients in use across the ecosystem:

- `httpx` — bus, bus-lib
- `aiohttp` — researcher, researcher-lib
- `githubkit` — developer, reviewer
- `openai` SDK — reviewer (for Ollama via OpenAI-compat)
- `ollama` client (via ollama-khonliang) — developer, researcher, researcher-lib

Each has a justified reason, but the mix means multiple HTTP client lifecycles per agent process. Not actionable by itself, just a map.

### 5.8 Legacy `khonliang.research.*` vs. new `khonliang_researcher.*`

Researcher imports `khonliang.research.base.BaseResearcher`, `khonliang.research.engine.BaseEngine`, `khonliang.research.models.ResearchTask/ResearchResult`, `khonliang.research.pool.ModelPool`. Researcher-lib (new) defines `BaseResearchAgent`, `BaseSearchEngine`, `EngineRegistry`, etc. Two research frameworks in one process. Is the old one still carrying weight, or is researcher using both transitionally? **Deep-scan target.**

### 5.9 `ollama-khonliang` = `khonliang` lives outside `/mnt/dev/ttoll/dev/`

At `/mnt/dev/ttoll/ollama-khonliang/`, symlinked into `dev/`. Plus a `kh17` checkout at `/mnt/dev/ttoll/ollama-khonliang-kh17/` and a `/tmp/ollama-khonliang/` copy — likely a build-temp and/or an abandoned checkout. Confirm before trusting the in-tree sources as canonical.

### 5.10 Librarian + Store agents — slated additions

From memory (`project_librarian_scoping.md`, `project_store_agent_inflight.md`):

- **Librarian agent** — split from researcher: researcher keeps ingest/distill, librarian owns corpus+evaluate+coordinate. Primitives already landed in `khonliang_researcher.librarian` (`LibrarianStore`, `classify_paper_from_triples`, `identify_gap_candidates`, etc.), but no standalone agent repo yet.
- **Store agent** — expected to own the artifact surface currently served by `bus_artifact_*` MCP tools on the bus itself.

Both affect the proposed architecture. Librarian is arguably already half-implemented in researcher-lib; formalizing the split means a new agent repo + possibly extracting `LibrarianStore` into its own lib (or a librarian-lib).

---

## §6 Capability catalog (deferred)

The full MCP tool catalogue per agent (deferred from this pass). Sketch from the deferred-tool list surfaced in-session:

- **bus:** 19 tools (artifact_*, flows, services, skills, matrix, trace, status, start/stop/restart, wait_for_event, feedback, refresh_skills)
- **developer:** ~70 tools — FR lifecycle, milestones, specs, bugs, dogfood, git_*, PR fleet/watchers, repo hygiene, session checkpoints, work units, tests
- **researcher:** ~55 tools — ingest, fetch_paper(s), distill, knowledge search/ingest, concept/entity graphs, synthesize, evaluate_capability, brief_on/brief_idea, register_evidence_source, catalog, coding/research guides
- **librarian-primary:** 7 tools — classify_paper, identify_gaps, library_health, promote_investigation, rebuild_neighborhoods, suggest_missing_nodes, taxonomy_report (live as a sub-agent of researcher today)
- **reviewer:** 5 tools — review_diff, review_pr, review_text, usage_summary, health_check

Flat list in a follow-up pass. The valuable output there is **"skill X on agent Y could also be useful for agent Z"** — that's the functional-pass the user asked for.

---

## §7 First-draft observations for the proposed architecture

Not FRs yet — triage candidates for `proposed.dot`.

1. **Retire stale bus-lib CLAUDE.md diagram.** Refresh to include reviewer/reviewer-lib. (trivial doc update)
2. **Resolve `khonliang-scheduler` symlink.** Check-out-here, relocate, or delete. Propagate result to bus-lib diagram. (needs user decision)
3. **Finish the concept→entity / project→target rename in researcher.** Update `researcher/graph.py`, `researcher/cli.py`, `researcher/server.py`, `researcher/synthesizer.py`, `researcher/librarian_agent.py` to the new names. Drop five backward-compat aliases from `khonliang_researcher/__init__.py`. (low-risk mechanical)
4. **Deep-scan the researcher/ vs. researcher-lib/ module duplicates.** Confirm which `researcher/*.py` files are pre-extraction fossils vs. live adapters. Proposed outcome: either delete or document (§5.1). (high-value vestige hunt)
5. **Evaluate `BaseResearchAgent` / `DomainConfig` usage in developer.** Decide between "developer legitimately runs a sub-research-agent" (keep, rename for clarity) and "legacy, rewrite without it" (delete). (§5.5)
6. **Hoist `LocalDocReader` / `DocContent`.** Move from `khonliang_researcher.doc_reader` to either bus-lib or a neutral doc-utils lib. It's agent-general, used by developer. (small, clean move)
7. **Evaluate legacy `khonliang.research.*` usage in researcher/.** If `BaseResearcher` / `BaseEngine` / `ResearchTask` are now shadowed by `khonliang_researcher` equivalents, deprecate and migrate. (§5.8 — larger)
8. **Decide boundary: what in `ollama-khonliang` belongs in the newer ecosystem vs. stays.** `khonliang.mcp` compact helpers are general-purpose (used by researcher); candidates for bus-lib. `KnowledgeStore` / `TripleStore` look like researcher-specific persistence — may belong in researcher-lib. `ModelPool`, `Blackboard`, consensus/debate mechanisms are framework-scale and have no replacement in the bus ecosystem. **Biggest open architectural question in this review.**
9. **Formalize librarian repo.** Split `khonliang_researcher.librarian.*` into its own agent + (possibly) librarian-lib. (per memory, already scoping)
10. **Add store agent.** Extract `bus_artifact_*` MCP tools into a standalone store agent. (per memory)

---

## §8 What this pass did not cover

- Module-body deep diff between suspected duplicates (§5.1). Important, deferred.
- Cross-repo utility dedup — defensive int coercion, compact-JSON serialisation, HTTP retry scaffolding, status-transition enums. Need a symbol-level grep pass.
- Full MCP tool catalogue (§6).
- Tests and dev-deps — only `[project].dependencies` considered.
- `khonliang-bus-wt/` + other worktree roots — assumed to mirror mainline repos.
- CI and deployment topology — out of scope for code-location review.
- Cross-reference with the true-north charter (`project_true_north_charter.md`) — should verify the proposed shape aligns with the 3-tier split there before filing FRs.

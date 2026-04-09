# MS-01 Review

**Reviewer:** Claude (researcher session)
**Spec:** `specs/MS-01/spec.md`
**Date:** 2026-04-09
**Status:** draft → needs revision

## TL;DR

Solid scope and tight design. Two categories of feedback:

1. **Technical correctness** — five issues where the spec references APIs or behaviors that don't quite match what's actually in the code. All fixable without changing the shape of MS-01.
2. **Architectural** — the shared-SQLite contract between researcher and developer is convenient but conceptually leaky. Worth pushing back on now, before code lands.

The architectural item is the bigger one. Recommend addressing it in spec revision before starting MS-01 implementation.

---

## Part 1 — Technical issues against the actual code

### 1. WAL mode is NOT enabled — this is a hard prerequisite, not an open question

**Spec location:** §Open questions #1, §Acceptance #9

**Reality checked against `khonliang.knowledge.store.KnowledgeStore`:**
- `__init__` runs `_ensure_schema()` and that's it
- The only `PRAGMA` call in the entire module is `PRAGMA table_info()` (a query)
- WAL mode is not enabled anywhere

**Impact:** Without WAL, two processes holding handles to `researcher.db` *will* hit `database is locked` errors as soon as researcher's distillation worker tries to write while developer is reading. SQLite defaults to rollback journal mode which serializes ALL access — not just writers.

**Two ways forward:**

- **A.** Add a small DB-init helper in MS-01 that does `PRAGMA journal_mode=WAL;` on the connection before any reads. Cheap, scoped to developer's startup. ~5 lines.
- **B.** Promote a tiny precondition FR against khonliang to enable WAL by default in `KnowledgeStore`. Cleaner long-term, blocks MS-01 on a separate PR.

**Recommendation:** (A) for MS-01, (B) as a follow-up FR. But see Part 2 — if we drop the shared DB entirely, this concern evaporates.

### 2. FR storage details — answered, but spec needs to use the actual API

**Spec location:** §SpecReader, §Open questions #2

The spec frames FR resolution as an open question. It's not — researcher already stores them concretely:

- **Tier:** `Tier.DERIVED` (not `Tier.IMPORTED`)
- **Tags:** `"fr"` plus `"target:<project>"` (e.g. `"target:developer"`); completed/archived FRs additionally tagged `"fr:completed"` / `"fr:archived"`
- **Status:** stored in `entry.metadata["fr_status"]` (`open` / `planned` / `in_progress` / `completed`)
- **Direct lookup by ID:** `knowledge.get(fr_id)` — returns the entry with content + metadata
- **Browse all:** `knowledge.get_by_tier(Tier.DERIVED)` then filter by `"fr" in (entry.tags or [])`
- **Per-target browse:** filter additionally by `f"target:{X}" in tags`
- **No special "scope":** `pipeline.search(query, scope="research")` is for *papers*, not FRs

**The spec wording "resolved against the FR store via KnowledgeStore/researcher's FR registry" should become:**

> `SpecReader` resolves `fr_*` references via direct `KnowledgeStore.get(fr_id)` lookup. Lookups that miss return `None` — the formatter shows the raw ID and a `(unresolved)` marker. For listing FRs by target, see `researcher.pipeline.ResearchPipeline.get_feature_requests` for the canonical filter logic (browse `Tier.DERIVED` + `"fr" in tags` + `"fr:archived"/"fr:completed"` exclusion).

Open question #2 can be closed.

### 3. Guides API — spec is missing the second half of the pattern

**Spec location:** §Server wiring example, §Acceptance #3

The example passes `guides={"developer_guide": "..."}` to the constructor. Researcher uses `base.add_guide("research_guide", "...")` after construction. **Both work** — they merge into the same `guide_tools` dict. So the inconsistency is cosmetic.

**But the spec is missing the second half.** `add_guide()` and the constructor `guides=` arg only register the *catalog entry* — they're a name + one-line description that shows up in `catalog(detail='brief')`. The actual markdown content has to be exposed as its own MCP tool. Researcher does this two-step:

```python
base.add_guide("research_guide", "how to discover, distill, and use research papers")
mcp = base.create_app()

_RESEARCH_GUIDE = """
# Research Pipeline Guide
...
"""

@mcp.tool()
async def research_guide() -> str:
    return _RESEARCH_GUIDE
```

The spec mentions `prompts/developer_guide.md` as a file in §Module layout but doesn't show the corresponding `@mcp.tool() async def developer_guide()` that loads and returns it. The implementer will hit this gap on day one.

**Recommendation:** Add the two-step example to §Server wiring. Either inline the guide content (researcher's pattern) or load from `prompts/developer_guide.md` at startup — both fine, but pick one and show it.

### 4. `pipeline.specs.read(path).format(detail)` — undefined API

**Spec location:** §Server wiring example, §SpecReader

The example calls `.format(detail)` on a parsed spec, but `LocalDocReader.read()` returns `DocContent` (a dataclass with `path`, `text`, `frontmatter`, `sections`, `references`). **`DocContent` has no `.format()` method.** The spec mentions a `ParsedSpec` type in §SpecReader but never defines it.

**Two options:**

- **A.** Define `ParsedSpec` as a wrapper around `DocContent` with `format(detail) -> str` returning compact/brief/full strings via `khonliang.mcp.compact.format_response`. Adds a small wrapper class to `developer/specs.py`.

- **B.** Drop `.format()` from the example. Have the tool call `format_response()` directly:

  ```python
  @mcp.tool()
  async def read_spec(path: str, detail: str = "brief") -> str:
      doc = pipeline.specs.read(path)
      return format_response(
          compact_fn=lambda: f"path={doc.path}|sections={len(doc.sections)}|refs={len(doc.references)}",
          brief_fn=lambda: ...,
          full_fn=lambda: ...,
          detail=detail,
      )
  ```

(B) is more honest about what `LocalDocReader` actually gives you and avoids inventing a new wrapper type. Either is fine — pick one and make the §SpecReader signatures concrete.

### 5. `digest_db_path: data/developer.db` is relative — same trap researcher just had

**Spec location:** §Config schema

We literally just fixed this in researcher (PR #7): a relative path resolved against cwd broke the blackboard when researcher was launched from a different directory. The root-cause fix was that `create_pipeline` resolves relative paths against the *config file's* directory, then writes the resolved value back to the config dict so any downstream consumer sees the absolute version.

**Developer's `Pipeline.from_config` will need the exact same pattern.** Worth adding to §Pipeline composition explicitly:

> `from_config()` resolves `digest_db_path`, `workspace_root`, and any other relative path field against `Path(config_path).resolve().parent`, then overwrites the config dict so any downstream consumer (stores, the LocalDocReader root, etc.) sees the absolute version.

**Bonus follow-up:** This is now the second app to need this. Worth tracking as a researcher-lib promotion: `resolve_config_paths(config: dict, config_path: str, fields: list[str]) -> dict`. Not required for MS-01 — just note it as a future FR target.

---

## Smaller items

- **§Acceptance #7 health_check** — references "Ollama version, model availability, DB path, workspace presence" but MS-01 has *no LLM calls*. Either drop the Ollama parts (they belong in MS-02 when `select_best_of_n` enters the picture) or add a comment that it's a forward-looking check. Recommend dropping them and adding back in MS-02.
- **§Scope `list_specs(project)`** — narrative hardcodes path pattern `workspace_root/<project>/specs/`. The config has `projects[X].specs_dir` which should be the source of truth. Use it.
- **§Module layout** lists `tests/fixtures/sample_spec.md` but acceptance criteria #8 doesn't reference it. Either name the fixture explicitly in #8 or drop it from the layout (since `specs/MS-01/spec.md` itself can serve as the test fixture per acceptance #4).
- **MS-01 doesn't register with khonliang-bus** — intentional per scope (bus is MS-06), but worth noting that the developer MCP comes up "invisible" to the bus until MS-06 lands. Fine for development; flag it so nobody's surprised.
- **Shared db_path is hardcoded absolute in §Config schema** — works because researcher's repo path is stable, but if researcher ever moves, both apps' configs need to update. Note in passing — see Part 2 for the deeper concern.

---

## What's right (worth keeping, regardless of architectural decision in Part 2)

- **Mirroring researcher's *server structure*** — same `KhonliangMCPServer` base, same `@mcp.tool()` registration pattern, same response convention, same config shape, same module layout, same test pattern. This is hygiene, not coupling. Keep it.
- **Non-persistent reads framing** — the cleanest way to express developer's role boundary against researcher. Specs are workspace documents, not corpus material. Keep this principle even if the storage architecture changes.
- **`LocalDocReader` reuse** instead of custom parsing — exactly why we extracted it. Keep.
- **Out-of-scope list** explicitly references which researcher-lib primitive each later milestone will pull in. Makes the whole roadmap traceable. Keep and extend.
- **`Pipeline` dataclass with `from_config`** — clean idiom. (Researcher uses a `create_pipeline()` factory function instead — both work; the dataclass is arguably nicer.)
- **Acceptance criteria #4** — parsing this very spec file is a self-validating test. Keep.
- **Five-capability roadmap** (specs → eval → FR lifecycle → worktrees → dispatch → bus) is well-shaped. Each milestone has clear inputs and outputs.

---

## Part 2 — Architectural pushback: shared storage is convenient but conceptually leaky

The bigger question: **is sharing `researcher.db` the right contract between these two apps, or is it cosmetic convenience hiding real coupling?**

The spec doubles down on tight coupling: same SQLite file, same `KnowledgeStore`, same `TripleStore`, same response convention, same Pipeline pattern, same call structure. The framing — "inverse operations on shared substrate" — is elegant as an *intuition*. As an *implementation contract*, it leaks.

### What's actually being shared

The "shared substrate" framing made sense when developer was vaporware. Now that you're about to build it, look at the real data flows:

**Researcher's data model:**
- papers (`Tier.IMPORTED`)
- distilled summaries (`Tier.DERIVED` + `summary` tag)
- triples (`TripleStore`)
- concepts + capability tags (`Tier.DERIVED` + `concept`/`capability` tags)
- relevance/embedding scores (in entry metadata)
- FRs (`Tier.DERIVED` + `fr` tag) ← **shared with developer**

**Developer's data model:**
- specs (workspace files, parsed on demand) ← **already isolated by spec design**
- milestone → spec linkage
- worktree state (path, branch, FR association, dirty/clean, conflict status)
- work bundles (which FRs grouped together for Claude)
- dispatch log (when did Claude get what work, what was the outcome)
- FR lifecycle transitions (planned → in_progress → completed, with timestamps)
- spec evaluation results (best-of-N output, evidence chain, reasoning)
- code state observations (what files changed, what's implemented vs planned)

**The actual overlap is tiny:**
- FRs (researcher writes via `synergize`, developer reads to bundle work and updates status as it progresses)
- Backing papers (developer needs paper content/summaries when evaluating specs)

That's it. Two narrow interfaces, surrounded by two completely independent data models. The current spec has them sharing *everything* to capture two columns of overlap.

### The hidden coupling cost

Sharing a SQLite file isn't just convenience — it's a contract:

1. **Schema lock-in.** Any time developer wants to add a column to track something developer-specific (worktree state, dispatch log, evaluation cache, conflict graph), it has to either pollute researcher's tables or create new tables in researcher's DB. Researcher's migrations now have to know about developer's schema. Cross-app PR coordination grows.

2. **Locking fragility, even with WAL.** SQLite WAL handles "many readers + one writer" cleanly. The moment developer needs to update FR status — and it absolutely will, since FR lifecycle management is a core developer capability — you have **two writers**. SQLite's `BEGIN IMMEDIATE` serialization is fine in theory, ugly in practice when both writers are long-running services.

3. **Operational coupling.** Backing up researcher's DB now also backs up developer's state. Restoring researcher to a known state also restores developer to that state. They have different operational lifecycles but you've welded them together. Disaster recovery gets weird.

4. **Conceptual leakage.** Developer's specs/worktrees/bundles end up in `KnowledgeStore` (which exists to model research artifacts) tagged with workarounds. Tag soup grows. Contributors have to learn researcher's mental model (`Tier.IMPORTED` vs `Tier.DERIVED`, what "summary" means as a tag, how `assessments` are stored) just to make sense of developer's data.

5. **The spec is already trying to negotiate ownership.** §Pipeline composition has to spell out "researcher owns writes to KnowledgeStore/TripleStore, developer owns writes to its own DigestStore." That sentence is the architecture screaming. The next sentence will be "...except FR status updates, which developer owns despite living in the shared store." And the sentence after that will be a comment in the code saying "DO NOT touch concept tags from developer or you'll break synergize."

### Three cleaner architectures

**A. Independent stores + MCP-to-MCP for the narrow interfaces (recommended)**

```
researcher.db                 developer.db
  papers                        specs (cached parses)
  triples                       milestones
  concepts                      worktrees
  capability tags               work_bundles
  relevance scores              dispatch_log
  FRs (authoritative)           fr_evaluations
                                fr_local_cache
                                tagged_reasoning
```

Developer has its own SQLite file, its own schema, its own lifecycle. When it needs an FR or a paper, it calls researcher's MCP server as a client. When researcher distills a new paper that affects an active spec evaluation, it publishes to khonliang-bus and developer reacts.

**Two narrow interfaces, both explicit:**
- **Sync calls (MCP-to-MCP):** `researcher.feature_requests`, `researcher.next_fr`, `researcher.update_fr_status`, `researcher.paper_context`, `researcher.knowledge_search` — all already exist
- **Async events (khonliang-bus):** `researcher.paper_distilled`, `researcher.fr_generated`, `developer.fr_status_changed`, `developer.spec_evaluated`, `developer.worktree_ready`

**B. Independent stores + dedicated FR registry**

If FRs really feel like joint property (which they kind of are — researcher writes, developer reads/updates), pull *just FRs* into a tiny third surface. Could be:
- A new `khonliang-fr-store` repo with its own MCP server
- A shared SQLite file that *only* contains FR records (small schema, no tier/scope soup)
- Or just a small HTTP API behind khonliang-bus

Researcher and developer both call into it. Everything else is local to each app. This is more infrastructure but the cleanest ownership story long-term.

**C. Stay with shared SQLite, but namespace properly**

If A and B feel like too much work for MS-01, the minimum to make the current shape sane:
- Move developer's tables into a *separate* SQLite file (`developer.db`) that lives in developer's repo
- Read researcher's data via a thin "researcher reader" interface (read-only attach is possible but adds yet another locking mode)
- **Never write to researcher's tables from developer**
- Even then, the FR status update problem remains. You'd need to delegate FR status writes to researcher via MCP-to-MCP for any cross-app mutation.

### Recommendation: Architecture A

Specifically:

1. **Developer has its own `developer.db`** with its own schema designed around development artifacts. Tables and migrations independent of researcher.

2. **FRs stay authoritative in researcher's store.** Developer treats them as remote objects fetched via MCP-to-MCP. A thin `ResearcherClient` wrapper (~50 lines) exposes `get_fr(id)`, `list_frs(target)`, `update_fr_status(id, status)`, `get_paper_context(query)`, etc. Cache results locally in `developer.db.fr_cache` for fast reads, refresh on bus events.

3. **Spec evaluations, worktree state, work bundles, dispatch logs all live in `developer.db`** with schemas designed for those concerns. No tier/scope/tag conventions inherited from researcher.

4. **Cross-app reads go through:**
   - **Sync calls:** MCP-to-MCP via the bus client SDK or a direct stdio MCP client (when developer needs an FR or paper *now*)
   - **Async events:** khonliang-bus subscriptions (`researcher.paper_distilled` → developer re-evaluates affected specs in the background)

5. **The mirror-researcher principle still applies for *server structure*** (`KhonliangMCPServer` subclass, response convention, config shape, tests pattern, `@mcp.tool()` registration) — that's hygiene, not coupling. But the *data model* is independent.

### What this changes in MS-01

The spec gets *smaller*, not bigger:

- **§Pipeline composition:** drop the shared store wiring. `Pipeline` holds developer-only stores plus a `ResearcherClient` (initially: a stub that just returns `None` for `get_fr()` and an empty list for `list_frs()`). MS-02 fills in the real client wiring.
- **§Open question #1 (WAL mode)** goes away — developer doesn't touch researcher's DB at all.
- **§Open question #2 (FR lookup)** becomes "stub `ResearcherClient.get_fr(id)` returns a placeholder for MS-01; wired up properly when MS-02 needs real FR content during spec evaluation."
- **§Acceptance #9** ("runs alongside researcher with shared DB handles") becomes "runs alongside researcher with no shared file handles; both processes are isolated; verified with both running concurrently."
- **The schema work for spec/milestone/worktree/bundle tables happens in developer's own DB**, free from researcher's tier/tag conventions.
- **§Config schema** — `db_path` becomes `developer.db` (developer's own SQLite, in `data/developer.db` resolved against config_dir per Issue #5). The shared `researcher.db` reference is removed. Add a `researcher_mcp` block describing how to reach researcher (URL or stdio command).
- **MS-06 (bus integration)** gets pulled forward conceptually — it's the natural sync mechanism — but the *implementation* can still be MS-06. MS-01 just sets up the seam where it'll plug in (the `ResearcherClient` stub is the seam).
- **Acceptance criteria** add: `developer.db` schema is independent; `ResearcherClient` is stubbed but interface-complete; no file handles are shared between developer and researcher processes.

### The honest trade-off

**You give up:** convenience of `pipeline.knowledge.get(fr_id)` returning instantly from a local SQLite read.

**You gain:** every other long-term thing.

**The cost of MCP-to-MCP for FR lookups is:**
- ~1ms per call locally (negligible)
- Slightly more code in developer (a thin `ResearcherClient` wrapper, ~50 lines)
- Need to handle "researcher MCP isn't running" gracefully (but you should anyway, and bus already has this concern)

**The cost of *not* doing this** and discovering the coupling pain in MS-04 (worktrees) or MS-06 (bus) is much higher — you'd be unwinding schema decisions across both apps, with code that depends on the shared storage already in production.

### Why now is the right moment

The spec is still in **draft**. No code has landed in `khonliang-developer` yet. Right now, switching architectures is a spec edit. Once `developer/pipeline.py` imports `KnowledgeStore` from researcher's repo and starts wiring it to the same SQLite path, the cost climbs fast.

The fix is also small: change `Pipeline` to hold its own stores + a stub `ResearcherClient`. That's it for MS-01. The hard part is the *decision*, not the *code*.

---

## Recommended changes before implementation starts

**Spec correctness (Part 1):**

1. Move open question #1 (WAL) to a hard MS-01 task — *or* obviate it entirely by adopting the architectural change (Part 2).
2. Close open question #2 with the actual storage details and lookup API documented above.
3. Pick A or B for `.format()` and make §SpecReader signatures concrete.
4. Add the `add_guide()` + `@mcp.tool() async def developer_guide()` two-step to the §Server wiring example.
5. Add the relative-path-resolution requirement to §Pipeline composition (and capture as a future researcher-lib promotion).
6. Trim the Ollama checks from `health_check` until MS-02 needs them.
7. Use `projects[X].specs_dir` from config in `list_specs`, not hardcoded `specs/`.
8. Decide on `tests/fixtures/sample_spec.md` (keep with explicit acceptance criteria, or drop and use this spec file).

**Architectural (Part 2):**

9. **Decide between shared storage and independent storage.** My recommendation: independent (Architecture A). If accepted, revise §Pipeline composition, §Config schema, §Acceptance criteria #9, and §Open questions to match. Add a small `ResearcherClient` section describing the seam.

10. If accepted, capture two follow-up FRs:
    - Researcher-lib: `resolve_config_paths()` helper (factor out the cwd-resolution pattern that both apps now need)
    - Khonliang or shared: a generic `MCPClient` base for app-to-app calls (the foundation `ResearcherClient` will subclass)

None of these change the *scope* of MS-01. Items 1–8 are correctness against actual code. Item 9 is the bigger architectural reframe — the spec gets *smaller* if we adopt it, and the result is much more durable.

---

## Suggested next step

Reach alignment on Part 2 (architecture) before revising the spec for Part 1 (correctness). If we go independent storage, several Part 1 items dissolve or change shape, so doing them in the wrong order means double work.

Once architecture is decided, the spec rewrite is mostly mechanical. I can do that pass if you'd like.

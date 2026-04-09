# MS-01 Milestone Review

**Reviewer:** Claude (researcher session)
**Milestone:** `milestones/MS-01/milestone.md`
**Spec:** `specs/MS-01/spec.md` (rev 2, approved)
**Date:** 2026-04-09
**Status:** approved with one required fix

## TL;DR

The milestone is well-structured, dependency-ordered, and tightly scoped. Tasks have concrete "done when" criteria, the critical path is marked, and the forward-looking seams are honestly explained. Spec-to-milestone coverage is 14/15 — one acceptance criterion (#9: no shared file handles) is *implicit* in the testing strategy but not *positively verified* by any task. Fix that and the milestone is ready to start.

The milestone's scope creep beyond the spec (`models` and `bus` config block placeholders) is **approved** per the user's stated principle: scope expansion beyond the spec is fine when it aligns with overall FR-level goals, as long as everything *in* the spec is reflected in the milestone. Both blocks meet that bar — MS-02 needs `models`, MS-06 needs `bus` — and including them now avoids a config migration churn later.

---

## Spec → milestone coverage check

Walked through every item in spec rev 2 §Scope, §Acceptance criteria, §Dependencies, §Open questions, §Out of scope. 14 of 15 items are explicitly covered:

| Spec item | Milestone coverage |
|---|---|
| §Scope: 8 file deliverables (`pyproject.toml`, `server.py`, `pipeline.py`, `specs.py`, `researcher_client.py`, `config.py`, `config.yaml`, `developer_guide.md`) | Tasks 1, 2, 3, 4, 5, 6, 7, 8 ✓ |
| §Scope: MCP tools (`read_spec`, `traverse_milestone`, `list_specs`, `health_check`, `developer_guide`, inherited `catalog`) | Task 6 ✓ |
| §Scope: two-step `add_guide()` + `@mcp.tool() async def developer_guide()` | Task 6 + 7 (explicit) ✓ |
| §Scope: `tests/test_specs.py` | Task 9 ✓ |
| §Acceptance #1 (file structure) | Tasks 1, 5, 6 ✓ |
| §Acceptance #2 (server boots) | Task 6 + 10 ✓ |
| §Acceptance #3 (`Config.load` resolves paths) | Task 2 + test in task 9 ✓ |
| §Acceptance #4 (`developer_guide` registered + tool) | Task 6 + 7 ✓ |
| §Acceptance #5 (`read_spec` on MS-01 spec) | Task 4 + 9 ✓ |
| §Acceptance #6 (`traverse_milestone` returns `(unresolved)`) | Task 4 + 9 ✓ |
| §Acceptance #7 (`list_specs` uses `projects[X].specs_dir`) | Task 4 + 9 ✓ |
| §Acceptance #8 (`health_check` no Ollama) | Task 6 ✓ |
| **§Acceptance #9 (no file handles shared, verified by booting both servers)** | **Gap — see Required Fix below** |
| §Acceptance #10 (`test_specs.py` passes) | Task 9 ✓ |
| §Acceptance #11 (no bus/eval/FR/worktrees, `ResearcherClient` interface-complete but stubbed) | §Out of scope + Task 3 ✓ |
| §Open questions deferred to MS-02 | §Open questions ✓ |
| §Out of scope items | §Out of scope ✓ |
| §Dependencies (no `researcher.db`, no Ollama) | Risk #1 + §Out of scope ✓ |

---

## Required fix: §Acceptance #9 isn't explicitly verified

**Spec criterion #9 says:**

> No file handles are shared between developer and researcher processes. Both can run concurrently without interfering. **Verified by booting both MCP servers and confirming developer's stores point only at `developer.db`.**

**Milestone testing strategy says:**

> No test against `researcher.db` or any researcher process. MS-01 is fully self-contained per the architectural decision in spec rev 2. Cross-app integration tests are MS-02+.

These are subtly different. The spec wants *positive verification* of isolation (boot both, confirm developer doesn't open `researcher.db`). The milestone says *no cross-app tests* — which is the right scoping decision, but doesn't verify the isolation criterion.

**Two cheap ways to close this:**

1. **Add to Task 5 (Pipeline composition):** "Verify `Pipeline.from_config` opens only `data/developer.db`. Assert that `KnowledgeStore.db_path`, `TripleStore.db_path`, and `DigestStore.db_path` all match the resolved `developer.db` path and that no other DB paths are constructed during wiring." Easy unit test, fully deterministic, runs in pytest.

2. **Add to Task 10 (Smoke validation):** "While developer MCP is running, boot researcher MCP from a separate process. Confirm both stay healthy concurrently and that `lsof -p <developer_pid> | grep '\.db$'` shows only `developer.db`." Matches the spec's literal "boot both" wording but requires manual execution.

**Recommendation:** add option 1 to Task 5 (cheap, deterministic, CI-ready) and option 2 as an optional check in Task 10 (matches the spec's literal language). Option 1 alone is sufficient to satisfy the criterion.

---

## Optional polish (non-blocking)

### 1. Catalog test for `developer_guide`

§Acceptance #4 has two halves: registered via `add_guide()` AND exposed as a tool. Task 6 covers both, but no test in Task 9 explicitly calls `catalog()` and asserts `developer_guide` is in the output. Smoke test #10 catches it implicitly. A 3-line addition to `tests/test_server.py` that calls `catalog(detail='brief')` and asserts the `developer_guide` entry would make §Acceptance #4 fully unit-testable.

### 2. researcher-lib pinning strategy in Task 1

Risk #1 correctly flags researcher-lib API drift. The fix is "Pin a specific commit/tag in `pyproject.toml`" — but the lib's primitives just landed today, so there's no tag yet. Two options:

- Add a small prerequisite step to Task 1: "Tag `khonliang-researcher-lib` (e.g., `v0.2.0`) before adding the dependency, and pin to that tag."
- Pin to the merge commit SHA directly: `khonliang-researcher-lib @ git+https://github.com/tolldog/khonliang-researcher-lib.git@<sha>`.

Either works. Worth being explicit in Task 1 about which approach is taken so the implementer doesn't have to make the call mid-task.

### 3. `bus` config block shape isn't defined

The milestone references the `bus` config block three times (Forward-looking seams table, Task 2, Task 8, Risk #5) but doesn't show what fields it has. If MS-01 truly parses and validates this block, the shape needs to exist. Suggested minimum:

```yaml
bus:
  url: http://localhost:8787
  enabled: false           # MS-06 flips this to true
```

The `enabled: false` flag makes the "parsed but no client constructed" guarantee explicit — Task 2's "validates structure" then has something concrete to validate.

The same applies to the `models` block — what fields are present? Suggested mirror of researcher's schema with all values either empty strings or `null`:

```yaml
models:
  summarizer: ""
  extractor: ""
  assessor: ""
  idea_parser: ""
  embedder: ""
  reviewer: ""
```

The validation rule for MS-01 is "block exists, keys are recognized, no values are required."

### 4. Acceptance checkboxes for Task 10

Task 10 says "verify each acceptance criterion (#1–#11) by hand and check off in this milestone doc" — but the doc has no checkboxes. Suggested addition: a final §Acceptance tracker section with one Markdown checkbox per criterion, e.g.:

```markdown
## Acceptance tracker
- [ ] #1 Repo structure complete
- [ ] #2 Server boots without error
- [ ] #3 Config.load resolves paths from non-cwd
...
```

Makes the manual smoke test produce a verifiable artifact in the doc itself.

---

## Scope creep beyond the spec — approved

The milestone introduces two config blocks (`models` and `bus`) that don't appear in spec rev 2. Per the user's stated principle:

> If the milestone aligns with overall goals, scope creep from the spec is OK as long as what is in the spec is reflected in the milestone.

Both blocks meet that bar:

- **`models`** — MS-02 needs Ollama-backed `select_best_of_n` for spec evaluation. Adding the placeholder block now avoids a config migration when MS-02 lands. Aligns with the FR's "evaluation pipeline" capability.
- **`bus`** — MS-06 needs khonliang-bus subscriptions and publishes. Adding the placeholder block now avoids a config migration when MS-06 lands. Aligns with the FR's "cross-app bus integration" capability.

Both are explicitly framed as forward-looking seams in the milestone's §Forward-looking seams table, with a clear "why now" justification per row. This is exactly the kind of decision-record that prevents scope drift while still allowing pragmatic anticipation.

**Approved as-is** for these blocks. The optional polish item #3 above (define their shapes) is a clarity improvement, not a scope concern.

---

## What's right (worth keeping)

- **Dependency-ordered tasks with critical path markers.** Tasks 1→2→5→6→10 form the spine; others can parallelize. Removes ambiguity about what blocks what.
- **"Done when" check per task.** Every task has a verifiable completion criterion. No "is this finished?" guesswork.
- **Forward-looking seams table.** Honest about what's included now vs. deferred, with "why now" justification per row. Exactly the kind of decision record that prevents drift.
- **Risk table with concrete mitigations.** Risk #1 (researcher-lib drift) is the right call to flag — the precondition primitives just merged today and revisions are likely.
- **Out-of-scope list is exhaustive** and matches the spec.
- **Merge criterion #7** (FR → `in_progress`, NOT `completed`) is the right call. MS-01 closes ~20% of `fr_developer_28a11ce2`; the FR stays open for MS-02 through MS-06.
- **Testing strategy** distinguishes unit vs. integration vs. smoke vs. no-cross-app-tests. Aligns cleanly with spec rev 2's architectural boundary.
- **Branch and PR plan** correctly lands planning docs on `main` first, then branches off for implementation. Keeps design decisions reviewable independently of code churn.
- **§Open questions** correctly defers the spec's two open questions (`ResearcherClient` transport, `developer.db` schema) to MS-02 where they belong.

---

## Recommended changes before implementation starts

**Required:**

1. Close the §Acceptance #9 verification gap. Add option 1 (Pipeline path assertion in Task 5) to make isolation positively verifiable. Optionally add option 2 (`lsof` check in Task 10) for the manual-verification record.

**Optional polish:**

2. Add a catalog assertion test for `developer_guide` in Task 9.
3. Make researcher-lib pinning strategy explicit in Task 1 (tag vs. SHA).
4. Define the shape of the `bus` and `models` config blocks so Task 2's validation has something to validate against.
5. Add a §Acceptance tracker section with checkboxes for Task 10 to mark off.

None of these change scope or shape. Required fix #1 is the only thing blocking start.

---

## Verdict

**Approved with one required fix.** Apply Required Change #1 (acceptance #9 verification), and the milestone is ready to start. Optional polish items can be addressed during implementation or skipped without consequence.

The spec ↔ milestone alignment is solid. The architectural boundary from spec rev 2 (independent storage, `ResearcherClient` seam, no shared file handles) is faithfully reflected. The forward-looking seams are documented honestly. The task breakdown is concrete and testable. Once acceptance #9 has a verification step, this is a clean execution plan for MS-01.

# MS-01 Code Review

**Reviewer:** Claude (researcher session)
**Branch:** `feat/ms-01-skeleton`
**Spec:** [`specs/MS-01/spec.md`](../../specs/MS-01/spec.md) (rev 2, approved)
**Milestone:** [`milestones/MS-01/milestone.md`](milestone.md) (rev 2, approved)
**Date:** 2026-04-09
**Status:** approved with one trivial fix

## TL;DR

**41/41 tests pass. All 11 acceptance criteria are explicitly verified by tests. Architecture A is faithfully implemented, with three-layer enforcement of store isolation (acceptance #9). Forward-looking config validation works correctly. Code is clean, well-commented, and idiomatic.**

One trivial fix needed: a `pass` where the comment promises a warning. Three optional polish items. No required changes beyond that.

This is the cleanest first-milestone implementation I've seen in this ecosystem. Ready to merge after the one fix.

---

## Acceptance criteria verification

Walked every criterion from spec rev 2 against the actual code and tests:

| # | Criterion | Verification |
|---|---|---|
| 1 | Repo structure complete | All 8 files present + `data/.gitkeep` exists ✓ |
| 2 | `python -m developer.server --config ...` boots | `tests/test_server.py::test_server_registers_developer_tools` ✓ |
| 3 | `Config.load()` resolves paths from non-cwd | `tests/test_config.py::test_load_resolves_relative_paths_against_config_dir` (uses `monkeypatch.chdir`) ✓ |
| 4 | `developer_guide` registered + tool exposes loaded markdown | `test_catalog_lists_developer_guide` + `test_developer_guide_returns_loaded_markdown` ✓ |
| 5 | `read_spec` parses MS-01 spec, returns frontmatter + sections + FR ref | `test_read_spec_returns_doc_content`, `test_summarize_extracts_bold_metadata`, `test_read_spec_finds_strict_fr_reference` ✓ |
| 6 | `traverse_milestone` returns `(unresolved)` from stub | `test_traverse_milestone_walks_back_to_specs`, `test_traverse_milestone_resolves_frs_via_stub`, `test_traverse_milestone_brief_shows_unresolved` ✓ |
| 7 | `list_specs(project='developer')` uses `projects[X].specs_dir` | `test_list_specs_discovers_via_project_config`, `test_list_specs_unknown_project_returns_empty` ✓ |
| 8 | `health_check` reports DB/workspace/researcher_mcp, **no Ollama** | `test_health_check_skips_ollama_in_ms01` (carefully checks non-db lines for `ollama`/`qwen`/`nomic`/`llama3` substrings) ✓ |
| 9 | **No file handles shared between developer and researcher** | Three-layer enforcement: (a) `Pipeline._assert_stores_isolated()` runtime check, (b) `test_stores_are_isolated`, (c) `test_stores_never_point_at_researcher_db`, (d) `test_pipeline_refuses_to_start_when_store_path_diverges` (uses monkeypatch to prove the assertion fires) ✓ |
| 10 | `tests/test_specs.py` passes against MS-01 spec as fixture | 41/41 tests pass ✓ |
| 11 | No bus/eval/FR-lifecycle/worktrees; `ResearcherClient` interface-complete but stubbed | All 4 stub methods covered by `test_researcher_client.py`, `update_fr_status` raises `NotImplementedError("MS-03")` ✓ |

**Verdict on acceptance criteria: 11/11 met, all explicitly tested.**

---

## Required fix (one trivial item)

### `developer/config.py:135-138` — comment promises a warning, code passes silently

```python
if not repo.exists() or not repo.is_dir():
    # Warn-don't-error per milestone Task 2: missing repo doesn't
    # block server startup, only ``list_specs`` for that project.
    pass
```

The comment correctly describes the intended behavior, but `pass` doesn't actually warn. The user gets no signal that a configured project's repo is missing — they'll just see empty `list_specs` output and have to debug from there.

**Fix:** add an actual log call.

```python
import logging
logger = logging.getLogger(__name__)

# ...

if not repo.exists() or not repo.is_dir():
    logger.warning(
        "projects[%r].repo does not exist: %s. list_specs(%r) will return empty.",
        name, repo, name,
    )
```

Trivial change, makes debugging much friendlier. Not blocking — but should land before merge.

---

## Optional polish (non-blocking)

### 1. Compact output convention note for future contributors

`khonliang.mcp.compact.compact_summary` escapes the field separator (`|` → `¦`) but **does not escape newlines**. Developer's current usage is safe because every value passed to `compact_summary` is single-line (titles via `_extract_title()` which strips newlines, paths, counts, FR IDs). But a future contributor adding a multi-line value (e.g., a section excerpt, an evaluation output) would silently corrupt compact output.

Suggested action: a brief comment near the first `compact_summary` call in `server.py` documenting the rule. Or, if you want it bulletproof, a small `_compact_field` helper like the one we added in researcher PR #6:

```python
def _compact_field(value, limit: int = 80) -> str:
    """Sanitize a value for inclusion in compact output (single-line, truncated)."""
    if value is None:
        return "?"
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"
```

The `_trunc()` helper already exists in `server.py:198` — extending it to also strip newlines (and renaming to `_compact_field` for clarity) would be a one-line change with future-proofing payoff.

### 2. Modern type syntax

A few spots use `Optional[FRRecord]` and `dict[str, Any]` mixed with `Optional`. With `from __future__ import annotations` (already present), you can drop `from typing import Optional` and use `FRRecord | None` consistently. Pure cosmetic; current code works fine.

### 3. `developer/__init__.py` could re-export the public surface

External callers (or the developer Claude when adding MS-02) might want:
```python
from developer import Config, Pipeline, create_developer_server
```
instead of:
```python
from developer.config import Config
from developer.pipeline import Pipeline
from developer.server import create_developer_server
```

A 5-line addition to `__init__.py`:
```python
from developer.config import Config
from developer.pipeline import Pipeline
from developer.server import create_developer_server

__all__ = ["Config", "Pipeline", "create_developer_server", "__version__"]
```

Optional — not a bug. Researcher's `__init__.py` doesn't do this either, so consistency-wise it's fine to skip.

### 4. `traverse_milestone` exception handling for spec reads in the loop

In `developer/specs.py::traverse_milestone`, the loop over `spec_paths` calls `self.summarize(str(spec_path))` and `self._reader.find_references(str(spec_path), ...)` for each linked spec. If a spec file is moved or deleted between the milestone read and the spec read (race condition), `FileNotFoundError` would propagate up and the entire traversal would fail. The MCP tool's outer `try` catches it and returns an error string, so it's not catastrophic, but a partial result (with the broken spec marked as unresolved) might be more useful.

Edge case, low priority. Skip unless you've actually hit it.

---

## Things done well (worth highlighting)

1. **Triple-layer isolation enforcement for acceptance #9.** This is exemplary defensive coding:
   - Runtime assertion in `Pipeline._assert_stores_isolated()` that refuses to start if any store points elsewhere
   - Dedicated `PipelineIsolationError` exception class with clear docstring
   - Three tests covering the happy path, the substring check, and a monkeypatched bad-path case that proves the assertion actually fires
   
   The architectural decision in spec rev 2 ("no shared file handles") is now enforced both statically (no `researcher.db` reference anywhere in the codebase) and dynamically (the runtime assertion). Belt and braces.

2. **Strict `FR_ID_PATTERN` solves a real problem.** The default `LocalDocReader` reference pattern would match python identifiers like `fr_status`, `fr_id`, `fr_lifecycle` from prose. The strict `\bfr_[\w-]+_[a-f0-9]{8}\b` pattern correctly rejects all of these and accepts the real shapes including `fr_researcher-lib_d75b118c` (note the hyphen). Tests verify both directions. This was anticipated in the spec, captured in code, and tested — good chain.

3. **`test_health_check_skips_ollama_in_ms01` is thoughtfully written.** It carefully strips the `db:` line from the search space because pytest's `tmp_path` naming might contain substrings that look like Ollama markers. Then checks for `ollama`, `qwen`, `nomic`, `llama3` substrings in the remaining lines. Defensive against false negatives, with a clear comment explaining why.

4. **Forward-looking config validation is enforced without constructing clients.** `_parse_models`, `_parse_bus`, `_parse_researcher_mcp` all validate structure but never open a connection. The `bus.enabled=true` check raises `ConfigError` with a message pointing at MS-06, making the failure mode self-documenting. Test `test_load_refuses_bus_enabled_true` proves this guarantee holds.

5. **`SpecReader.summarize` handles real-world bold-line metadata.** The two-step parse (regex extracts the bold line, then a second regex pulls the canonical FR ID out of messy values like `` `fr_developer_28a11ce2` (partial — closes ~20%...) ``) is exactly what the milestone document needs. Tests verify against the actual milestone file as fixture.

6. **`test_pipeline_refuses_to_start_when_store_path_diverges` is a great test.** Uses monkeypatch to inject a `KnowledgeStore` subclass that swaps the path under itself, then asserts `PipelineIsolationError` fires with `"knowledge"` in the message. Proves the runtime assertion isn't dead code.

7. **Two-step guide registration matches researcher exactly.** `base.add_guide("developer_guide", ...)` for the catalog entry + `@mcp.tool() async def developer_guide()` returning `pipeline.developer_guide_text` for the content. Both halves verified separately by tests.

8. **`_load_developer_guide` placeholder is defensive but doesn't mask test failures.** If the file is missing, the server boots with a clear placeholder text. The tests assert the real text content (`"Developer Pipeline Guide"`), so a missing file would fail the test loudly. Good balance.

9. **Conftest fixtures are clean and reusable.** `temp_config_file`, `loaded_config`, `pipeline` chain together via dependency injection. The `temp_config_file` factory returns a callable that accepts overrides, which makes the per-block validation tests concise. Test hygiene is excellent.

10. **`pyproject.toml` includes a console entry point.** `khonliang-developer = "developer.server:main"` means the server can be invoked as a system command after install. Small touch, professional polish.

11. **Error messages reference future milestones.** `ConfigError("'bus.enabled' must be false in MS-01; bus integration lands in MS-06")`, `NotImplementedError("ResearcherClient.update_fr_status lands in MS-03 (FR lifecycle)")`. Whoever hits these errors gets immediate context for why and when it'll be addressed. No mystery breakage.

12. **Tests use the spec/milestone files as fixtures.** `test_summarize_extracts_bold_metadata` parses `specs/MS-01/spec.md` directly. `test_traverse_milestone_walks_back_to_specs` uses `milestones/MS-01/milestone.md`. This means the spec, milestone, and test corpus stay in lockstep — if any of the three drift, the tests catch it.

---

## Spec/milestone alignment

| Spec/milestone item | Code state |
|---|---|
| Spec §Pipeline composition: own `developer.db`, `ResearcherClient` seam, `developer_guide_text` loaded at startup | `pipeline.py` faithful ✓ |
| Spec §Config schema: `db_path`, `workspace_root`, `prompts_dir`, `projects`, `models`, `bus`, `researcher_mcp` | `config.py` faithful ✓ |
| Spec §SpecReader: `read()` returns `DocContent`, `traverse_milestone` returns `MilestoneChain`, `list_specs` uses `projects[X].specs_dir` | `specs.py` faithful ✓ |
| Spec §ResearcherClient: stub, `FRRecord` dataclass, `update_fr_status` raises | `researcher_client.py` faithful ✓ |
| Spec §Server wiring: `KhonliangMCPServer` base, two-step guide registration, `format_response` directly | `server.py` faithful ✓ |
| Milestone Task 1: `pyproject.toml`, `developer/__init__.py`, `.gitignore`, `data/.gitkeep`, lib pinning strategy | All present ✓ (lib pin: `khonliang-researcher-lib` without version specifier — see note below) |
| Milestone Task 2: config layer with path resolution + forward-looking validation | `config.py` faithful ✓ |
| Milestone Task 5 isolation assertion | `Pipeline._assert_stores_isolated` ✓ |
| Milestone Task 9: 5 test files, all listed acceptance assertions | Present + comprehensive ✓ |
| Milestone §Config block shapes: `models` (6 keys), `bus` (`url`, `enabled`) | Match exactly ✓ |
| Milestone §Acceptance tracker | Present in milestone doc ✓ (still unchecked, awaiting Task 10 manual smoke test) |

**One open thread on Task 1:** the milestone says "pin to a tag if available, else commit SHA". The actual `pyproject.toml` has `"khonliang-researcher-lib"` with no version specifier or git URL — it's an unversioned dependency. This relies on the local editable install (which is what we have at `/mnt/dev/ttoll/dev/khonliang-researcher-lib`). For solo/local development that's fine, but it doesn't match the milestone's intent of pinning for stability. **Recommendation:** before opening the PR, decide whether to:
- Tag `khonliang-researcher-lib` (e.g., `v0.2.0`) and pin via `git+https://...@v0.2.0`
- Pin to the merge commit SHA (`git+https://...@<sha>`)
- Document explicitly that this repo uses local editable installs and skip the pin

Not a code bug — it's a deployment-readiness call. The current state works for solo development; the pin matters when someone else (or CI) tries to install from scratch.

---

## Test results

```
$ cd /mnt/dev/ttoll/dev/khonliang-developer && .venv/bin/python -m pytest -q
.........................................                                [100%]
41 passed in 5.71s
```

Test files and counts:
- `test_config.py` — 11 tests (path resolution, forward-looking block validation, real config file load)
- `test_specs.py` — 9 tests (spec parsing, list_specs, traverse_milestone, FR pattern hygiene)
- `test_pipeline.py` — 4 tests (isolation, guide loading, monkeypatched bad path)
- `test_researcher_client.py` — 6 tests (stub returns, NotImplementedError, no connection on construct)
- `test_server.py` — 11 tests (tool registration, catalog assertion, all 4 acceptance tools end-to-end)

41 total. Coverage hits every acceptance criterion. No test markers indicate skipped or expected-failure cases.

---

## Recommended changes before merge

**Required:**

1. Replace the `pass` in `developer/config.py:135-138` with an actual `logger.warning(...)` call. Add `import logging` + module-level `logger = logging.getLogger(__name__)` if not already present.

**Should resolve before opening the PR (non-code):**

2. Make a call on the `khonliang-researcher-lib` pinning strategy (tag, SHA, or document local-editable convention). Update `pyproject.toml` accordingly.

**Optional polish (any time, no order):**

3. Extend `_trunc` → `_compact_field` to also strip newlines, for future-proofing compact output. Current usage is safe but a future contributor might add multi-line values.
4. `developer/__init__.py` could re-export the public surface for cleaner external imports.
5. Modernize `Optional[X]` → `X | None` for consistency with `from __future__ import annotations`.
6. Consider partial-result handling in `traverse_milestone` if a linked spec disappears mid-walk. Edge case, low priority.

---

## Verdict

**Approved with one trivial required fix** (the `pass` → `logger.warning`). Once that's in, the code is ready to commit, push, and open as a PR against `main`.

This is a clean, well-tested implementation of MS-01. Architectural decisions from spec rev 2 are faithfully encoded in both runtime guards and tests. The forward-looking seams are present without leaking into MS-01 behavior. The test suite is comprehensive and would catch the kinds of regressions that matter (broken isolation, FR pattern drift, accidental Ollama coupling, accidental bus activation).

Nothing in the codebase blocks MS-02. The seams are real, the stubs are interface-complete, and the contract with researcher (via `ResearcherClient`) is clean enough to swap in a real transport without touching any caller.

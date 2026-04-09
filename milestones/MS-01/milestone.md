# MS-01: Developer MCP skeleton + spec/milestone management

**Spec:** [`specs/MS-01/spec.md`](../../specs/MS-01/spec.md) (rev 2, approved)
**FR:** `fr_developer_28a11ce2` (partial — closes ~20% of acceptance criteria; capabilities #2–#5 remain)
**Branch:** `feat/ms-01-skeleton`
**Status:** ready (rev 2 — addressing milestone review)

## Revision notes (rev 2 → addressing `review.md`)

Applied the required fix and all four optional polish items from the milestone review:

- **Required** — Acceptance #9 verification gap closed: Task 5 now includes a unit assertion that all three stores point at the resolved `developer.db` path. Task 10 adds an optional `lsof`-based runtime check for parity with the spec's literal "boot both" wording.
- **Polish #2** — Task 9 (`tests/test_server.py`) now includes a catalog assertion for `developer_guide`.
- **Polish #3** — Task 1 makes the `khonliang-researcher-lib` pinning strategy explicit (tag if available at branch-cut, else commit SHA).
- **Polish #4** — `bus` and `models` config block shapes are now defined in §Config block shapes (new section under Forward-looking seams). Task 2 and Task 8 reference these as the validation schema.
- **Polish #5** — New §Acceptance tracker section at the bottom with one checkbox per acceptance criterion. Task 10 ticks them off during smoke validation.

No scope changes. No spec changes required.

## Goal

Stand up the `khonliang-developer` MCP server with a working spec/milestone reading layer. After this milestone merges, an external Claude can call `developer.read_spec`, `developer.list_specs`, `developer.traverse_milestone`, `developer.health_check`, and `developer.developer_guide` against a real config. No LLM calls, no FR lifecycle, no worktrees, no bus — those are MS-02 through MS-06.

## Forward-looking seams included in MS-01 (per reviewer/user approval)

The user explicitly approved laying foundation for later milestones as long as it doesn't compromise MS-01 correctness. The seams below are included in MS-01 *only because* they save churn in MS-02/03/06 without adding risk now:

| Seam | Form in MS-01 | Why now |
|---|---|---|
| `ResearcherClient` | Interface-complete stub returning `None`/`[]` | Already required by spec — exercises the call path so MS-02 only fills in transport |
| `models` config block | Placeholder keys present, validated, unused | Avoids a config migration when MS-02 adds Ollama calls |
| `bus` config block | Placeholder URL, parsed but no client constructed | Avoids a config migration when MS-06 wires subscriptions |
| `data/developer.db` schema bootstrap | Empty schema initialized via `KnowledgeStore`/`TripleStore`/`DigestStore` constructors | These already create tables on first run; nothing extra needed, but verifies the file is writable from day one |

**Not included** (deferred to their proper milestones, even though "harmless"):
- `developer/evaluation.py`, `developer/fr_lifecycle.py`, `developer/worktrees.py`, `developer/dispatch.py`, `developer/bus.py` — empty placeholder files would be clutter. Each lands when its milestone needs it.
- Real `select_best_of_n` integration — even a wrapper risks pulling Ollama into MS-01's test surface.
- Actual MCP-to-MCP transport for `ResearcherClient` — the stub is the seam; transport choice is MS-02.

### Config block shapes (forward-looking, validated but inert in MS-01)

The `bus` and `models` blocks are parsed and structurally validated by `Config.load`, but no client is constructed and no LLM call is made. Defining their shapes here so Task 2's "validates structure" has something concrete to validate against.

```yaml
# MS-02 will populate these; MS-01 just validates the shape exists.
models:
  summarizer: ""
  extractor: ""
  assessor: ""
  idea_parser: ""
  embedder: ""
  reviewer: ""

# MS-06 will flip enabled=true and connect; MS-01 just validates the shape exists.
bus:
  url: http://localhost:8787
  enabled: false
```

**MS-01 validation rule:** the block must exist with the listed keys present. Values may be empty strings (for `models`) or the defaults shown above (for `bus`). The `enabled: false` flag on `bus` is the explicit guarantee that no client is constructed in MS-01 — Task 2 asserts this is `false` and refuses to construct a bus client even if it were `true`.

## Task breakdown

Tasks are ordered by dependency. Each one has a clear "done when" check. Tasks marked **[critical path]** must complete in order; others can be parallelized.

### 1. Repo skeleton **[critical path]**
- `pyproject.toml` (Python 3.11+, async, deps on `khonliang`, `khonliang-researcher-lib`, `mcp`/`fastmcp`)
- `developer/__init__.py` with `__version__`
- `.gitignore` (Python standard + `data/*.db`, `.venv/`, `__pycache__/`)
- `data/.gitkeep` so the directory exists in the repo
- **`khonliang-researcher-lib` pinning strategy:** the 3 precondition primitives just landed and no tag exists yet. Approach:
  1. Before this task starts, check if `khonliang-researcher-lib` has been tagged (e.g. `v0.2.0`). If yes, pin to the tag in `pyproject.toml`.
  2. If no tag exists, pin to the merge commit SHA that landed `LocalDocReader`/`BaseIdeaParser`/`select_best_of_n`. The implementer captures the SHA in this milestone doc when the task starts.
- **Done when:** `pip install -e .` succeeds in a fresh venv, `python -c "import developer"` works, and the dependency is pinned (tag or SHA, recorded here).

### 2. Config layer **[critical path]**
- `developer/config.py`: `Config` dataclass + `Config.load(path)` classmethod
- Resolves `db_path`, `digest_db_path`, `workspace_root`, `prompts_dir` against `Path(path).resolve().parent`
- Rewrites the resolved values back into the config dict before returning
- Validates `projects[*].repo` exists and is a directory; warns (not errors) if `specs_dir` is missing
- Parses but does **not** activate the `models`, `bus`, and `researcher_mcp` blocks (forward-looking seams). Validation against the schemas in §Config block shapes:
  - `models`: required keys present (`summarizer`, `extractor`, `assessor`, `idea_parser`, `embedder`, `reviewer`); values may be empty strings
  - `bus`: required keys present (`url`, `enabled`); `enabled` must be `false` in MS-01 (raise `ConfigError` if `true` — MS-01 will not construct a bus client)
  - `researcher_mcp`: required keys present (`transport`, `command`/`args` or `url`, `timeout`); no connection attempted
- **Done when:** `tests/test_config.py` proves load-from-non-cwd resolves correctly (acceptance #3) and that all three forward-looking blocks parse + validate without constructing clients

### 3. ResearcherClient stub
- `developer/researcher_client.py`: `FRRecord` dataclass + `ResearcherClient` class
- All methods return stub values per spec §ResearcherClient
- `update_fr_status` raises `NotImplementedError("MS-03")`
- Constructor takes `ResearcherMCPConfig` (subset of `Config`) but doesn't open any connection
- **Done when:** importable, instantiable, methods callable; test asserts stub return values

### 4. SpecReader
- `developer/specs.py`: `SpecReader`, `SpecSummary`, `MilestoneChain` types
- `read(path)` is a passthrough to `LocalDocReader.read()` returning `DocContent`
- `list_specs(project)` uses `LocalDocReader.glob_docs()` against `projects[project].specs_dir`
- `traverse_milestone(path)` walks milestone → linked specs → calls `ResearcherClient.get_fr()` for each FR ref → returns `MilestoneChain` with `(unresolved)` markers when stubbed
- **Done when:** `tests/test_specs.py` parses `specs/MS-01/spec.md`, finds `fr_developer_28a11ce2`, lists this spec via `list_specs('developer')`

### 5. Pipeline composition **[critical path]**
- `developer/pipeline.py`: `Pipeline` dataclass with `from_config()` factory
- Constructs `KnowledgeStore`, `TripleStore`, `DigestStore` against `developer.db`
- Constructs `LocalDocReader`
- Constructs `SpecReader`, `ResearcherClient`
- Loads `prompts/developer_guide.md` into `developer_guide_text`
- **Isolation assertion (acceptance #9, required by milestone review):** after wiring, assert that `pipeline.knowledge.db_path`, `pipeline.triples.db_path`, and `pipeline.digest.db_path` all equal the resolved `developer.db` path from config. Refuse to start if any store points elsewhere. This is the positive verification that no developer code path can open `researcher.db`. Covered by `tests/test_pipeline.py::test_stores_are_isolated`.
- **Done when:** `Pipeline.from_config(Config.load("config.yaml"))` returns a fully-wired pipeline; smoke test imports it; isolation assertion passes

### 6. MCP server wiring **[critical path]**
- `developer/server.py`: `create_developer_server(pipeline)` returns FastMCP instance
- Registers `developer_guide` via both `KhonliangMCPServer.add_guide()` (catalog entry) AND `@mcp.tool() async def developer_guide()` (content)
- Registers tools: `read_spec`, `traverse_milestone`, `list_specs`, `health_check`
- Each tool calls `format_response()` directly with compact/brief/full lambdas (no `.format()` on `DocContent`)
- `__main__` block parses `--config`, builds `Pipeline`, calls `create_developer_server(pipeline).run()`
- **Done when:** `python -m developer.server --config /mnt/dev/ttoll/dev/khonliang-developer/config.yaml` boots without error and stays running

### 7. Developer guide content
- `prompts/developer_guide.md`: workflow guide for external Claudes
- Mirrors researcher's `_RESEARCH_GUIDE` style (sections: Quick start, Reading specs, Traversing milestones, MS-02+ preview)
- **Done when:** loaded by `Pipeline.from_config`, returned by `developer_guide()` tool

### 8. Config file
- `config.yaml` at repo root
- Real values for `db_path`, `workspace_root`, `projects.developer`, `projects.researcher`
- Placeholder values for `models`, `bus`, `researcher_mcp` (parseable but inert)
- **Done when:** server boots from this config

### 9. Tests
- `tests/test_config.py`: load from non-cwd, verify path resolution (acceptance #3); verify `models`/`bus`/`researcher_mcp` blocks parse + validate without constructing clients
- `tests/test_specs.py`: parse this very spec file as fixture (acceptance #5, #6, #7)
- `tests/test_pipeline.py::test_stores_are_isolated`: asserts all three stores point at `developer.db` (acceptance #9, per milestone review required fix)
- `tests/test_server.py`: smoke test — boot pipeline, register tools, hit each tool with `detail='compact'`, assert non-empty responses. **Includes** a `catalog(detail='brief')` call asserting the `developer_guide` entry is present (acceptance #4 second-half verification, per milestone review polish #2).
- `tests/test_researcher_client.py`: stub verification (returns None/[], `update_fr_status` raises)
- **Done when:** `pytest` passes locally with a clean exit code

### 10. Smoke validation **[critical path]**
- Boot the server pointing at a real config
- From a separate terminal, hit it via the MCP CLI client (or python script using the MCP SDK)
- Verify each acceptance criterion (#1–#11) by hand and check off in §Acceptance tracker below
- **Optional `lsof` check (acceptance #9 belt-and-braces, per milestone review):** while developer MCP is running, also boot researcher MCP from a separate process. Confirm both stay healthy concurrently and that `lsof -p <developer_pid> | grep '\.db$'` shows only paths under `khonliang-developer/data/`. The unit-level isolation assertion in Task 5 is the primary verification; this runtime check matches the spec's literal "boot both" wording and is recommended but not blocking.
- **Done when:** all 11 acceptance criteria from `specs/MS-01/spec.md` are checked off in §Acceptance tracker

## Testing strategy

- **Unit tests** for `Config`, `SpecReader`, `ResearcherClient` stub. Each is a pure-Python class with no external state — fast, deterministic.
- **Integration test** for `Pipeline.from_config` + server registration. Boots the pipeline against a temp directory config (no real workspace needed); asserts every tool registered.
- **Smoke test** for the actual server process. Run `python -m developer.server` against the real `config.yaml`, hit each tool via stdio MCP client, assert success. Not run in CI initially (no CI yet — solo project), but documented in this milestone for the maintainer to run before merge.
- **No test against `researcher.db` or any researcher process.** MS-01 is fully self-contained per the architectural decision in spec rev 2. Cross-app integration tests are MS-02+.

## Merge criteria

1. All 11 acceptance criteria from `specs/MS-01/spec.md` are met
2. `pytest` passes
3. Manual smoke test (task #10) checked off
4. Code review complete (review.md filed and resolved)
5. PR opened against `main`, Copilot's auto-review responded to
6. PR review complete, all conversations resolved
7. FR `fr_developer_28a11ce2` status updated to `in_progress` (NOT `completed` — MS-01 only closes the skeleton, not the full FR)

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| `khonliang-researcher-lib` API drifts before merge | High — `LocalDocReader` signature change would break `SpecReader` | Pin a specific commit/tag in `pyproject.toml`. The 3 precondition primitives just landed; revisions are likely. |
| `KhonliangMCPServer.add_guide()` semantics differ from researcher's pattern | Medium — guide tool returns wrong content or fails to register | Mirror researcher exactly; if researcher's pattern works, ours will. Verified by reading `khonliang-researcher/researcher/server.py:39` during spec drafting. |
| `data/developer.db` permissions / write failures on first boot | Low — would fail loudly on startup | Smoke test catches this; `health_check` reports DB writability |
| `ResearcherClient` stub returns confuse early callers | Low — the spec's `(unresolved)` formatting handles this; tests verify | Document explicitly in `developer_guide.md` that MS-01 stubs FR resolution |
| `config.yaml` placeholder blocks (`models`, `bus`, `researcher_mcp`) accidentally activated | Medium — would attempt connections that aren't ready | Config loader explicitly does not construct clients for these blocks; only parses and validates structure. Test asserts no client connections during `Pipeline.from_config`. |

## Out of scope (explicit, to prevent scope creep)

- Any `@mcp.tool()` not in the spec's "MCP tools (initial set)" list
- Any code in `developer/evaluation.py`, `developer/fr_lifecycle.py`, `developer/worktrees.py`, `developer/dispatch.py`, `developer/bus.py` (these files don't exist in MS-01)
- Any LLM call, even "just to test the model is reachable"
- Any write to `researcher.db` from any developer code
- Any subscription to khonliang-bus or publish call
- CI/CD setup (no CI exists for this repo yet; solo workflow for now)
- README polish beyond a one-line pointer to `CLAUDE.md`

## Branch and PR plan

- Branch: `feat/ms-01-skeleton` cut from `main` (initial commit will create `main` since the repo currently has only `CLAUDE.md`)
- Initial commit on `main`: `CLAUDE.md` + `specs/MS-01/spec.md` + `specs/MS-01/review.md` + `milestones/MS-01/milestone.md` (so the planning documents are on `main` before any code branches off)
- Then `git checkout -b feat/ms-01-skeleton`
- Implementation commits land on the branch
- PR opens against `main` when tasks #1–#10 are checked off

## Open questions

None blocking. Spec's two open questions (`ResearcherClient` transport, `developer.db` schema design) are MS-02 concerns and don't affect this milestone.

## Acceptance tracker

Task 10 (smoke validation) ticks each box after manual verification against a running server. Each entry maps to its acceptance criterion in [`specs/MS-01/spec.md`](../../specs/MS-01/spec.md).

- [ ] **#1** Repo structure: `pyproject.toml`, `developer/` package (server/pipeline/specs/researcher_client/config), `tests/`, `config.yaml`, `prompts/developer_guide.md`, `data/` directory
- [ ] **#2** `python -m developer.server --config /mnt/dev/ttoll/dev/khonliang-developer/config.yaml` starts and exposes its tools
- [ ] **#3** `Config.load()` resolves all relative path fields against config-file dir (verified by `tests/test_config.py` from non-cwd)
- [ ] **#4** `developer_guide` registered via `add_guide()` AND exposed as `developer_guide()` MCP tool returning loaded markdown; catalog assertion in `tests/test_server.py`
- [ ] **#5** `read_spec` parses `specs/MS-01/spec.md`, returns frontmatter + sections + FR ref `fr_developer_28a11ce2` in brief/full/compact
- [ ] **#6** `traverse_milestone` walks milestone → specs → FRs; FR records come back `(unresolved)` because `ResearcherClient` is stubbed
- [ ] **#7** `list_specs(project='developer')` discovers `specs/MS-01/spec.md` using `projects[developer].specs_dir` (no hardcoded `specs/`)
- [ ] **#8** `health_check` reports DB path/size, `workspace_root` presence, `ResearcherClient` config validity; **no Ollama checks**
- [ ] **#9** No file handles shared between developer and researcher: stores all point at `developer.db` (Task 5 unit assertion); optionally verified by `lsof` in Task 10
- [ ] **#10** `pytest` passes — `test_config.py`, `test_specs.py`, `test_pipeline.py`, `test_server.py`, `test_researcher_client.py`
- [ ] **#11** No bus integration, no eval pipeline, no FR lifecycle, no worktrees; `ResearcherClient` is interface-complete but stubbed

# MS-01: Developer MCP skeleton + spec/milestone management

**FR:** `fr_developer_28a11ce2`
**Priority:** high
**Class:** app
**Status:** approved (rev 2)

## Revision notes (rev 2 → addressing `review.md`)

This revision applies the reviewer's recommended Architecture A (independent storage + MCP-to-MCP for narrow interfaces) and the Part 1 correctness fixes. Summary of changes for re-review:

**Architectural (Part 2):**
- **Independent storage adopted.** Developer no longer shares `researcher.db`. It runs its own `developer.db` (KnowledgeStore + TripleStore + DigestStore). FRs and paper context become *remote objects* fetched via a thin `ResearcherClient` (stubbed in MS-01, wired in MS-02).
- **`ResearcherClient` seam added** — new section under Technical Design. Stub for MS-01 returns `None`/`[]`; later milestones swap it for an MCP-to-MCP or bus-backed implementation. Mirror-researcher principle still applies for *server structure* but no longer for *data model*.
- **§Pipeline composition** rewritten — drops shared store wiring, adds `ResearcherClient` field.
- **§Config schema** rewritten — `db_path` now points at developer's own `data/developer.db`; new `researcher_mcp` block describes how to reach researcher (placeholder values for MS-01).

**Correctness (Part 1):**
- **Open question #1 (WAL)** dissolved — developer no longer touches researcher's DB. Removed.
- **Open question #2 (FR storage)** closed with the actual API documented in §ResearcherClient (`Tier.DERIVED` + `"fr"` tag + `"target:X"` tag, `entry.metadata["fr_status"]`, lookup via `KnowledgeStore.get(fr_id)`). Stays in researcher; developer wraps it.
- **Issue #3 (guides two-step)** — §Server wiring example now shows both `add_guide()` *and* the `@mcp.tool() async def developer_guide()` that returns the markdown content. The `prompts/developer_guide.md` file is loaded at startup.
- **Issue #4 (`.format()` undefined)** — adopted reviewer's option B: tools call `format_response()` directly; `SpecReader.read()` returns `DocContent` from `LocalDocReader` unchanged (no `ParsedSpec` wrapper). Signatures in §SpecReader updated accordingly.
- **Issue #5 (relative path resolution)** — added explicit requirement to §Pipeline composition: `Config.load()` resolves all relative path fields against `Path(config_path).resolve().parent` and rewrites the config dict in place. Captured the researcher-lib `resolve_config_paths()` promotion as a follow-up note in §Dependencies.
- **Smaller items:**
  - `health_check` no longer references Ollama/models in MS-01 (no LLM calls land until MS-02). Returns DB path, workspace presence, `ResearcherClient` reachability.
  - `list_specs` uses `projects[X].specs_dir` from config — no hardcoded path pattern.
  - `tests/fixtures/sample_spec.md` removed from §Module layout. Acceptance #4 already uses this very spec file as the test fixture; one fewer moving part.
  - Bus invisibility note added to §Out of scope: developer comes up "invisible" to khonliang-bus until MS-06. Explicit so nobody's surprised.

**What did *not* change:**
- Mirror-researcher *server structure*: `KhonliangMCPServer` subclass, `@mcp.tool()` registration, `create_*_server(pipeline)` factory, response convention, `Pipeline` dataclass. Hygiene, kept.
- `LocalDocReader` reuse for all spec/milestone parsing.
- Non-persistent reads framing for spec/milestone documents.
- Five-capability roadmap (MS-02 through MS-06) with researcher-lib primitive references.
- Acceptance criterion that this spec file is its own test fixture.

---

## Problem

The `khonliang-developer` repo is empty. Before any of the five capability areas in `fr_developer_28a11ce2` (spec evaluation, FR lifecycle, worktree orchestration, work dispatch, bus integration) can land, the app needs a runnable MCP server skeleton and a working spec/milestone reading layer. Without that foundation every later milestone has to invent its own structure, and Claude can't yet call any developer tool.

## Design Principle

**Mirror researcher's *server structure*, not its *data model*.** The two apps share an MCP base class, a config shape, a response convention, a tool-registration pattern, and a `Pipeline` dataclass idiom — that's hygiene. They do **not** share storage. Developer has its own `developer.db` with schemas designed for development artifacts (specs, milestones, worktrees, dispatch logs, FR lifecycle state). Researcher's data model stays intact.

**Cross-app reads go through narrow seams, not shared files.** When developer needs an FR or a paper, it asks researcher via a small `ResearcherClient` wrapper — sync calls now (stub), MCP-to-MCP or bus-backed later. Async cache-invalidation comes from khonliang-bus events in MS-06. The seam is named explicitly so the boundary can't drift.

**Lean on researcher-lib primitives.** Spec/milestone reading uses `LocalDocReader` directly. No custom file parsing.

**Non-persistent reads.** Specs and milestones are *workspace* documents, not corpus material. They are read on demand via `LocalDocReader` and never go through `KnowledgeStore.add()`. Tagged reasoning (a later milestone) writes back to the spec file itself, not to any store.

## Scope

### In scope
- Repo skeleton: `pyproject.toml`, `developer/` package, `tests/` dir, `config.yaml`, `prompts/developer_guide.md`
- `developer/server.py` — `create_developer_server(pipeline)` mirroring `researcher.server.create_research_server`. Wraps `KhonliangMCPServer.create_app()` and registers developer tools via `@mcp.tool()` decorators. Entry point: `python -m developer.server --config /absolute/path/config.yaml`
- `developer/pipeline.py` — `Pipeline` dataclass holding developer-only stores (`KnowledgeStore`, `TripleStore`, `DigestStore`, all against `developer.db`) + `LocalDocReader` instance + `ResearcherClient` instance. `from_config()` resolves relative paths against the config-file directory.
- `developer/config.py` — config dataclass + YAML loader. Same shape as `researcher/config.yaml` for shared concerns; adds `workspace_root`, `projects`, and `researcher_mcp` connection block.
- `developer/researcher_client.py` — `ResearcherClient` with stub implementations for MS-01:
  - `get_fr(fr_id) -> Optional[FRRecord]` — returns `None`
  - `list_frs(target: str) -> list[FRRecord]` — returns `[]`
  - `get_paper_context(query: str) -> str` — returns `""`
  - Real implementations land in MS-02 (when spec evaluation needs evidence) and MS-03 (when FR lifecycle needs FR records). Interface is complete in MS-01 so the seam is exercised.
- `developer/specs.py` — spec/milestone domain layer built on `LocalDocReader`:
  - `SpecReader.read(path) -> DocContent` — passthrough to `LocalDocReader.read()`. Returned `DocContent` carries `frontmatter`, `sections`, `references` (already populated by `LocalDocReader`).
  - `SpecReader.traverse_milestone(path) -> MilestoneChain` — backward walk: milestone file → linked specs → linked FRs (each FR resolved via `ResearcherClient.get_fr()`; in MS-01 those return `None` and are surfaced as `(unresolved)`). Evidence resolution lands in MS-02.
  - `SpecReader.list_specs(project) -> list[SpecSummary]` — uses `LocalDocReader.glob_docs()` against `projects[project].specs_dir`. No hardcoded path pattern.
- MCP tools (initial set):
  - `read_spec(path, detail='brief')` — calls `SpecReader.read`, formats via `khonliang.mcp.compact.format_response`
  - `traverse_milestone(path, detail='brief')` — formats `MilestoneChain`
  - `list_specs(project, detail='brief')` — formats spec summaries
  - `health_check()` — DB path + size, `workspace_root` presence, `ResearcherClient` reachability (in MS-01 this is just whether the stub is wired). **No Ollama/model checks until MS-02.**
  - `developer_guide()` — returns the markdown content of `prompts/developer_guide.md`, loaded at server startup
  - `catalog(detail=...)` — inherited from `KhonliangMCPServer`
- One `developer_guide` registered via `KhonliangMCPServer.add_guide()` for catalog discovery; the markdown is exposed via the `developer_guide` tool above
- Tests: `tests/test_specs.py` covering `specs/MS-01/spec.md` itself end-to-end (frontmatter parsing, section extraction, FR reference detection, glob discovery)

### Out of scope (later milestones)
- **MS-02** Evaluation pipeline (best-of-N spec evaluation, tagged reasoning written back to spec docs) — uses `select_best_of_n` from researcher-lib; first real `ResearcherClient` calls land here
- **MS-03** FR lifecycle management (status progression, overlap detection, bundling) — uses `BaseIdeaParser` from researcher-lib for spec/PR text decomposition; `ResearcherClient.update_fr_status()` added
- **MS-04** Worktree orchestration (git worktree per FR, conflict detection, cleanup)
- **MS-05** Work dispatch (briefing prompt generation, work bundling for Claude)
- **MS-06** Cross-app bus integration (subscribe `researcher.paper_distilled`, publish `developer.*` events, service registry). **Until MS-06, the developer MCP is invisible to khonliang-bus** — it runs as a standalone MCP process. Flagged here so nobody is surprised.
- Web dashboard, multi-agent advocate/critic, automated PR creation, non-Python language support (all explicitly out of scope per `fr_developer_28a11ce2`)

## Technical Design

### Module layout
```
khonliang-developer/
├── CLAUDE.md
├── config.yaml
├── pyproject.toml
├── developer/
│   ├── __init__.py
│   ├── server.py             # MCP entry + @mcp.tool() decorators
│   ├── pipeline.py           # Pipeline dataclass: stores + reader + researcher client
│   ├── specs.py              # SpecReader (uses LocalDocReader)
│   ├── researcher_client.py  # ResearcherClient (stub in MS-01)
│   └── config.py             # Config dataclass + YAML loader (resolves relative paths)
├── prompts/
│   └── developer_guide.md
├── data/
│   └── developer.db          # created on first run
├── specs/
│   └── MS-01/spec.md         # this file — also serves as the test fixture
└── tests/
    └── test_specs.py
```

### Server wiring
`create_developer_server(pipeline)` mirrors researcher's pattern at `khonliang-researcher/researcher/server.py:39`. Note the **two-step guide registration** (issue #3 from review):

```python
def create_developer_server(pipeline: Pipeline) -> FastMCP:
    base = KhonliangMCPServer(
        knowledge_store=pipeline.knowledge,
        triple_store=pipeline.triples,
    )
    base.add_guide(
        "developer_guide",
        "development workflow + spec/milestone management",
    )
    mcp = base.create_app()

    # Load guide content once at startup
    guide_text = pipeline.developer_guide_text  # populated in Pipeline.from_config

    @mcp.tool()
    async def developer_guide() -> str:
        return guide_text

    @mcp.tool()
    async def read_spec(path: str, detail: str = "brief") -> str:
        doc = pipeline.specs.read(path)
        return format_response(
            compact_fn=lambda: compact_summary({
                "path": doc.path,
                "sections": len(doc.sections),
                "refs": len(doc.references),
            }),
            brief_fn=lambda: _format_spec_brief(doc),
            full_fn=lambda: _format_spec_full(doc),
            detail=detail,
        )

    # ... read_spec, traverse_milestone, list_specs, health_check, ...
    return mcp
```

`__main__` block parses `--config`, builds `Pipeline` from `Config.load()`, calls `create_developer_server(pipeline).run()`.

### Pipeline composition
```python
@dataclass
class Pipeline:
    config: Config
    knowledge: KnowledgeStore       # against developer.db
    triples: TripleStore            # against developer.db
    digest: DigestStore             # against developer.db
    reader: LocalDocReader
    specs: SpecReader               # wraps reader
    researcher: ResearcherClient    # stub in MS-01
    developer_guide_text: str       # loaded from prompts/developer_guide.md at startup

    @classmethod
    def from_config(cls, config: Config) -> "Pipeline":
        ...
```

**Path resolution requirement (from review issue #5):** `Config.load(path)` MUST resolve all relative path fields (`db_path`, `workspace_root`, `prompts_dir`, etc.) against `Path(path).resolve().parent` and rewrite them in the config dict before constructing `Pipeline`. This is the same fix researcher landed in PR #7. Captured as a follow-up FR target in §Dependencies — both apps now need it, so it should be promoted to researcher-lib as `resolve_config_paths(config, config_path, fields)`.

All stores point at developer's *own* SQLite file (`data/developer.db`, resolved to absolute by `Config.load`). Developer never opens a handle to `researcher.db`. Schema migrations are owned entirely by developer.

### ResearcherClient
The seam between developer and researcher. MS-01 stubs the interface so callers can already use it; MS-02+ swap in the real implementation.

```python
@dataclass
class FRRecord:
    fr_id: str
    title: str
    target: str
    priority: str
    status: str            # open | planned | in_progress | review | completed
    description: str
    metadata: dict[str, Any]


class ResearcherClient:
    """Thin wrapper exposing researcher's data as remote objects.

    MS-01: stubbed (returns None / empty). MS-02 wires real lookups
    via MCP-to-MCP or direct knowledge-store access from a sibling
    process. MS-06 adds bus-backed cache invalidation.
    """

    def __init__(self, config: ResearcherMCPConfig):
        self._config = config

    async def get_fr(self, fr_id: str) -> Optional[FRRecord]:
        return None  # MS-01 stub

    async def list_frs(self, target: str) -> list[FRRecord]:
        return []  # MS-01 stub

    async def get_paper_context(self, query: str, max_papers: int = 5) -> str:
        return ""  # MS-01 stub

    async def update_fr_status(self, fr_id: str, status: str) -> bool:
        raise NotImplementedError("MS-03")
```

**Why the stub is interface-complete in MS-01:** `SpecReader.traverse_milestone()` already calls `get_fr()`. Even though it gets `None`, the call path is exercised, the formatter handles the `(unresolved)` case, and the integration point doesn't have to be invented later — only filled in.

**Real-implementation note (for MS-02):** Per review issue #2, the actual researcher-side storage is `Tier.DERIVED` entries with `"fr"` and `"target:<name>"` tags, status in `entry.metadata["fr_status"]`, lookup via `KnowledgeStore.get(fr_id)`. See `researcher.pipeline.ResearchPipeline.get_feature_requests` for the canonical filter logic. The `ResearcherClient` real implementation can either:
1. Call researcher's MCP tools as a client (`feature_requests`, `next_fr`, `update_fr_status` already exist)
2. Open a read-only connection to researcher's DB (faster, but reintroduces the coupling concern — option 1 preferred)

Decision deferred to MS-02.

### SpecReader
```python
class SpecReader:
    def __init__(
        self,
        reader: LocalDocReader,
        workspace_root: Path,
        projects: dict[str, ProjectConfig],
        researcher: ResearcherClient,
    ):
        self._reader = reader
        self._root = workspace_root
        self._projects = projects
        self._researcher = researcher

    def read(self, path: str) -> DocContent:
        return self._reader.read(path)

    async def traverse_milestone(self, path: str) -> MilestoneChain:
        ...

    def list_specs(self, project: str) -> list[SpecSummary]:
        proj = self._projects[project]
        pattern = f"{proj.specs_dir}/**/spec.md"
        return [self._summarize(p) for p in self._reader.glob_docs(proj.repo, pattern)]
```

`SpecReader.read()` returns `DocContent` directly — no `ParsedSpec` wrapper (review issue #4, option B). Tools call `format_response()` themselves, building compact/brief/full strings from `DocContent` fields. Keeps the type story honest about what `LocalDocReader` actually provides.

### Config schema
```yaml
# Developer-owned stores (NOT shared with researcher)
db_path: data/developer.db        # resolved against config-file dir at load time
digest_db_path: data/developer.db # same file; DigestStore is just another table set

# Workspace
workspace_root: /mnt/dev/ttoll/dev
prompts_dir: prompts

# Projects developer manages
projects:
  developer:
    repo: /mnt/dev/ttoll/dev/khonliang-developer
    specs_dir: specs
  researcher:
    repo: /mnt/dev/ttoll/dev/khonliang-researcher
    specs_dir: specs

# Researcher MCP connection (stub in MS-01, real in MS-02)
researcher_mcp:
  transport: stdio                # stdio | http
  command: python                 # used when transport=stdio
  args: ["-m", "researcher.server", "--config", "/abs/path/researcher/config.yaml"]
  url: ""                         # used when transport=http
  timeout: 30
```

Notably absent (compared to researcher's config): `models`, `model_timeouts`, `ollama_url`, `relevance_threshold`, `synergize_samples`, `predicate_aliases`. Developer doesn't call LLMs directly in MS-01 — those config keys land in MS-02.

### Response convention
All tools follow researcher's convention (per `response_modes` guide): default `compact`, support `brief` and `full`. Use `khonliang.mcp.compact.format_response`. No preamble. Token-efficient.

## Acceptance criteria

1. Repo at `/mnt/dev/ttoll/dev/khonliang-developer` contains: `pyproject.toml`, `developer/` package with `server.py`/`pipeline.py`/`specs.py`/`researcher_client.py`/`config.py`, `tests/`, `config.yaml`, `prompts/developer_guide.md`, `data/` directory
2. `python -m developer.server --config /mnt/dev/ttoll/dev/khonliang-developer/config.yaml` starts the MCP server without error and exposes its tools
3. `Config.load()` resolves `db_path`, `digest_db_path`, `workspace_root`, and `prompts_dir` against the config-file directory and rewrites them in the config dict before `Pipeline` is constructed. Verified by a test that loads the config from a non-cwd location.
4. The server registers `developer_guide` via `add_guide()` *and* exposes a `developer_guide()` MCP tool returning the loaded markdown content. Both behaviours verified.
5. `read_spec` parses this very file (`specs/MS-01/spec.md`) and returns frontmatter + section list + FR reference `fr_developer_28a11ce2` (in `brief` and `full` modes; `compact` returns key=value summary)
6. `traverse_milestone` invoked on a milestone file resolves the backward chain milestone → specs → FRs. In MS-01, FR records come back as `(unresolved)` because `ResearcherClient.get_fr()` is stubbed — the test verifies the call path is exercised and the formatter renders the unresolved marker correctly.
7. `list_specs(project='developer')` discovers `specs/MS-01/spec.md` using `projects[developer].specs_dir` from config (no hardcoded `specs/`)
8. `health_check` reports DB path + size, `workspace_root` presence, and `ResearcherClient` configuration validity (transport set, args present). **No Ollama/model checks** — those land in MS-02.
9. **No file handles are shared between developer and researcher processes.** Both can run concurrently without interfering. Verified by booting both MCP servers and confirming developer's stores point only at `developer.db`.
10. `tests/test_specs.py` passes against `specs/MS-01/spec.md` as the fixture: covers frontmatter parsing, section extraction, FR reference detection, and `list_specs` glob discovery. Path-resolution test (#3) lives in `tests/test_config.py`.
11. No bus integration, no evaluation pipeline, no FR lifecycle, no worktrees — those are MS-02 through MS-06. `ResearcherClient` is interface-complete but stubbed.

## Dependencies

- `khonliang` library (already exists) — `KhonliangMCPServer`, `KnowledgeStore`, `TripleStore`, `DigestStore`, `khonliang.mcp.compact` (`format_response`, `compact_summary`, `compact_kv`, `truncate`)
- `khonliang-researcher-lib` (already exists, 3 precondition primitives landed):
  - `LocalDocReader` — used directly in MS-01
  - `BaseIdeaParser` — not used in MS-01 (MS-03)
  - `select_best_of_n` — not used in MS-01 (MS-02)
- Python 3.11+, async throughout
- **No** dependency on `researcher.db` file. **No** dependency on Ollama in MS-01.

**Follow-up FR targets identified during this revision (capture as researcher/researcher-lib FRs, not blocking MS-01):**
- `resolve_config_paths(config: dict, config_path: str, fields: list[str]) -> dict` in researcher-lib — both researcher (PR #7) and developer now need this; promote it.
- `MCPClient` base class in khonliang for app-to-app calls — `ResearcherClient` would subclass this in MS-02; could also live as a sibling primitive in researcher-lib. Decision deferred.

## Open questions

1. **`ResearcherClient` transport for MS-02** — stdio (subprocess of researcher's MCP server) or HTTP (if researcher exposes a network MCP transport). Doesn't affect MS-01 (the stub is transport-agnostic). Decision needed before MS-02 starts.
2. **`developer.db` schema design for MS-02+** — what tables developer needs for spec evaluations, FR cache, work bundles, dispatch logs. Out of MS-01 scope, but worth a design pass before MS-02 lands so the schema doesn't grow ad-hoc.

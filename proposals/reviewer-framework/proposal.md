# Proposal (v3): Reviewer framework expansion + versioning discipline

**Status:** Ready for FR authoring (v1→15 findings, v2→8 findings, v3→addresses v2)
**Scope:** Cross-repo initiative, split into **four independent milestones**
**Author:** Claude (reviewer-dogfood session, 2026-04-21)
**Supersedes:** earlier v1 and v2 drafts (not tracked in-repo; session transcript only)
**Related PRs:** `tolldog/khonliang-bus-lib#12` — subset of Milestone A; see §Milestone-A disposition

## v2 → v3 delta

Eight v2 review findings, all addressed:

| v2 Finding | v3 resolution |
|---|---|
| [1] Cache invalidation is dead code | Dropped invalidation clause. Cache is **per-class, process-lifetime**. No invalidation semantics claimed |
| [2] `source_globs` name vs exclusion default | Renamed to structured `source_paths: {include: [...], exclude: [...]}`. Default `include=["**"]`, `exclude=["docs/**", "*.md", ".github/**", "tests/**"]` |
| [3] GH Action YAML-reading mechanism unspecified | Milestone C spec now includes a **composite action** using a Python step to parse `.reviewer/config.yaml` from the base branch |
| [4] Shallow-clone severity wrong as `nit` | Upgraded to `concern`. Infrastructure failure, not style |
| [5] `git show` + shallow fetch silent failure | Milestone C workflow **requires `fetch-depth: 0`** (or explicit `git fetch origin $BASE_SHA` before check invocation). Stated as workflow prerequisite, enforced via AC |
| [6] AC `0.2.0` literal will rot | Restated as invariant: "matches `project.version` from the on-disk `pyproject.toml` at invocation time" |
| [7] "Three-way compare" misnomer | Renamed to **cross-source comparison**. Ground truth is the fixture (one column); model outputs are rows scored against it |
| [8] Ground-truth authorship unspecified | Milestone D scope now states: **fixtures are human-authored, LLM-generated `expected_findings` are prohibited** for the seed corpus |

## Revision summary (v1 → v2)

Reviewer-primary review of v1 (claude-sonnet-4-6, 15 findings) is addressed below:

| v1 Finding | v2 resolution |
|---|---|
| [1] Scope covers 3–4 milestones | **Split into Milestones A/B/C/D**, each independently shippable |
| [2] Namespace collision with `reviewer/rules/` | Deterministic checks live in `reviewer.checks`, **not** `reviewer.rules`. Existing `reviewer/rules/` stays = routing policy |
| [3] Precedence operator ambiguous | Restated as unambiguous **override chain**: model-specific **overrides** vendor default **overrides** repo config |
| [4] `instructions.md` prompt-injection surface | Defined **trust boundary**: reviewer reads `instructions.md` from **base branch HEAD**, never from PR branch tip. Stated in Milestone B |
| [5] `resolve_version` walk needs sentinel | Walk stops at **first ancestor containing `.git/`**. Documented; prevents traversing past repo root |
| [6] Base `pyproject.toml` fetch unspecified | Added `DiffContext.base_file(path)` interface using `git show <base_sha>:<path>`. Failure modes enumerated |
| [7] Label bypass has no access control | Dropped label escape hatch. Replaced with **repo-level opt-out** in `.reviewer/config.yaml` (committed to base branch, changes require a code review) |
| [8] `tolldog/khonliang-bus-lib#12` disposition implicit | Milestone A explicit: **merge `tolldog/khonliang-bus-lib#12` first**, then Milestone-A work builds on it (pyproject-walk branch + `--version` flag are additive) |
| [9] Items 2–3 have no AC | Added AC for each milestone; Milestones B and C now have explicit acceptance |
| [10] Benchmark ground-truth corpus unscoped | Seed corpus is a **Milestone D deliverable** with stated floor: N≥20 fixtures across `version_bump` TP/FP/TN/FN quadrants |
| [11] False positive on GH Action path | Retained `tolldog/.github/.github/workflows/...` — this IS the correct org-level reusable-workflow form |
| [12] `reviewer-benchmarks/` location unspecified | Top-level `benchmarks/` directory **in `khonliang-reviewer`**. Promotion to standalone repo only if it grows its own dep tree |
| [13] No fallback when `.reviewer/` absent | Documented: **absent `.reviewer/` = use built-in defaults, no error, no deterministic rules disabled** |
| [14] Test coverage expectations missing | Each new module ships with tests covering happy path + fallback/sentinel edges. Restated in each milestone's AC |
| [15] Open Q1 security-relevant | Resolved: `instructions.md` lives in **`.reviewer/instructions.md`**. Rationale: reviewer-scoped ownership boundary, not general-purpose `CLAUDE.md`/`AGENTS.md` surface |

---

## Milestone A: Versioning primitives in `khonliang-bus-lib`

**Ships:** immediately — standalone, no dependencies on later milestones.
**Supersedes:** `tolldog/khonliang-bus-lib#12` (same resolver surface, extended with pyproject walk).

### Scope
- New `khonliang_bus.versioning` module:
  - `resolve_version(module_name=None) -> str | None` — resolution chain:
    1. If `module_name == "__main__"`, consult `sys.modules["__main__"].__spec__.name` (`tolldog/khonliang-bus-lib#12` logic).
    2. Walk up from the module's file, stopping at **first directory containing `.git/`**. Try `pyproject.toml` at each level; on hit, parse `project.version` via `tomllib`.
    3. Fall back to `importlib.metadata.version(...)` via `packages_distributions()` (today's `tolldog/khonliang-bus-lib#11` logic).
    4. Return `None` on total miss.
  - `add_version_flag(parser, module_name=None) -> None` — argparse helper wiring `--version`/`-V`. Prints resolved version + exits.
- `BaseAgent.__init__` uses `resolve_version` internally.
- Caching: per-class, process-lifetime. Once resolved for a given class, subsequent calls return the cached value; no invalidation path, since module names and pyproject locations do not change at runtime.

### AC
- `python -m reviewer.agent --version` prints a value that matches `project.version` in the on-disk `pyproject.toml` at invocation time (no pip reinstall required between the bump and the read).
- Bumping `pyproject.toml` and restarting reviewer-primary shows the new version in `bus_services` on the next heartbeat, with no other intervention.
- `resolve_version` walk terminates at `.git/`-containing ancestor; test with a nested-fake-repo fixture verifies it doesn't escape.
- `BaseAgent` behavior: all existing `test_agent.py` tests pass; a new test verifies pyproject-walk preferred over metadata when both are present with differing versions.
- `add_version_flag` in a script: `python my_script.py --version` prints and exits 0.

### Interface note
Explicit override chain still applies from `tolldog/khonliang-bus-lib#11`: subclass `version` class attribute or pre-`super()` instance assignment wins over auto-resolution. `resolve_version` is called *only* when `_has_explicit_version(self)` is False.

---

## Milestone B: `.reviewer/` directory + deterministic checks framework

**Ships after:** Milestone A.
**Depends on:** stable `resolve_version` (Milestone B's `version_bump` check reads base/head pyproject via Milestone A primitives).

### Scope

#### B.1 — `.reviewer/` directory loader

Mirrors `.claude/` layout:

```
.reviewer/
  config.yaml
  instructions.md
  models/
    <vendor>/
      <model>.yaml
      _default.yaml
  checks/           # future — repo-specific deterministic checks
  baselines/        # future — known-issue suppressions
```

- `reviewer.config.repo.load(repo_root) -> RepoConfig`
- **Override chain** (higher overrides lower): model-specific → vendor default → repo `config.yaml` → built-in defaults.
- **Scope rule (enable/disable is repo-only)**: the override chain above applies to check *parameters* (tuning knobs like `source_paths`, severity floors, thresholds). The `enabled` flag for any check is **repo-level only** and cannot be overridden by vendor or model configs. This keeps the Milestone B.3 repo-level opt-out (`checks.version_bump.enabled: false` in base-branch `.reviewer/config.yaml`) authoritative: vendor/model configs can retune a check's behavior but cannot re-enable a check the repo has turned off, nor disable a check the repo expects to run. Concretely: a `checks.<name>.enabled` key under `.reviewer/models/<vendor>/<model>.yaml` or `_default.yaml` is ignored at load time (and surfaced as a config-hygiene warning); only `.reviewer/config.yaml`'s `enabled` is read. Parameters (e.g. `checks.version_bump.source_paths`) follow the normal precedence chain.
- **Trust boundary**: all files under `.reviewer/` read from **base-branch HEAD**, never from PR branch tip. Prevents a PR from disabling checks that would flag it.
- Fallback: missing `.reviewer/` = built-in defaults, no error, no deterministic checks disabled.

#### B.2 — Deterministic checks framework

Namespace: `reviewer.checks` (not `reviewer.rules` — that's routing policy).

```python
class Check(Protocol):
    name: str
    def evaluate(self, context: CheckContext) -> list[Finding]: ...
```

`CheckContext` carries:
- `diff: str` — PR diff
- `changed_files: list[str]`
- `diff_context: DiffContext` — with `.base_file(path) -> str | None` via `git show <base_sha>:<path>`
- `repo_config: RepoConfig` — from `.reviewer/config.yaml`
- `metadata: dict` — PR number, base/head SHAs, repo name

`DiffContext` failure modes:
- Shallow clone → raise `DiffContextError`. Caller emits a single `concern`-severity finding "deterministic checks unavailable: shallow clone (base SHA unreachable)". Severity is `concern` not `nit` because an infrastructure failure silently disables all checks; surfacing prominently lets the operator fix the workflow (see Milestone C workflow prerequisites).
- File absent in base → `.base_file()` returns `None`, checks handle per-file

Checks run **before** the LLM pass; LLM prompt sees their findings and can reference them.

#### B.3 — First check: `version_bump`

- Compares base and head `pyproject.toml` via `packaging.version.parse`.
- Triggers when source files changed and version did not increment.
- "Source files" are controlled by `checks.version_bump.source_paths` in `.reviewer/config.yaml`, with the shape:
  ```yaml
  checks:
    version_bump:
      source_paths:
        include: ["**"]
        exclude: ["docs/**", "*.md", ".github/**", "tests/**"]
  ```
  A file is "source" iff it matches at least one `include` glob and no `exclude` globs. The `include`/`exclude` pair is the canonical shape used by both the reviewer check **and** Milestone C's GH Action — one interface, two implementations, no divergence.
- Severity: `concern`.
- Opt-out: `checks.version_bump.enabled: false` in **base-branch** `.reviewer/config.yaml`.

### AC
- `.reviewer/config.yaml` with `checks.version_bump.enabled: false` results in no `version_bump` findings.
- `instructions.md` under `.reviewer/` is merged into the LLM prompt when present on base branch.
- A PR branch modifying `.reviewer/instructions.md` does **not** change the reviewer's effective instructions (base-branch read).
- A PR that changes `reviewer/agent.py` without bumping `pyproject.toml` emits a `version_bump` concern finding with path `pyproject.toml` and a suggested version (patch bump).
- A PR that only touches `docs/` passes with no `version_bump` finding.
- Missing `.reviewer/` directory results in built-in-default check set running; no error logged.
- Each new module (`config.repo`, `checks.framework`, `checks.version_bump`) ships with unit tests covering happy path + at least one fallback/sentinel edge.

### Non-goals
- Custom repo-supplied checks (`.reviewer/checks/`): directory reserved, loader not implemented. Follow-up FR.
- Baseline suppressions: directory reserved, not implemented.

---

## Milestone C: GH Action reusable workflow

**Ships:** parallel with or after Milestone B. Shares no code with B; independent.

### Scope
- `tolldog/.github` (org-level `.github` repo) hosts the reusable workflow at `.github/workflows/version-bump-check.yml` **and** a supporting composite action at `.github/actions/read-reviewer-config/action.yml`.
- **Workflow prerequisites** (enforced via the reusable workflow's `checkout` step):
  - `actions/checkout@v4` with **`fetch-depth: 0`** so the base SHA is locally reachable. Without this, `git show <base_sha>:pyproject.toml` fails silently and every run would look like a shallow-clone infrastructure error (see Milestone B's `DiffContext` failure modes).
  - Alternative: explicit `git fetch --depth=1 origin $BASE_SHA` before the check step, for repos that need to keep default checkout shallow. The reusable workflow picks whichever is appropriate.
- **Config reading**: the composite action `read-reviewer-config` runs a Python step that installs two lightweight dependencies inline (`pip install 'pyyaml>=6.0,<7' 'packaging>=23.0,<25'` in the same step — stdlib is not sufficient because YAML parsing and SemVer comparison both need third-party packages; version ranges are pinned to major.minor so CI stays reproducible and the supply-chain surface is bounded). A stricter alternative is a locked `requirements.txt` committed alongside the composite action and installed via `pip install -r`; the inline form is kept for readability at the Milestone C scope and can be tightened later without a protocol change. Then the step:
  1. Reads `.reviewer/config.yaml` from the **base branch** (`git show $BASE_SHA:.reviewer/config.yaml`).
  2. Extracts `checks.version_bump.enabled` + `checks.version_bump.source_paths` into workflow outputs.
  3. Outputs fallback defaults (enabled=true, canonical include/exclude globs from Milestone B.3) when the file is absent.
- **Check step** then uses those outputs + `git show` on both pyproject files to compare versions via `packaging.version.parse`. Fails if source files (matching the extracted globs) changed and version did not increment.
- Adoption per repo: 5 lines of YAML.

```yaml
jobs:
  version-bump:
    uses: tolldog/.github/.github/workflows/version-bump-check.yml@main
```

- **No bypass label.** Repo-level opt-out via `.reviewer/config.yaml` on base branch (same switch as B.3).

### AC
- A PR in a repo using the action that changes source but not version **fails** the check.
- A PR changing only docs **passes**.
- The workflow reads and respects `.reviewer/config.yaml`'s `checks.version_bump` settings (both `enabled` flag and `source_paths.{include,exclude}`) from the **base branch** when present, and uses built-in defaults when absent.
- The workflow explicitly pins `fetch-depth: 0` (or performs a targeted `git fetch $BASE_SHA`). A CI test exercises the reusable workflow against a repo with default `fetch-depth: 1` and confirms it either corrects the fetch automatically or fails with a clear message — **never silently passes due to unreachable base SHA**.
- Workflow itself is testable — adopted by `khonliang-bus-lib` as the first consumer, with at least one fixture PR demonstrating fail + pass cases + opt-out case.

---

## Milestone D: Benchmark harness

**Ships:** after Milestone B. Consumes `.reviewer/models/` + deterministic checks framework for three-way compare.

### Scope
- Directory: `khonliang-reviewer/benchmarks/` (top-level in that repo).
- Each case: `benchmarks/cases/<name>/` with `input.diff`, `kind`, `expected_findings.yaml`.
- Runner: iterates over `.reviewer/models/<vendor>/<model>.yaml` × cases, invokes `review_text`, scores precision/recall/F1 per `(vendor, model)` against the fixture's `expected_findings.yaml`.
- **Cross-source comparison** (per saved project memory: local-first is the default path):
  - Ground truth comes from `expected_findings.yaml` (the fixture, one value per case).
  - Model outputs come from running each `(vendor, model)` against the case.
  - Score each model output against ground truth; matrix rows are `(vendor, model)`, one column per metric (precision, recall, F1, per-check and overall).
  - Ground truth is **not** a matrix row — it's the reference each row is scored against.
  - Local-LLM (Ollama) always runs; external-LLM (Claude) runs only for models configured in `.reviewer/models/anthropic/`.
- Output: markdown matrix written to `benchmarks/results/<timestamp>.md`. Checked in for drift tracking.

### Seed corpus floor
- Minimum 20 cases at first ship, distributed across:
  - `version_bump` true-positive (source changed, no bump) — at least 5
  - `version_bump` true-negative (docs-only, metadata-only) — at least 5
  - LLM-only findings (logic bugs, naming, missing tests) — at least 10
- **Ground-truth authorship**: all `expected_findings.yaml` files are **human-authored** by the maintainer (or during PR review of fixture-adding changes). **LLM-generated `expected_findings` are prohibited** for the seed corpus — measuring a model against its own output or a sibling model's output turns the benchmark into a self-consistency test rather than an accuracy test. Fixture PRs that contain LLM-generated expected findings must be rejected at review.
- Seed fixtures land with the milestone (not a future exercise).

### AC
- `python -m benchmarks.run` produces a markdown matrix with rows = `(vendor, model)` and columns = per-check precision/recall/F1.
- A new case added to `benchmarks/cases/` is picked up without runner changes.
- CI smoke test exercises the runner against a single case per vendor to prevent bit-rot.

### Non-goals
- Semantic scoring (LLM judge): note as follow-up FR. Milestone D is strict + substring only.

---

## Adoption

Each repo adopts incrementally:

1. **`khonliang-bus-lib`** — first adopter (dogfood Milestone A + B + C on itself).
2. **`khonliang-reviewer`** — second.
3. **`khonliang-developer`**, **`khonliang-researcher`**, **`khonliang-bus`** — roll out after framework stabilizes in adopters #1–2.

Minimum viable adoption per repo: `.reviewer/config.yaml` enabling `version_bump` + pointing to `pyproject.toml`, plus 5-line workflow reference.

---

## Open questions (resolved from v1)

- v1-Q1 (`instructions.md` location) → **`.reviewer/instructions.md`**. Reviewer-scoped, ownership boundary, distinct from general-purpose `CLAUDE.md`/`AGENTS.md`.
- v1-Q2 (GH Action home) → **`tolldog/.github`** (org-level `.github` repo). Keeps reusable workflows in canonical location for all `tolldog/*` repos.
- v1-Q3 (rule framework plugin format) → **Python `Check` protocol only** for now. Declarative YAML checks as follow-up FR if patterns emerge.

## Remaining open questions

- **Vendor-level `_default.yaml` merge semantics**: shallow merge (keys overlay) or deep merge (nested dicts merged recursively)? Assume shallow for MVP; document if that's wrong.
- **Severity threshold filtering**: does `.reviewer/config.yaml` carry a per-check severity floor that filters findings post-evaluation, or is severity hard-coded per check? Leaning post-evaluation filter; needs decision before B.3 ships.
- **`reviewer.checks.framework` reuse across agents**: if developer-primary or researcher-primary want to run the same framework (e.g., repo-hygiene checks), does it promote to `khonliang-bus-lib` or stay in `khonliang-reviewer`? Deferred to after Milestone B lands.

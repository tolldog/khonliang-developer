# Migration plan: ecosystem → productized scope-aware shape

Companion to `inventory.md`, `self-evaluation.md`, and the dot-graph artifacts.
Turns the architectural direction into a sequenced set of milestones that
preserves khonliang's dogfooding throughout. Written 2026-04-24.

## Outcome

The ecosystem is usable by any single developer against any single project,
with clean scope categories (per-user-singleton / per-domain / per-project)
and the 3-rule framework (work-semantics vs. transport · reusability-in-scope ·
transport-only vs. LLM-augmented) codified in the data model and API surface.

## Keep-functional strategy

- **Worktree-per-PR.** Per `feedback_parallel_dev_via_worktrees`:
  `git worktree add ../<name> -b <branch> <base>`. Each milestone's PRs use
  their own worktree; main-branch checkouts stay runnable for dogfooding.
- **No parallel-fleet agents.** Single-user, single-host — running two
  developer-primary instances concurrently is unnecessary. Validate migration
  work in a worktree against a **copy of the data dir**; cut-over is a
  one-shot migrator + agent restart.
- **Reversible cut-over.** Back up `data/` before every schema migration.
  Store the pre-migration archive alongside the PR artifacts until the new
  layout has proven stable for a handful of sessions.
- **No public API breaks.** Skill signatures gain optional args (e.g. new
  `project=` params default to the active project); old callers keep working.
  Breaking changes route through supersede, not replace.

## Milestones

Four proposed. MS1 is the load-bearing foundation; MS2, MS3, MS4 layer on
top and can run in parallel once MS1 is merged.

### MS1 — Project as first-class entity (FOUNDATION)

**Deliverable:** Project records, `project_init`, data-model migration for
FR / milestone / spec / bug / dogfood. khonliang is bootstrapped as the first
project; existing workflow unchanged in practice.

**FRs bundled:**
- `fr_developer_5d0a8711` (high) — project_init + lifecycle + migration
- `fr_developer_5564b81f` (medium) — `project_ecosystem` read-only skill
  (lands against the real project records this MS introduces; falls back to
  heuristics if the record is missing)

**Cut-over checklist:**
1. Worktree: `git worktree add ../developer-project-foundation -b fr/project-foundation main`
2. Implement schema migration as a one-shot script; verify idempotence.
3. Back up `data/` to `data.backup.pre-project-foundation-<date>/`.
4. Run migrator against the backup; compare record counts + IDs.
5. Merge PR; restart developer-primary pointing at migrated layout.
6. Verify a few live skills (`list_frs_local`, `next_fr_local`, `list_bugs`)
   return identical results to pre-migration.
7. Keep backup around for ≥1 week of active dogfooding before archiving.

**Why high priority:** blocks MS2, MS3, MS4.

### MS2 — Cross-project capability

**Deliverable:** a second project can be registered and have FRs filed
against it, with cross-project deps tracked properly.

**FRs bundled:**
- `fr_developer_b053cf8b` (medium) — cross-project FR filing
  (depends on `fr_developer_5d0a8711`)

**Cut-over checklist:**
1. Worktree: `../developer-cross-project` on a branch off MS1's merge point.
2. Extend `promote_fr` + add `list_frs_by_origin` / `list_frs_by_target`.
3. Register a test second project (e.g. a sandbox clone); file a cross-project
   FR both directions to validate.
4. Merge PR.

**Unblocked by:** MS1 merged.
**Parallelizable with:** MS3, MS4.

### MS3 — Architecture-introspection skill set

**Deliverable:** the 2026-04-24 manual review workflow becomes a bus-callable
audit anyone can run on their project.

**FRs bundled:**
- `fr_developer_f92e1387` (medium) — project_* introspection skill set
  (`project_import_graph`, `project_runtime_topology`, `project_self_evaluation`,
  `project_architecture`). Depends on `fr_developer_5d0a8711`.
- Soft companion: `fr_store_d22556bb` (viewer on store) — not strictly required
  for MS3 but pairs naturally; audit output is most useful rendered in the viewer.

**Cut-over checklist:**
1. Worktree: `../developer-audit-skills`.
2. Port the four generators from `proposals/architecture-review-2026-04-24/`
   into developer as skill implementations; preserve the dot output contract.
3. Wire each skill to write its artifact via store / bus (current store
   surface).
4. Merge PR.

**Unblocked by:** MS1 merged.
**Parallelizable with:** MS2, MS4.

### MS4 — Viewer + store agent

**Deliverable:** store agent materializes (currently only exists as a
planning concept); viewer mode ships on it per the agreed design.

**FRs bundled:**
- `fr_store_d22556bb` (medium) — viewer mode on store agent. Pairs with
  a companion store-agent-skeleton FR (not yet filed; would introduce the
  store agent repo + `artifact_*` skills migrated off bus).

**Cut-over checklist:**
1. Filing: companion FR for store-agent repo + `artifact_*` skill migration
   (when ready).
2. Worktree: `khonliang-store` new repo.
3. Port `bus_artifact_*` surface from bus → store.
4. Implement `display(artifacts)` + viewer subprocess per viewer FR.
5. Deprecate `bus_artifact_*` on bus (alias → warning → removal over 2 cuts).

**Parallelizable with:** MS2, MS3.

## Parallelism map

```
     ┌──────────────────────┐
     │   MS1 — foundation   │  (must land first)
     └───────────┬──────────┘
                 │
    ┌────────────┼────────────┐
    ▼            ▼            ▼
 ┌────┐      ┌──────┐     ┌──────────┐
 │MS2 │      │ MS3  │     │   MS4    │
 │cross│     │audit │     │store+view│
 │proj │     │skills│     │          │
 └────┘      └──────┘     └──────────┘
```

Each downstream milestone gets its own worktree; they do not touch the same
modules in developer. Merge order between MS2/MS3/MS4 doesn't matter.

## Stashed from earlier review — not milestones yet

These were surfaced in the architecture review but are **not** in this
migration plan — they're independent cleanup tracks to pick up when
bandwidth allows.

- Finish concept → entity / project → target rename in researcher
  (§5.2 of inventory) — drop 5 backward-compat aliases.
- Hoist `LocalDocReader` / `DocContent` out of researcher-lib into a
  neutral home (§5.5 / §7.6 of inventory).
- Close librarian subscriber loop (`library.gap_identified` → subscriber).
- Declare missing `Collaboration` records matching grep-confirmed
  `self.request` call sites (§5 of runtime.dot narrative).
- Audit researcher skill names for "project" semantic clarity under the
  new multi-project model.
- `git_*` stays on developer under the product-agent bundling argument;
  re-evaluate only if reviewer / another agent starts doing git ops
  directly.

## What's explicitly out of scope

- **autostock migration** — framework-ready for evaluation, not scheduled.
- **Multi-user / multi-host** — separate future-debt bucket with a
  different trigger.
- **Agent-type namespacing for per-project instances** — preferred approach
  is in-agent arg routing, which MS1's migration already supports.

## Worktree setup cheatsheet

```bash
# for each milestone's implementation:
cd /mnt/dev/ttoll/dev
git -C khonliang-developer worktree add \
  ../khonliang-developer-wt/<slug> \
  -b fr/<branch> main

# work in the worktree:
cd khonliang-developer-wt/<slug>
# ... implement, commit, push ...

# after PR merges, clean up:
git -C /mnt/dev/ttoll/dev/khonliang-developer worktree remove \
  ../khonliang-developer-wt/<slug>
git -C /mnt/dev/ttoll/dev/khonliang-developer branch -d fr/<branch>
```

## Dev / run coexistence — how to keep agents running while developing

**The pyproject-git-pin trap.** Declarations like
`khonliang-researcher-lib @ git+https://…/@<SHA>` resolve against GitHub,
not local worktrees. A change in a worktree does **not** reach running
agents unless the consuming agent's venv is told to use the worktree
explicitly. Worktrees give git isolation; they do not give Python import
isolation.

**Recommended pattern: single shared dev venv + editable installs.**

```
/mnt/dev/ttoll/dev/.venv-dev/                    # one shared venv
  (editable installs of every khonliang-* repo below)

/mnt/dev/ttoll/dev/khonliang-<repo>/             # canonical main-branch checkouts
/mnt/dev/ttoll/dev/khonliang-<repo>-wt/<branch>/ # PR worktrees alongside
```

One-time bootstrap:

```bash
uv venv /mnt/dev/ttoll/dev/.venv-dev
for repo in khonliang-bus-lib khonliang-researcher-lib khonliang-reviewer-lib \
            khonliang-bus khonliang-developer khonliang-researcher \
            khonliang-reviewer ollama-khonliang; do
  uv pip install --python /mnt/dev/ttoll/dev/.venv-dev -e "./$repo"
done
```

Running agents use this venv; any edit in any canonical checkout shows up
immediately on agent restart. No reinstall needed per session.

**To test a PR locally against running agents (before merge):**

```bash
uv pip install --python /mnt/dev/ttoll/dev/.venv-dev -e \
  ./khonliang-researcher-lib-wt/<branch>
# restart affected agents; exercise feature
# after PR merges, swap back to canonical:
uv pip install --python /mnt/dev/ttoll/dev/.venv-dev -e \
  ./khonliang-researcher-lib
```

**For MS1 (data-model migration) specifically.** This is the one where
"keep running" matters most because cut-over touches live data:

1. Create worktree but **do not** install it over the dev venv yet.
2. Run migrator against a **copy** of `data/` at `/tmp/migration-test/`.
   Verify schema + record counts + sampled IDs match.
3. Only when tests pass: stop developer-primary → install worktree editable
   over dev venv → back up live `data/` → run migrator against live data →
   restart developer-primary.
4. Rollback path: install canonical back over dev venv → restore backup →
   restart.

**For MS2/3/4** (additive work): worktree + editable install + restart. No
data risk.

**Productization implication.** This bootstrap is exactly what a new
contributor needs to do to get started. Bundle into a `CONTRIBUTING.md` +
`bootstrap-dev.sh` when productization activates. Candidate FR at that
point; not in scope for the initial migration.

### Parallel bus-dev for PR exercise without disrupting dogfooding

The shared-venv + editable-install pattern above solves "how do my edits
reach the interpreter." It does **not** solve "how do I exercise a PR
without disrupting the live canonical agents on bus-prod." For that,
run a **second bus instance** on a different port and register the
worktree agents there.

```
bus-prod (canonical):
  bind      localhost:8788
  data      /mnt/dev/ttoll/dev/khonliang-bus/data/
  config    config.yaml
  agents    developer-primary, researcher-primary,
            librarian-primary, reviewer-primary  (from canonical checkouts)
  ← Claude Code MCP points here; dogfooding flows uninterrupted

bus-dev (disposable, per-PR or per-session):
  bind      localhost:8789
  data      /tmp/bus-dev-<run>/data/
  config    config-dev.yaml
  agents    developer-dev, researcher-dev, …  (from worktree checkouts)
  ← exercise PR via curl / ad-hoc MCP / second Claude Code instance
```

Existing agent configurability already supports this — every agent takes
`--bus http://…` and `--config …`, so pointing at `bus-dev` is an argv
choice, no code change.

**Multi-PR concurrency.** Suffix agent IDs per PR and register on the same
bus-dev:

```
python -m developer --id developer-dev-fr45 --bus http://localhost:8789 ...
python -m developer --id developer-dev-fr46 --bus http://localhost:8789 ...
```

Each PR exercises its own agent_type without clobbering the others.

**Tear-down.** When a PR merges, kill bus-dev + its agents, wipe
`/tmp/bus-dev-<run>/`, done. bus-dev was disposable by design — no
migration, no rollback.

**Benefits over swap-editable-in-place:**
- bus-prod stays online; live FRs / milestones / PR watchers /
  reviewer usage tracking are never interrupted.
- PR data is isolated from canonical data; no contamination risk.
- Multiple PRs exercisable concurrently without cross-clobbering.
- Data is disposable; cleanup is `rm -rf`.

**When to swap-editable-in-place is still right:** terminal validation
just before merge, once the PR has been exercised on bus-dev and needs
one final verification against real canonical state. Otherwise, prefer
bus-dev.

### Permanent dev fleet (not disposable)

Dev is a standing environment, not a throwaway. It mirrors prod code
once (via git worktrees under `dev-mirror/`), runs continuously on
bus-dev, accumulates its own state, and is the default place to try
things before touching prod. Code flows:

- **prod → dev** (pulling prod updates into dev):
  `reset-dev-mirror.sh [--force]` — fetches `origin/main`, hard-resets
  every mirrored repo to it. Use when dev has drifted behind prod and
  you want a clean baseline.
- **dev → prod** (promoting signed-off work):
  commit/push on `dev-mirror/<repo>` (or a feature branch cut from it) →
  PR against `main` → review → merge. Prod fleet picks up the change on
  its next canonical restart.

Layout:

```
/mnt/dev/ttoll/dev/
├─ khonliang-*/                      canonical checkouts (prod fleet runs from here)
├─ khonliang-*-wt/<slug>/            per-PR worktrees (individual branches)
├─ dev-mirror/                       dev fleet's code mirror
│   ├─ khonliang-bus/                worktree of prod repo on dev-mirror/khonliang-bus branch
│   ├─ khonliang-bus-lib/
│   ├─ khonliang-developer/
│   ├─ khonliang-researcher/
│   ├─ khonliang-researcher-lib/
│   ├─ khonliang-reviewer/
│   ├─ khonliang-reviewer-lib/
│   ├─ ollama-khonliang/             (also worktree; canonical at /mnt/dev/ttoll/ollama-khonliang)
│   └─ state/                        persistent dev fleet state
│       └─ bus-dev/                  bus-dev's db + pid + log
└─ .venv-dev/                        shared dev venv; every khonliang-* editable-installed
                                     from its dev-mirror path
```

Helper scripts (in `khonliang-developer/proposals/architecture-review-2026-04-24/`):

- `setup-dev-mirror.sh` — one-time: create `dev-mirror/` worktrees on
  `dev-mirror/<name>` branches off `main`. Idempotent.
- `bootstrap-dev-venv.sh` — (re-)install `.venv-dev` editable from the
  mirror. Agent pyprojects git-pin their libs, which clobbers editable
  installs during agent-install; the script re-installs libs after
  agents to reclaim editable status. Verifies all 8 imports resolve
  against the mirror before exiting.
- `reset-dev-mirror.sh [--force]` — sync dev's mirrors to `origin/main`.
- `bus-dev-up.sh [-d]` / `bus-dev-down.sh [--wipe-data]` — bus-dev
  lifecycle on port 8789. Uses `.venv-dev` and `dev-mirror/state/bus-dev/`
  for data (persistent across restarts; `--wipe-data` is the explicit
  opt-in for nuking it).

Full dev recycle — when you want to start over without losing anything
that's already in prod:

```bash
bus-dev-down.sh --wipe-data            # stop bus-dev + wipe its state
rm -rf /mnt/dev/ttoll/dev/.venv-dev    # wipe dev venv
reset-dev-mirror.sh --force            # force all mirrors back to origin/main
bootstrap-dev-venv.sh                  # rebuild venv against mirrors
bus-dev-up.sh -d                       # start bus-dev fresh
```

Nothing in prod is touched.

## Rollback posture

- **MS1 migrator:** keep `data.backup.*` for ≥1 week; restore via agent stop
  + dir swap + agent restart if issues surface.
- **MS2/3/4:** additive — rollback is `git revert` on the feature PR, no
  data implications.
- **Cross-session breakage:** session checkpoints survive the MS1 migration
  because they live in their own store; test post-migration that resume
  still works.

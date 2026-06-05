# khonliang-developer — concern-level invariants

Repo-specific invariants distilled from real cross-vendor review history
(milestone lifecycle PR #43; async batch work PR #29/#39/#42; git-guardrails
fr_developer_44fc7dde). A local hot-tier model tends to catch
type/docstring/dead-code issues but misses these cross-cutting correctness
invariants. Flag a diff at **concern** severity when it violates one of the
patterns below. Each entry is a *canonical* bad/good pair — match by shape,
not by exact identifier.

## mutation_cache_refresh

A cached projection field must be recomputed from its *real* source on every
mutation. `milestone.draft_spec` renders from `milestone.work_unit['frs']`,
not directly from `milestone.fr_ids` — so updating `fr_ids` without syncing
`work_unit['frs']` leaves `draft_spec` stale even if `_refresh_draft_spec`
runs.

- Bad: `m.fr_ids = new_ids; self._refresh_draft_spec(m)`  *(reads stale work_unit['frs'])*
- Good: `m.fr_ids = new_ids; _sync_work_unit_frs(m.work_unit, new_ids); self._refresh_draft_spec(m)`
  — sync the real source first (mirrors `milestone_store._sync_work_unit_frs`,
  preserving per-item description/priority). (PR #43 R6)

## normalization_filter_consistency

When normalization is added at a write site (e.g. `archived → abandoned`),
every read-path filter/list/query must apply the **same** normalization;
writes and filter-matches must agree.

- Bad: write maps `archived→abandoned`, but `list(status=x)` compares `m.status == x` raw.
- Good: a shared `_normalize()` applied on both `update_status` and the `list` filter target. (PR #43 R8)

## fr_redirect_reporting

When an FR id is resolved through a redirect before use, error messages must
report **both** the original id the caller passed and the resolved id when
they differ — users debug with the id they passed.

- Bad: `raise MilestoneError(f"blocked by {fr.id}")`  *(caller passed a different id)*
- Good: `if fr.id != original: raise MilestoneError(f"bundled FR {original!r} (resolved to {fr.id!r}) is in_progress")` (PR #43 R8)

## delete_safety_checks

A delete method that iterates referenced entities to verify safety must treat
a lookup failure as a **hard error** with a status-aware recovery hint — never
silent-skip, and never add a `force=True` that bypasses the check itself (the
escape hatch is a sibling API like `update_milestone_frs` / `supersede_milestone`).

- Bad: `try: fr = fr_store.get(fr_id) except Exception: continue`  *(safety check silently skipped)*
- Good: on lookup failure, `raise MilestoneError(f"cannot delete {mid!r}: failed to verify FR {fr_id!r} ... {recovery}") from exc` (mirrors `MilestoneStore.delete`, fail-closed). (PR #43 R5/R8)

## batch_operation_per_item_error_isolation

Batch operations (ingest N papers, iterate N FRs, …) must `try/except` each
item, append failures to a `failed` list, and continue — a single transient
error must not abort the batch and lose in-flight progress.

- Bad: `for url in urls: paper = await fetch(url); results.append(...)`  *(one flaky fetch aborts all)*
- Good: per-item `try: ... except asyncio.CancelledError: raise except Exception as exc: failed.append({"item": url, "error": str(exc)})`, returning `{ingested, failed, status}`. (PR #29 R6 / #39 R4 / #42 R2)

## cancelled_error_propagation

Async code catching exceptions around cooperative work must re-raise
`asyncio.CancelledError` explicitly **before** any broader catch — an
always-apply defensive default that survives catch-width refactors and bare
`except:`.

- Bad: `try: await self._step() except: log.error(...); await asyncio.sleep(1)`  *(swallows cancellation; loop won't exit)*
- Good: `except asyncio.CancelledError: raise` then `except Exception as exc: log.exception(...)`. (PR #39 R4 / #42 R2)

## shell_git_in_worktree_trap

Any commit/push run via raw bash must be a **single** invocation that pins the
cwd absolutely (`cd /abs/path && git ...`) or use `developer.git_pr_commit_push`
(fails fast on branch mismatch). Chained `git add -A && git commit && git push`
from an ambient cwd is the trap that landed wrong-content commits direct on
`main`.

- Bad: `git add -A && git commit -m "fix PR #44" && git push`  *(ambient cwd may be on main; `-A` captures unrelated files; bypasses PR review)*
- Good: `developer.git_pr_commit_push(cwd="/abs/worktree", branch="...", paths="y.txt", set_upstream=True)` — gates every step on the declared branch matching cwd, refuses wildcards/protected branches. (fr_developer_44fc7dde / Episode 19)

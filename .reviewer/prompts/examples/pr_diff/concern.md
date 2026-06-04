# khonliang-developer — concern-level invariants

Repo-specific invariants distilled from real cross-vendor review findings
(see PR #43, milestone lifecycle). A local hot-tier model tends to catch
type/docstring/dead-code issues but misses these cross-cutting correctness
invariants. Flag a diff at **concern** severity when it violates one of the
patterns below. Each entry is a *canonical* bad/good pair — match by shape,
not by exact identifier.

## mutation_cache_refresh

When mutating `milestone.status`, `milestone.fr_ids`, or
`milestone.work_unit`, the derived `milestone.draft_spec` must be recomputed
in the **same** call. Read surfaces rely on `draft_spec` matching current
state; a stale cache silently serves the pre-mutation spec.

- Bad: `m.fr_ids = new_ids; store.save(m)`  *(draft_spec now stale)*
- Good: `m.fr_ids = new_ids; m.draft_spec = render_spec(m); store.save(m)`

## normalization_filter_consistency

When normalization is added at an input site (e.g. `archived → abandoned`
on write), every read-path filter (`list_*`, query helpers) must apply the
**same** normalization. A mismatch makes `list(status="archived")` silently
miss records stored under the synonym.

- Bad: write normalizes `archived→abandoned`, but `list(status=x)` compares `x` raw.
- Good: normalize `status` on both the write path and the list/query filter.

## fr_redirect_reporting

When reporting an FR id in an error message, if the caller-supplied `fr_id`
differs from the resolved `fr.id` (a merge redirect), include **both** so the
caller can correlate to their bundle:

- Bad: `raise ValueError(f"{resolved!r} not found")`
- Good: `raise ValueError(f"{original!r} (resolved to {resolved!r}) not found")`

## delete_safety_checks

A delete method that iterates over referenced entities must treat a lookup
failure (entity not found, store raises) as a **hard-fail**, not a
silent-skip — a skipped lookup bypasses the safety check it was guarding.

- Bad: `try: ref = store.get(id) except KeyError: continue  # skips the guard`
- Good: a missing referenced entity raises (or blocks the delete) rather than
  being swallowed, so the safety check cannot be bypassed.

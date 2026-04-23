---
kind: code_review
severity: concern
---

# Cached projection fields must be refreshed on every mutation

**Invariant**: when mutating a record field that feeds a cached projection, every upstream field the projection reads must be kept in sync, and the cached projection recomputed in the same call. Concrete case: `milestone.draft_spec` is rendered by `_draft_spec(...)` from `milestone.work_unit['frs']` — not directly from `milestone.fr_ids`. So updating `fr_ids` without also syncing `work_unit['frs']` leaves `draft_spec` rendering from stale FR entries even if `_refresh_draft_spec` is called.

**Bad pattern**:
```python
def update_fr_ids(self, milestone_id, new_ids):
    m = self._load(milestone_id)
    m.fr_ids = new_ids
    self._refresh_draft_spec(m)   # reads work_unit['frs'] — still stale
    self._save(m)
```

**Good pattern** (mirrors `developer.milestone_store._sync_work_unit_frs`):
```python
def _sync_work_unit_frs(work_unit, new_fr_ids):
    existing = work_unit.get("frs") or []
    by_id = {_fr_id_from_item(item): item for item in existing if _fr_id_from_item(item)}
    rebuilt = [by_id.get(fid, {"fr_id": fid}) for fid in new_fr_ids]
    work_unit["frs"] = rebuilt

def update_fr_ids(self, milestone_id, new_ids):
    m = self._load(milestone_id)
    m.fr_ids = new_ids
    _sync_work_unit_frs(m.work_unit, new_ids)   # sync the real source
    self._refresh_draft_spec(m)                 # now reads fresh work_unit['frs']
    self._save(m)
```

**Rationale**: cached projections silently rot when any input to the projection is missed by a mutation path. Here the dependency chain is `fr_ids → work_unit['frs'] → draft_spec`; forgetting the middle link produces a refresh that looks correct but renders stale content. Preserve per-item metadata (description, priority) across the sync so the rendered spec keeps its context. Sourced from PR #43 R6.

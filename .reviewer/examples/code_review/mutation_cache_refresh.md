---
kind: code_review
severity: concern
---

# Cached projection fields must be refreshed on every mutation

**Invariant**: when mutating a record field that feeds a cached projection (e.g. `milestone.status`, `milestone.fr_ids`, `milestone.work_unit` feeding `milestone.draft_spec`), the cached projection MUST be recomputed in the same call. Every mutation site routes through a shared refresh helper.

**Bad pattern**:
```python
def set_fr_ids(self, milestone_id, new_list):
    m = self._load(milestone_id)
    m.fr_ids = new_list
    self._save(m)  # m.draft_spec now stale
```

**Good pattern**:
```python
def set_fr_ids(self, milestone_id, new_list):
    m = self._load(milestone_id)
    m.fr_ids = new_list
    self._refresh_draft_spec(m)  # shared helper, called from every mutation
    self._save(m)
```

**Rationale**: cached projections silently rot when any single mutation site forgets to recompute them. Centralizing via `_refresh_*` helpers makes the invariant enforceable by inspection. Sourced from PR #43 R6 (milestone lifecycle).

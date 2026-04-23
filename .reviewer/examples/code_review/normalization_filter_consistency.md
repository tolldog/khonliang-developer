---
kind: code_review
severity: concern
---

# Write-side normalization requires matching read-side filter normalization

**Invariant**: when normalization is added at an input site (e.g. `archived -> abandoned` on write), audit every read-path filter / list helper / query and apply the same normalization. Writes and filter-matches must agree.

**Bad pattern**:
```python
def update_status(self, mid, status):
    if status == "archived":
        status = "abandoned"           # normalized on write
    self._save(mid, status=status)

def list(self, status=None):
    return [m for m in self._all() if m.status == status]  # no normalization
```

**Good pattern**:
```python
_ALIASES = {"archived": "abandoned"}

def _normalize(s):
    return _ALIASES.get(s, s)

def update_status(self, mid, status):
    self._save(mid, status=_normalize(status))

def list(self, status=None):
    target = _normalize(status) if status else None
    return [m for m in self._all() if target is None or m.status == target]
```

**Rationale**: asymmetric normalization causes silent filter misses for legacy rows. Sourced from PR #43 R8.

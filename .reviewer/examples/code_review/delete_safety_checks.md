---
kind: code_review
severity: concern
---

# Delete safety-check lookups must HARD FAIL, not silently skip

**Invariant**: delete methods that iterate over referenced entities to verify safety must treat lookup failures as hard errors, not silent skips. A silent-skip catch bypasses the very check that protects against unsafe deletes.

**Bad pattern**:
```python
def delete(self, milestone_id):
    m = self._load(milestone_id)
    for fr_id in m.fr_ids:
        try:
            fr = fr_store.get(fr_id)
            if fr.status == "in_progress":
                raise MilestoneError("cannot delete: active FR")
        except Exception:
            continue  # fr_store error => safety check silently skipped
    self._delete_row(milestone_id)
```

**Good pattern**:
```python
def delete(self, milestone_id, force=False):
    m = self._load(milestone_id)
    for fr_id in m.fr_ids:
        try:
            fr = fr_store.get(fr_id)
        except Exception as e:
            raise MilestoneError(
                f"safety check failed for {fr_id!r}: {e}; pass force=True to override"
            ) from e
        if fr.status == "in_progress" and not force:
            raise MilestoneError("cannot delete: active FR")
    self._delete_row(milestone_id)
```

**Rationale**: safety checks that fail-open aren't checks. Force-override must be explicit. Sourced from PR #43 R1.

---
kind: code_review
severity: concern
---

# Delete safety-check lookups must HARD FAIL with explicit recovery paths

**Invariant**: delete methods that iterate over referenced entities to verify safety must treat lookup failures as hard errors with a status-aware recovery hint. Never silently skip, and never add a `force=True` bypass for the safety check itself — the recovery path is to use a different API (`update_frs` for proposed bundles, `supersede_milestone` otherwise).

**Bad pattern**:
```python
def delete(self, milestone_id, *, fr_store):
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

**Good pattern** (mirrors `MilestoneStore.delete` — fail-closed, no force override):
```python
def delete(self, milestone_id, *, fr_store):
    m = self.get(milestone_id)
    for fr_id in m.fr_ids:
        try:
            fr = fr_store.get(fr_id)
        except Exception as exc:
            if m.status == MILESTONE_STATUS_PROPOSED:
                recovery = (
                    "Clear the FR bundle via update_milestone_frs first "
                    "if the FR is unreachable by design, then retry delete."
                )
            else:
                recovery = (
                    f"Milestone status is {m.status!r}; use "
                    "supersede_milestone instead (update_milestone_frs "
                    "only accepts 'proposed' milestones)."
                )
            raise MilestoneError(
                f"cannot delete milestone {milestone_id!r}: "
                f"failed to verify FR state for {fr_id!r} "
                f"({type(exc).__name__}: {exc}). {recovery}"
            ) from exc
        ...
```

**Rationale**: safety checks that fail-open aren't checks. A `force=True` bypass on the lookup itself would re-introduce the silent-skip footgun under a different name — the right escape hatches are the sibling APIs (`update_frs` / `supersede_milestone`), which preserve audit trails and enforce status rules. Sourced from PR #43 R5/R8.

---
kind: code_review
severity: concern
---

# Error messages must report the ORIGINAL bundled FR id, not just the resolved one

**Invariant**: when an FR id is resolved through a redirect / alias before use, error messages must show both the ORIGINAL id the caller referenced AND the resolved id if they differ. Reporting only the resolved id makes errors opaque for callers who only know the original.

**Bad pattern**:
```python
fr = fr_store.resolve(fr_id)  # may redirect fr_id -> fr.id
if fr.status == "in_progress":
    raise MilestoneError(f"blocked by {fr.id}")  # caller passed different id
```

**Good pattern**:
```python
original = fr_id
fr = fr_store.resolve(fr_id)
if fr.status == "in_progress":
    if fr.id != original:
        raise MilestoneError(
            f"bundled FR {original!r} (resolved to {fr.id!r}) is in_progress"
        )
    raise MilestoneError(f"bundled FR {original!r} is in_progress")
```

**Rationale**: users debug with the id they passed; a resolved id that doesn't match the call site forces manual redirect-chain tracing. Sourced from PR #43 R8.

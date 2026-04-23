---
kind: code_review
severity: concern
---

# Batch operations must isolate per-item failures

**Invariant**: batch operations (ingest N papers, iterate N FRs, process N comments) must `try/except` each item individually, append failures to a `failed` list, and continue. A single transient error must not abort the entire batch. In async contexts the per-item catch should re-raise `asyncio.CancelledError` first as a defensive default — see `cancelled_error_propagation.md`.

**Bad pattern**:
```python
async def ingest_batch(self, urls):
    results = []
    for url in urls:
        paper = await fetch(url)   # one flaky fetch aborts all remaining
        results.append(self._store(paper))
    return {"ingested": results}
```

**Good pattern**:
```python
async def ingest_batch(self, urls):
    ingested, failed = [], []
    for url in urls:
        try:
            paper = await fetch(url)
            ingested.append(self._store(paper))
        except asyncio.CancelledError:
            raise                  # explicit intent; defends against catch-widening
        except Exception as exc:
            failed.append({"item": url, "error": str(exc)})
    status = "completed" if not failed else ("failed" if not ingested else "partial")
    return {
        "ingested": ingested,
        "failed": failed,
        "summary": {"ok": len(ingested), "fail": len(failed)},
        "status": status,
    }
```

**Rationale**: batches are the one place where partial progress matters; aborting loses work already done. The `except CancelledError: raise` layer is a defensive default — don't tie the pattern to the exact Python-version semantics of what `except Exception:` catches. Sourced from PR #29 R6 + PR #39 R4 / PR #42 R2.

---
kind: code_review
severity: concern
---

# Batch operations must isolate per-item failures

**Invariant**: batch operations (ingest N papers, iterate N FRs, process N comments) must `try/except` each item individually, append failures to a `failed` list, and continue. A single transient error must not abort the entire batch. In async contexts the per-item catch MUST re-raise `asyncio.CancelledError` first — this pattern layers on top of the cancellation-propagation invariant (see `cancelled_error_propagation.md`); otherwise the batch loop swallows cancellation and breaks cooperative shutdown.

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
            raise                  # cooperative shutdown wins
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

**Rationale**: batches are the one place where partial progress matters; aborting loses work already done. In async code the broad catch must sit below an explicit `CancelledError` re-raise — otherwise `task.cancel()` becomes advisory and the loop refuses to exit. Sourced from PR #29 R6 (per-item isolation) + PR #39 R4 / PR #42 R2 (cancellation propagation).

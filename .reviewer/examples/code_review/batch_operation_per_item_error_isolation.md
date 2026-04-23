---
kind: code_review
severity: concern
---

# Batch operations must isolate per-item failures

**Invariant**: batch operations (ingest N papers, iterate N FRs, process N comments) must `try/except` each item individually, append failures to a `failed` list, and continue. A single transient error must not abort the entire batch.

**Bad pattern**:
```python
def ingest_batch(self, urls):
    results = []
    for url in urls:
        paper = fetch(url)         # one flaky fetch aborts all remaining
        results.append(self._store(paper))
    return {"ingested": results}
```

**Good pattern**:
```python
def ingest_batch(self, urls):
    ingested, failed = [], []
    for url in urls:
        try:
            paper = fetch(url)
            ingested.append(self._store(paper))
        except Exception as e:
            failed.append({"item": url, "error": str(e)})
    status = "completed" if not failed else ("failed" if not ingested else "partial")
    return {
        "ingested": ingested,
        "failed": failed,
        "summary": {"ok": len(ingested), "fail": len(failed)},
        "status": status,
    }
```

**Rationale**: batches are the one place where partial progress matters; aborting loses work already done. Sourced from PR #29 R6 (same pattern recurs in developer batch contexts).

---
kind: code_review
severity: concern
---

# `except Exception` around async work must re-raise `CancelledError` first

**Invariant**: any `except Exception:` block wrapping async work MUST re-raise `asyncio.CancelledError` before the broad catch. On Python 3.8+ `CancelledError` is a subclass of `Exception`; a broad catch silently converts cancellation into the error-handling path, breaking cooperative shutdown.

**Bad pattern**:
```python
async def run(self):
    while True:
        try:
            await self._step()
        except Exception as e:       # swallows CancelledError too
            log.error("step failed", error=e)
            await asyncio.sleep(1)   # loop refuses to exit on cancel
```

**Good pattern**:
```python
async def run(self):
    while True:
        try:
            await self._step()
        except asyncio.CancelledError:
            raise                    # cooperative shutdown wins
        except Exception as e:
            log.error("step failed", error=e)
            await asyncio.sleep(1)
```

**Rationale**: without the explicit `CancelledError` re-raise, `task.cancel()` becomes advisory; shutdown hangs. Sourced from PR #39 R4 and PR #42 R2.

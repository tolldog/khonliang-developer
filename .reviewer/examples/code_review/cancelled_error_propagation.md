---
kind: code_review
severity: concern
---

# Re-raise `asyncio.CancelledError` before a broad catch in async work

**Invariant**: async code catching exceptions around cooperative work should re-raise `asyncio.CancelledError` explicitly before any broader catch. The explicit re-raise documents intent, protects against bare `except:` / `except BaseException:` (which DO catch cancellation), and defends against future widening of a narrower catch. Treat this as an always-apply defensive pattern — don't rely on the Python-version / BaseException-subclass details.

**Bad pattern** (bare except swallows cancellation and everything else):
```python
async def run(self):
    while True:
        try:
            await self._step()
        except:                          # catches CancelledError + BaseException
            log.error("step failed")
            await asyncio.sleep(1)       # loop refuses to exit on cancel
```

**Good pattern** (explicit CancelledError re-raise, narrow Exception catch):
```python
async def run(self):
    while True:
        try:
            await self._step()
        except asyncio.CancelledError:
            raise                        # cooperative shutdown wins
        except Exception as exc:
            log.exception("step failed: %s", exc)
            await asyncio.sleep(1)
```

**Rationale**: the explicit `except asyncio.CancelledError: raise` is low-cost, reader-friendly, and robust across Python versions and catch-width refactors. Recommend it any time async work lives inside an `except` block — don't make the pattern conditional on the exact semantics of what `except Exception:` does or doesn't catch in a given Python release. Sourced from PR #39 R4 and PR #42 R2.

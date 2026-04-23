---
kind: code_review
severity: concern
---

# Re-raise `asyncio.CancelledError` before a broad catch in async work

**Invariant**: async code catching exceptions around cooperative work should re-raise `asyncio.CancelledError` explicitly before any broader catch. On Python 3.8+ `CancelledError` inherits from `BaseException`, so `except Exception:` does **not** catch it — but `except:` / `except BaseException:` does, and future refactors can silently widen a catch. Explicit re-raise documents intent and defends against both.

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

**Rationale**: in Python 3.11+ (this repo: `requires-python >=3.11`) `except Exception:` alone is technically safe for cancellation — `CancelledError` is a `BaseException` subclass. The explicit re-raise-first pattern is still the right default: it states intent, protects against a future widening to `BaseException`, and matches the shape a reader scanning for cancellation handling expects. Flag bare `except:` / `except BaseException:` in async code, not `except Exception:`. Sourced from PR #39 R4 and PR #42 R2; corrected for Python 3.8+ semantics in R2.

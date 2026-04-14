"""One-shot migrations for developer's store.

Scripts in this package run rare, often one-time operations (e.g. the
FR data migration from researcher.db). They're not part of the
everyday API surface — import from here deliberately, not as a
side effect of normal imports.
"""

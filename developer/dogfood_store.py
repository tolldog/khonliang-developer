"""Developer-owned dogfood (friction/UX) store.

Phase 1 of the tracking-infrastructure stack (``fr_developer_1324440c``).
CRUD-only slice: log, list, get, mark dismissed, mark duplicate. Triage
promotion (to BugStore and FRStore) is deferred to Phase 2.

Mirrors :mod:`developer.fr_store` on storage: one ``Tier.DERIVED``
``KnowledgeEntry`` per observation, tagged ``dogfood``.

**Cheap path is load-bearing.** ``log_dogfood`` must never call an LLM,
embed text, or otherwise block on external services — a lightweight local
write is the whole point. If friction capture is expensive, people stop
capturing friction.

Schema (per ``fr_developer_1324440c``):
    id            — ``dog_<8 hex of sha256>``
    observation
    kind          — friction / bug / ux / docs / other  (default friction)
    target
    context
    reporter
    observed_at   — epoch seconds
    status        — observed / triaged / promoted / dismissed / duplicate
                    (default observed)
    promoted_to   — list[str]  (populated by Phase 2)
    duplicate_of  — str  (set by :meth:`mark_duplicate`)
    source        — optional attribution struct (per ``fr_developer_47271f34``
                    forward-compat). Default ``None``.

Seed data: on first construction when zero ``dogfood``-tagged entries
exist, writes five curated observations pulled verbatim from the FR
body. Idempotent across restarts.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from khonliang.knowledge.store import (
    EntryStatus,
    KnowledgeEntry,
    KnowledgeStore,
    Tier,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DOGFOOD_KIND_FRICTION = "friction"
DOGFOOD_KIND_BUG = "bug"
DOGFOOD_KIND_UX = "ux"
DOGFOOD_KIND_DOCS = "docs"
DOGFOOD_KIND_OTHER = "other"

ALLOWED_KINDS = {
    DOGFOOD_KIND_FRICTION,
    DOGFOOD_KIND_BUG,
    DOGFOOD_KIND_UX,
    DOGFOOD_KIND_DOCS,
    DOGFOOD_KIND_OTHER,
}

DOGFOOD_STATUS_OBSERVED = "observed"
DOGFOOD_STATUS_TRIAGED = "triaged"
DOGFOOD_STATUS_PROMOTED = "promoted"
DOGFOOD_STATUS_DISMISSED = "dismissed"
DOGFOOD_STATUS_DUPLICATE = "duplicate"

ACTIVE_STATUSES = {DOGFOOD_STATUS_OBSERVED, DOGFOOD_STATUS_TRIAGED}
TERMINAL_STATUSES = {
    DOGFOOD_STATUS_PROMOTED,
    DOGFOOD_STATUS_DISMISSED,
    DOGFOOD_STATUS_DUPLICATE,
}
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Domain object
# ---------------------------------------------------------------------------


@dataclass
class Dogfood:
    """A dogfood observation as read out of the store."""

    id: str
    observation: str
    kind: str
    target: str
    context: str
    reporter: str
    status: str
    observed_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    promoted_to: list[str] = field(default_factory=list)
    duplicate_of: str = ""
    notes_history: list[dict] = field(default_factory=list)
    source: Optional[dict[str, Any]] = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "observation": self.observation,
            "kind": self.kind,
            "target": self.target,
            "context": self.context,
            "reporter": self.reporter,
            "status": self.status,
            "observed_at": self.observed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "promoted_to": list(self.promoted_to),
            "duplicate_of": self.duplicate_of,
            "notes_history": list(self.notes_history),
            "source": dict(self.source) if self.source else None,
        }

    def to_brief_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "observation": self.observation,
            "status": self.status,
            "observed_at": self.observed_at,
            "updated_at": self.updated_at,
        }

    def to_compact_dict(self) -> dict[str, Any]:
        d = self.to_brief_dict()
        d["kind"] = self.kind
        d["target"] = self.target
        return d


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class DogfoodError(ValueError):
    """Raised on invalid dogfood operations (bad kind/status, unknown id)."""


class DogfoodStore:
    """Developer-side dogfood (friction/UX observation) store.

    Persists observations as ``Tier.DERIVED`` entries with the ``dogfood``
    tag. One entry per observation.

    Seeds five curated entries on the first construction (from
    ``fr_developer_1324440c``'s "concrete shape" list) if the store
    contains zero ``dogfood``-tagged entries. Idempotent across restarts.
    """

    def __init__(self, knowledge: KnowledgeStore, *, seed: bool = True):
        self.knowledge = knowledge
        if seed:
            self._seed_if_empty()

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    def get_dogfood(self, dog_id: str) -> Optional[Dogfood]:
        entry = self.knowledge.get(dog_id)
        if entry is None or "dogfood" not in (entry.tags or []):
            return None
        return _dogfood_from_entry(entry)

    def list_dogfood(
        self,
        *,
        kind: str = "",
        target: str = "",
        since: Optional[float] = None,
        status: Optional[Iterable[str] | str] = None,
        include_terminal: bool = False,
        limit: Optional[int] = 20,
    ) -> list[Dogfood]:
        """List dogfood entries, newest ``observed_at`` first.

        Default excludes terminal statuses (promoted / dismissed /
        duplicate). Pass ``status="all"`` (or ``include_terminal=True``)
        to include them. ``since`` filters by ``observed_at`` cutoff.
        ``limit`` caps the returned count; pass None for no cap.
        """
        allowed_statuses = _parse_status_filter(status, include_terminal=include_terminal)
        if kind and kind not in ALLOWED_KINDS:
            raise DogfoodError(
                f"kind must be one of {sorted(ALLOWED_KINDS)}, got {kind!r}"
            )

        entries = self.knowledge.get_by_tier(Tier.DERIVED)
        dogs: list[Dogfood] = []
        for entry in entries:
            if "dogfood" not in (entry.tags or []):
                continue
            d = _dogfood_from_entry(entry)
            if kind and d.kind != kind:
                continue
            if target and d.target != target:
                continue
            if since is not None and d.observed_at < since:
                continue
            if allowed_statuses is not None and d.status not in allowed_statuses:
                continue
            dogs.append(d)
        dogs.sort(key=lambda x: x.observed_at, reverse=True)
        if limit is not None and limit >= 0:
            dogs = dogs[:limit]
        return dogs

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    def log_dogfood(
        self,
        observation: str,
        *,
        kind: str = DOGFOOD_KIND_FRICTION,
        target: str = "",
        context: str = "",
        reporter: str = "",
        source: Optional[dict[str, Any]] = None,
        observed_at: Optional[float] = None,
    ) -> Dogfood:
        """Record a single dogfood observation.

        **Must be cheap.** No LLM calls, no embedding, no external
        service calls — purely a local SQLite write. The whole point of
        the tool is that capturing friction is near-free.
        """
        observation = (observation or "").strip()
        if not observation:
            raise DogfoodError("log_dogfood requires a non-empty observation")
        if kind not in ALLOWED_KINDS:
            raise DogfoodError(
                f"kind must be one of {sorted(ALLOWED_KINDS)}, got {kind!r}"
            )

        now = time.time()
        observed = observed_at if observed_at is not None else now
        dog_id = _derive_dog_id(observation, observed)
        existing = self.knowledge.get(dog_id)
        if existing is not None and "dogfood" in (existing.tags or []):
            raise DogfoodError(
                f"dogfood already exists with id {dog_id} "
                "(same observation+observed_at as an existing entry)"
            )

        dog = Dogfood(
            id=dog_id,
            observation=observation,
            kind=kind,
            target=target,
            context=context,
            reporter=reporter,
            status=DOGFOOD_STATUS_OBSERVED,
            observed_at=observed,
            created_at=now,
            updated_at=now,
            promoted_to=[],
            duplicate_of="",
            notes_history=[{"at": now, "status": DOGFOOD_STATUS_OBSERVED, "notes": "logged"}],
            source=dict(source) if source else None,
        )
        self._store(dog)
        return dog

    def mark_dismissed(self, dog_id: str, notes: str = "") -> Dogfood:
        """Terminal: dismiss a dogfood observation (won't act on it)."""
        dog = self.get_dogfood(dog_id)
        if dog is None:
            raise DogfoodError(f"unknown dogfood id: {dog_id}")
        if dog.status in TERMINAL_STATUSES:
            raise DogfoodError(
                f"cannot dismiss {dog_id}: status is {dog.status!r} (terminal)"
            )
        now = time.time()
        dog.status = DOGFOOD_STATUS_DISMISSED
        dog.notes_history.append({
            "at": now,
            "status": DOGFOOD_STATUS_DISMISSED,
            "notes": notes or "dismissed",
        })
        dog.updated_at = now
        self._store(dog)
        return dog

    def mark_duplicate(self, dog_id: str, duplicate_of: str) -> Dogfood:
        """Terminal: mark ``dog_id`` as a duplicate of ``duplicate_of``."""
        duplicate_of = (duplicate_of or "").strip()
        if not duplicate_of:
            raise DogfoodError("duplicate_of must be non-empty")
        if duplicate_of == dog_id:
            raise DogfoodError("a dogfood entry cannot be a duplicate of itself")
        if self.get_dogfood(duplicate_of) is None:
            raise DogfoodError(f"unknown duplicate target id: {duplicate_of!r}")
        dog = self.get_dogfood(dog_id)
        if dog is None:
            raise DogfoodError(f"unknown dogfood id: {dog_id}")
        if dog.status in TERMINAL_STATUSES:
            raise DogfoodError(
                f"cannot mark duplicate on {dog_id}: status is {dog.status!r} (terminal)"
            )
        now = time.time()
        dog.status = DOGFOOD_STATUS_DUPLICATE
        dog.duplicate_of = duplicate_of
        dog.notes_history.append({
            "at": now,
            "status": DOGFOOD_STATUS_DUPLICATE,
            "notes": f"duplicate of {duplicate_of}",
        })
        dog.updated_at = now
        self._store(dog)
        return dog

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _store(self, dog: Dogfood) -> None:
        entry = KnowledgeEntry(
            id=dog.id,
            tier=Tier.DERIVED,
            title=dog.observation[:80],
            content=dog.observation,
            source="developer.dogfood_store",
            scope="development",
            confidence=1.0,
            status=EntryStatus.DISTILLED,
            tags=["dogfood", f"kind:{dog.kind}"]
            + ([f"target:{dog.target}"] if dog.target else []),
            metadata={
                "dogfood_status": dog.status,
                "kind": dog.kind,
                "target": dog.target,
                "context": dog.context,
                "reporter": dog.reporter,
                "observed_at": dog.observed_at,
                "promoted_to": list(dog.promoted_to),
                "duplicate_of": dog.duplicate_of,
                "notes_history": list(dog.notes_history),
                "source": dict(dog.source) if dog.source else None,
            },
            created_at=dog.created_at,
            updated_at=dog.updated_at,
        )
        self.knowledge.add(entry)
        stored = self.knowledge.get(dog.id)
        if stored is not None:
            dog.created_at = stored.created_at
            dog.updated_at = stored.updated_at

    def _count_dogfood(self) -> int:
        count = 0
        for entry in self.knowledge.get_by_tier(Tier.DERIVED):
            if "dogfood" in (entry.tags or []):
                count += 1
        return count

    def _seed_if_empty(self) -> None:
        """Write the 5 curated seed entries if no dogfood entries exist yet.

        Seeds pulled verbatim from ``fr_developer_1324440c``'s "concrete
        shape" list. Idempotent: no-op if any dogfood row already exists.

        Uses a deterministic backdated ``observed_at`` per entry so the
        seed rows keep a stable ordering across first-runs regardless of
        clock skew, and so their ids (which depend on observed_at) are
        deterministic.
        """
        if self._count_dogfood() > 0:
            return

        # Deterministic timestamps so seed ids are stable across fresh DBs.
        # Spaced by 1 second so list ordering (newest first) is
        # predictable for tests and for downstream dashboards.
        base = 1_700_000_000.0  # a fixed anchor, not "now"
        seeds: list[tuple[str, str, str, str, str, float]] = [
            (
                "Had to use WebFetch + ingest_file to work around Substack 403 — "
                "4 tool calls where 1 should have sufficed.",
                DOGFOOD_KIND_FRICTION,
                "researcher",
                "",
                "user",
                base + 0,
            ),
            (
                "distill_paper output is ~4KB of JSON-escaped JSON-in-JSON; "
                "hard to read inline.",
                DOGFOOD_KIND_UX,
                "researcher",
                "",
                "user",
                base + 1,
            ),
            (
                "Wanted to ingest a URL without launching Claude Code / MCP.",
                DOGFOOD_KIND_UX,
                "researcher",
                "",
                "user",
                base + 2,
            ),
            (
                "Distiller tagged a non-RL article as reinforcement-learning.",
                DOGFOOD_KIND_BUG,
                "researcher",
                "",
                "user",
                base + 3,
            ),
            (
                "promote_fr returns only the id; to verify the description saved "
                "correctly I'd have to call get_fr_local.",
                DOGFOOD_KIND_FRICTION,
                "developer",
                "",
                "user",
                base + 4,
            ),
        ]
        for observation, kind, target, context, reporter, observed_at in seeds:
            self.log_dogfood(
                observation,
                kind=kind,
                target=target,
                context=context,
                reporter=reporter,
                observed_at=observed_at,
            )


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _dogfood_from_entry(entry: KnowledgeEntry) -> Dogfood:
    meta = entry.metadata or {}
    src = meta.get("source")
    return Dogfood(
        id=entry.id,
        observation=entry.content,
        kind=meta.get("kind", DOGFOOD_KIND_FRICTION),
        target=meta.get("target", ""),
        context=meta.get("context", ""),
        reporter=meta.get("reporter", ""),
        status=meta.get("dogfood_status", DOGFOOD_STATUS_OBSERVED),
        observed_at=float(meta.get("observed_at") or entry.created_at),
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        promoted_to=list(meta.get("promoted_to") or []),
        duplicate_of=meta.get("duplicate_of", ""),
        notes_history=list(meta.get("notes_history") or []),
        source=dict(src) if isinstance(src, dict) else None,
    )


def _derive_dog_id(observation: str, observed_at: float) -> str:
    """Stable dogfood id per (observation, observed_at).

    ``observed_at`` is included so the same observation logged at two
    distinct moments (e.g. recurring friction) yields distinct ids —
    the store's job is to capture every occurrence, not dedupe.
    """
    payload = f"{observation}|{observed_at:.6f}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:8]
    return f"dog_{digest}"


def _parse_status_filter(
    status: Optional[Iterable[str] | str],
    *,
    include_terminal: bool,
) -> Optional[set[str]]:
    if status is None:
        if include_terminal:
            return None
        return set(ACTIVE_STATUSES)
    if isinstance(status, str):
        if status.strip().lower() == "all":
            return None
        parts = [s.strip() for s in status.split(",") if s.strip()]
    else:
        parts = [str(s).strip() for s in status if str(s).strip()]
    if not parts:
        return set(ACTIVE_STATUSES) if not include_terminal else None
    allowed = set()
    for p in parts:
        if p.lower() == "all":
            return None
        if p not in ALL_STATUSES:
            raise DogfoodError(
                f"status filter contains unknown value {p!r}; "
                f"allowed: {sorted(ALL_STATUSES | {'all'})}"
            )
        allowed.add(p)
    return allowed

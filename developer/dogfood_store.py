"""Developer-owned dogfood (friction/UX) store.

Phase 1 of the tracking-infrastructure stack (``fr_developer_1324440c``)
defined the CRUD slice: log, list, get, mark dismissed, mark duplicate.
Phase 2A layers the promotion path on top: :meth:`DogfoodStore.
record_promotion` is the authoritative mutation for stamping an
observation as promoted to a bug or FR while **preserving the original
observation verbatim** — the spec's per-FR acceptance criterion for
provenance. Cross-store orchestration (filing the downstream bug/FR)
lives at the agent handler level; this module stays free of bug/FR
knowledge.

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

from developer.project_store import DEFAULT_PROJECT
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
    # Phase 3 of fr_developer_5d0a8711: project as a first-class dimension.
    project: str = DEFAULT_PROJECT

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "observation": self.observation,
            "kind": self.kind,
            "target": self.target,
            "context": self.context,
            "reporter": self.reporter,
            "status": self.status,
            "project": self.project,
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
        project: Optional[str] = None,
    ) -> list[Dogfood]:
        """List dogfood entries, newest ``observed_at`` first.

        Default excludes terminal statuses (promoted / dismissed /
        duplicate). Pass ``status="all"`` (or ``include_terminal=True``)
        to include them. ``since`` filters by ``observed_at`` cutoff.
        ``limit`` caps the returned count; pass ``None`` for no cap.
        Negative values are normalized to ``0`` (returns empty list) to
        match the MCP handler's normalization — callers who want no cap
        must pass ``None`` explicitly.
        """
        allowed_statuses = _parse_status_filter(status, include_terminal=include_terminal)
        # Normalize project filter — None = all projects; anything else
        # maps ""/whitespace to DEFAULT_PROJECT so bus/CLI defaults
        # don't silently bypass the filter.
        if project is not None:
            project = project.strip() or DEFAULT_PROJECT
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
            if project is not None and d.project != project:
                continue
            if since is not None and d.observed_at < since:
                continue
            if allowed_statuses is not None and d.status not in allowed_statuses:
                continue
            dogs.append(d)
        dogs.sort(key=lambda x: x.observed_at, reverse=True)
        if limit is not None:
            effective_limit = max(limit, 0)
            dogs = dogs[:effective_limit]
        return dogs

    def triage_queue(self, *, limit: Optional[int] = 10) -> list[Dogfood]:
        """Return the next entries to triage, oldest-first, rank-scored.

        Picks ``observed``-status only (not yet triaged / promoted /
        dismissed / duplicate), then ranks by a blend of recency and a
        per-kind priority so the output is a useful periodic-triage
        input rather than an insertion-ordered dump.

        Scoring rationale:
            - recency term (``observed_at``) is ascending — oldest
              unresolved friction sinks to the top so stale captures
              aren't forgotten.
            - kind_priority nudges ``bug`` > ``ux`` / ``friction`` >
              ``docs`` / ``other`` — a latent bug observation is more
              urgent to convert than a docs nit.

        The score is stable (pure function of stored fields), so the
        order is deterministic across identical inputs. ``limit`` caps
        the return size; pass ``None`` for no cap. Negative values are
        normalized to ``0``.
        """
        # kind_priority: lower int means triage sooner. ``bug`` first
        # because an un-triaged bug is a latent defect masquerading as
        # friction; ``docs`` / ``other`` last because they rarely block
        # real work.
        kind_priority = {
            DOGFOOD_KIND_BUG: 0,
            DOGFOOD_KIND_UX: 1,
            DOGFOOD_KIND_FRICTION: 1,
            DOGFOOD_KIND_DOCS: 2,
            DOGFOOD_KIND_OTHER: 3,
        }

        entries = self.knowledge.get_by_tier(Tier.DERIVED)
        dogs: list[Dogfood] = []
        for entry in entries:
            if "dogfood" not in (entry.tags or []):
                continue
            d = _dogfood_from_entry(entry)
            if d.status != DOGFOOD_STATUS_OBSERVED:
                continue
            dogs.append(d)

        # Sort key: (kind_priority asc, observed_at asc) — oldest urgent
        # items first. Tie-breaks on id for deterministic ordering.
        dogs.sort(key=lambda d: (
            kind_priority.get(d.kind, 99),
            d.observed_at,
            d.id,
        ))
        if limit is not None:
            effective_limit = max(int(limit), 0)
            dogs = dogs[:effective_limit]
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
        project: str = DEFAULT_PROJECT,
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
        if existing is not None:
            # Refuse to overwrite ANY pre-existing entry at this id, not just
            # dogfood-tagged ones. A non-dogfood entry at the same id signals
            # either data corruption or id-namespace collision with another
            # entry type; either way, silently overwriting it would lose data.
            if "dogfood" in (existing.tags or []):
                raise DogfoodError(
                    f"dogfood already exists with id {dog_id} "
                    "(same observation+observed_at as an existing entry)"
                )
            raise DogfoodError(
                f"id collision with non-dogfood entry at {dog_id} "
                f"(existing tags: {sorted(existing.tags or [])}); refusing to overwrite"
            )

        project = (project or DEFAULT_PROJECT).strip() or DEFAULT_PROJECT
        dog = Dogfood(
            id=dog_id,
            observation=observation,
            kind=kind,
            target=target,
            context=context,
            reporter=reporter,
            status=DOGFOOD_STATUS_OBSERVED,
            project=project,
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

    def record_promotion(
        self,
        dog_id: str,
        target_id: str,
        target_kind: str,
        *,
        notes: str = "",
    ) -> Dogfood:
        """Mark ``dog_id`` as promoted to a bug or FR.

        Authoritative mutation path for Phase 2A's dogfood->(bug|FR)
        wiring. The agent-layer ``triage_dogfood`` handler calls this
        after creating (or identifying) the downstream record.

        **Preserves the observation verbatim.** Only ``status``,
        ``promoted_to``, and ``notes_history`` change; the observation
        text stays intact. This is the spec's per-FR acceptance
        criterion for provenance — the original friction report must
        survive triage.

        Idempotent: promoting to the same target twice is a no-op (no
        duplicate entry in ``promoted_to``, no audit noise). Allows
        promoting to multiple distinct targets (e.g. one bug + one FR)
        — in that case ``promoted_to`` grows; ``status`` stays
        ``promoted``.

        ``target_kind`` must be ``bug`` or ``fr``. Promotion is terminal
        for the observation's lifecycle as friction-triage input, but
        the row itself remains readable.
        """
        target_id = (target_id or "").strip()
        if not target_id:
            raise DogfoodError("target_id must be non-empty")
        if target_kind not in {"bug", "fr"}:
            raise DogfoodError(
                f"target_kind must be 'bug' or 'fr', got {target_kind!r}"
            )
        dog = self.get_dogfood(dog_id)
        if dog is None:
            raise DogfoodError(f"unknown dogfood id: {dog_id}")
        # A dogfood entry that's already terminally dismissed or a
        # duplicate shouldn't be promoted — the terminal transition is
        # an explicit curation decision. A row already ``promoted`` can
        # pick up additional downstream targets (see idempotent clause).
        if dog.status in {DOGFOOD_STATUS_DISMISSED, DOGFOOD_STATUS_DUPLICATE}:
            raise DogfoodError(
                f"cannot promote {dog_id}: status is {dog.status!r} (terminal)"
            )
        if target_id in dog.promoted_to:
            return dog  # idempotent
        now = time.time()
        dog.promoted_to.append(target_id)
        dog.status = DOGFOOD_STATUS_PROMOTED
        audit = f"promoted to {target_kind} {target_id}"
        if notes:
            audit = f"{audit} ({notes})"
        dog.notes_history.append({
            "at": now,
            "status": DOGFOOD_STATUS_PROMOTED,
            "notes": audit,
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
                "project": dog.project or DEFAULT_PROJECT,
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

    # ------------------------------------------------------------------
    # fr_developer_1c5178d2 — project-dimension migration helper
    # ------------------------------------------------------------------

    def migrate_records_to_project(
        self, project: str = DEFAULT_PROJECT
    ) -> int:
        """Stamp ``project`` onto dogfood records whose metadata lacks it.

        In-place metadata patch via :func:`dataclasses.replace` — see
        :meth:`FRStore.migrate_records_to_project` for the rationale
        around not round-tripping through the dataclass serializer.
        Idempotent; returns the number of records actually rewritten.
        """
        import dataclasses
        project = (project or DEFAULT_PROJECT).strip() or DEFAULT_PROJECT
        rewritten = 0
        for entry in self.knowledge.get_by_tier(Tier.DERIVED):
            if "dogfood" not in (entry.tags or []):
                continue
            meta = dict(entry.metadata or {})
            if meta.get("project"):
                continue
            meta["project"] = project
            patched = dataclasses.replace(entry, metadata=meta)
            self.knowledge.add(patched)
            rewritten += 1
        return rewritten

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
        project=meta.get("project") or DEFAULT_PROJECT,
        observed_at=float(
            meta["observed_at"]
            if meta.get("observed_at") is not None
            else entry.created_at
        ),
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

"""Developer-owned Project store.

Phase 1 of ``fr_developer_5d0a8711`` (project as first-class entity).
Lands the storage primitive only — no migration of FR / milestone / spec /
bug / dogfood records, no ``khonliang``-bootstrap seed, no skill
registrations. Those land in follow-up commits on the same branch.

Mirrors :mod:`developer.fr_store` and :mod:`developer.bug_store` on
storage: one ``Tier.DERIVED`` ``KnowledgeEntry`` per project, tagged
``project``. Keeps this store independent of the other stores; the
multi-project cross-store migration layers on top later.

Schema (per ``fr_developer_5d0a8711``)::

    id           — ``project_<slug>`` (slug is caller-supplied, validated)
    name         — human-readable label (default: slug)
    slug         — URL-safe identifier, unique per developer instance
    domain       — free-form string (e.g. ``software-engineering``,
                   ``genealogy``). Defaults to ``generic``.
    repos        — list of ``{path, role, install_name}`` dicts.
                   ``role`` ∈ {``library``, ``agent``, ``service``, ``app``}.
    config       — free-form dict for per-project overrides.
    created_at   — epoch seconds
    updated_at   — epoch seconds
    status       — ``active`` | ``retired``  (default ``active``)

Status terminology mirrors the other stores: ``active`` records are the
default working set; ``retired`` is a soft-archive — data preserved,
reads require an explicit opt-in flag. Hard delete is not supported in
this phase.
"""

from __future__ import annotations

import re
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


PROJECT_ROLE_LIBRARY = "library"
PROJECT_ROLE_AGENT = "agent"
PROJECT_ROLE_SERVICE = "service"
PROJECT_ROLE_APP = "app"

ALLOWED_ROLES = {
    PROJECT_ROLE_LIBRARY,
    PROJECT_ROLE_AGENT,
    PROJECT_ROLE_SERVICE,
    PROJECT_ROLE_APP,
}

PROJECT_STATUS_ACTIVE = "active"
PROJECT_STATUS_RETIRED = "retired"

ALLOWED_STATUSES = {PROJECT_STATUS_ACTIVE, PROJECT_STATUS_RETIRED}

# Slug rules: lowercase ASCII alphanumerics, dashes, underscores. No slashes,
# no whitespace. Kept tight so slugs are safe for filesystem paths, URLs, and
# future project-scoped data dirs.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# KnowledgeStore tag used for this record type. Distinct from ``fr``,
# ``bug``, ``dogfood``, ``milestone`` so the single underlying DB can host
# every store without cross-talk.
ENTRY_TAG = "project"

# Project-dimension default for records that pre-date fr_developer_1c5178d2
# (Phase 3 of fr_developer_5d0a8711). Every FR / milestone / bug / dogfood
# record gets a ``project`` field with this default so records written
# before the field existed continue to surface correctly at read time.
# ``migrate_records_to_project`` bulk-stamps the metadata explicitly when
# the caller wants the data tidy (not just fallback-tidy).
DEFAULT_PROJECT = "khonliang"


def normalize_project(value: Any) -> str:
    """Canonicalize a project slug for read/write/filter consistency.

    Every surface that touches the project dimension routes through
    this helper so writers, readers, list-filter inputs, and migration
    checks agree on what counts as "effectively empty". Without it, a
    record whose persisted metadata is ``"alpha "`` would round-trip
    through a reader unstripped, while the list filter strips its
    input — the record becomes unfilterable.

    Rules:
    - ``None`` or missing → :data:`DEFAULT_PROJECT`.
    - Any string, including ``""`` or whitespace-only: stripped; if
      the result is empty, falls back to :data:`DEFAULT_PROJECT`.
    - Non-string / non-None → coerced via :func:`str`, then stripped.
      Defensive but not expected in practice; metadata values for
      ``project`` should always be strings.
    """
    if value is None:
        return DEFAULT_PROJECT
    if isinstance(value, str):
        return value.strip() or DEFAULT_PROJECT
    return str(value).strip() or DEFAULT_PROJECT


# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------


@dataclass
class RepoRef:
    """One repo belonging to a project."""

    path: str
    role: str = PROJECT_ROLE_APP
    install_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "role": self.role, "install_name": self.install_name}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RepoRef":
        # Normalize path early: `None` and whitespace-only must NOT survive as
        # a truthy str (str(None) == 'None', " " is truthy). Later validation
        # relies on `if not ref.path` which breaks without this normalization.
        raw_path = data.get("path")
        path = (raw_path or "").strip() if isinstance(raw_path, str) or raw_path is None else str(raw_path).strip()
        return cls(
            path=path,
            role=str(data.get("role") or PROJECT_ROLE_APP),
            install_name=str(data.get("install_name") or ""),
        )


@dataclass
class Project:
    """A project — 1..N repos, 1 domain, shared developer-lifecycle scope."""

    slug: str
    name: str = ""
    domain: str = "generic"
    repos: list[RepoRef] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    status: str = PROJECT_STATUS_ACTIVE
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def id(self) -> str:
        return f"project_{self.slug}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name or self.slug,
            "domain": self.domain,
            "repos": [r.to_dict() for r in self.repos],
            "config": dict(self.config),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        return cls(
            slug=str(data.get("slug", "")),
            name=str(data.get("name") or data.get("slug", "")),
            domain=str(data.get("domain") or "generic"),
            repos=[RepoRef.from_dict(r) for r in data.get("repos", []) or []],
            config=dict(data.get("config") or {}),
            status=str(data.get("status") or PROJECT_STATUS_ACTIVE),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_slug(slug: str) -> None:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise ValueError(
            f"project slug {slug!r} must be lowercase ascii alphanumeric/dash/"
            "underscore, 1-64 chars, starting with alphanumeric"
        )


def _validate_role(role: str) -> None:
    if role not in ALLOWED_ROLES:
        raise ValueError(f"repo role {role!r} not in {sorted(ALLOWED_ROLES)}")


def _validate_status(status: str) -> None:
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"project status {status!r} not in {sorted(ALLOWED_STATUSES)}")


def _normalize_repos(repos: Iterable[Any]) -> list[RepoRef]:
    """Normalize a ``repos`` arg into a list of :class:`RepoRef`.

    Accepted shapes per entry: ``str`` (path-only), ``dict`` with
    path/role/install_name, or :class:`RepoRef` instance.

    Note: a bare ``str`` for ``repos`` is treated as a single-repo list —
    iterating a string would otherwise produce one RepoRef per character,
    a well-known footgun. Passing ``repos="/a"`` is equivalent to
    ``repos=["/a"]``.
    """

    # Guard the bare-string case before iteration. Mirrors milestone_store's
    # _normalize_fr_ids convention of refusing to treat a string as an
    # iterable-of-chars.
    if isinstance(repos, str):
        repos = [repos]

    out: list[RepoRef] = []
    for raw in repos:
        if isinstance(raw, str):
            ref = RepoRef(path=raw.strip())
        elif isinstance(raw, RepoRef):
            ref = raw
        elif isinstance(raw, dict):
            ref = RepoRef.from_dict(raw)
        else:
            raise TypeError(
                f"repo entry must be str, dict, or RepoRef; got {type(raw).__name__}"
            )
        _validate_role(ref.role)
        if not ref.path or not ref.path.strip():
            raise ValueError("repo entry must have a non-empty 'path'")
        out.append(ref)
    return out


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ProjectDuplicateError(ValueError):
    """Raised when trying to ``create`` a slug that already exists."""


class ProjectStore:
    """Persistent store for :class:`Project` records.

    Backed by :class:`khonliang.knowledge.store.KnowledgeStore`. One
    ``KnowledgeEntry`` per project, tagged with :data:`ENTRY_TAG` in
    ``KnowledgeEntry.tags`` so :class:`FRStore` / :class:`BugStore` /
    etc. can coexist in the same store without cross-talk.
    """

    def __init__(self, knowledge_store: KnowledgeStore) -> None:
        self.knowledge = knowledge_store

    # ---- internal ---------------------------------------------------------

    def _deserialize(self, entry: KnowledgeEntry) -> Project:
        import json

        raw = entry.content or "{}"
        data = json.loads(raw)
        project = Project.from_dict(data)
        # KnowledgeStore.add() stamps its own ``updated_at`` on every write,
        # so the entry's timestamps are the authoritative source of truth
        # (see FRStore._store docs for the same pattern). Prefer them over
        # whatever was serialized into the content blob to avoid divergence
        # on round-trips.
        if entry.created_at:
            project.created_at = float(entry.created_at)
        if entry.updated_at:
            project.updated_at = float(entry.updated_at)
        return project

    def _serialize(self, project: Project) -> str:
        import json

        return json.dumps(project.to_dict(), sort_keys=True)

    def _put(self, project: Project) -> None:
        # Pass the in-memory created_at (preserved on re-writes of an
        # existing project). KnowledgeStore.add will ALWAYS overwrite
        # updated_at with its own clock — that's the authoritative source
        # of truth for the persisted timestamp, so we sync it back after.
        entry = KnowledgeEntry(
            id=project.id,
            title=project.name or project.slug,
            content=self._serialize(project),
            tier=Tier.DERIVED,
            status=EntryStatus.DISTILLED,
            tags=[ENTRY_TAG, f"status:{project.status}", f"domain:{project.domain}"],
            created_at=project.created_at,
            updated_at=project.updated_at,
        )
        self.knowledge.add(entry)
        persisted = self.knowledge.get(project.id)
        if persisted is not None:
            if persisted.created_at:
                project.created_at = float(persisted.created_at)
            if persisted.updated_at:
                project.updated_at = float(persisted.updated_at)

    # ---- public API -------------------------------------------------------

    def create(
        self,
        slug: str,
        repos: Iterable[Any],
        *,
        name: Optional[str] = None,
        domain: str = "generic",
        config: Optional[dict[str, Any]] = None,
    ) -> Project:
        """Create a new project record.

        Raises :class:`ProjectDuplicateError` if ``slug`` already exists.
        """

        _validate_slug(slug)
        entry_id = f"project_{slug}"
        if self.knowledge.get(entry_id) is not None:
            raise ProjectDuplicateError(f"project {slug!r} already exists")

        # Leave timestamps at 0.0 so KnowledgeStore.add stamps both with
        # the same atomic ``now`` — preserves the
        # ``created_at == updated_at`` invariant on create, which can't
        # hold if we use time.time() here (store's clock runs later).
        project = Project(
            slug=slug,
            name=name or slug,
            domain=str(domain or "generic"),
            repos=_normalize_repos(repos),
            config=dict(config or {}),
            status=PROJECT_STATUS_ACTIVE,
            created_at=0.0,
            updated_at=0.0,
        )
        self._put(project)
        return project

    def get(self, slug: str) -> Optional[Project]:
        """Return the project with the given slug, or ``None`` if absent.

        Mirrors :meth:`FRStore.get` / :meth:`BugStore.get_bug`: missing
        record is a return-None, not a raise. Caller decides whether
        absence is exceptional.

        Tag-gated: even if some other store (or a manual write) uses the
        ``project_<slug>`` id shape, a record without :data:`ENTRY_TAG`
        in its tags is treated as absent. Prevents accidental cross-
        store bleed-through.

        Invalid slug (never a match by construction) still raises
        :class:`ValueError` — signals a programming error, not a
        runtime absence.
        """

        _validate_slug(slug)
        entry = self.knowledge.get(f"project_{slug}")
        if entry is None:
            return None
        if ENTRY_TAG not in (entry.tags or []):
            # Id collision with some other store; do not surface a
            # non-project record as a Project.
            return None
        return self._deserialize(entry)

    def exists(self, slug: str) -> bool:
        try:
            _validate_slug(slug)
        except ValueError:
            return False
        entry = self.knowledge.get(f"project_{slug}")
        return entry is not None and ENTRY_TAG in (entry.tags or [])

    def list(self, *, include_retired: bool = False) -> list[Project]:
        """Return projects in the store.

        Filters retired by default; pass ``include_retired=True`` for the
        full roster (e.g. archival browsing).
        """

        out: list[Project] = []
        # Mirrors FRStore.list / BugStore.list: scan the Tier.DERIVED slice
        # and filter by tag in-memory. KnowledgeStore has no tag-indexed
        # query path today; volume is low so the linear scan is fine.
        for entry in self.knowledge.get_by_tier(Tier.DERIVED):
            if ENTRY_TAG not in (entry.tags or []):
                continue
            try:
                project = self._deserialize(entry)
            except Exception:
                # Skip corrupt entries rather than failing the whole list —
                # same forgiving read posture as bug_store / fr_store.
                continue
            if not include_retired and project.status == PROJECT_STATUS_RETIRED:
                continue
            out.append(project)
        # Stable order: alphabetical by slug.
        out.sort(key=lambda p: p.slug)
        return out

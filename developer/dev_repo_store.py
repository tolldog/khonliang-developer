"""Developer-owned dev-repo registry.

``fr_developer_5f3dc62e``: researcher's repo registry conflates "data
source to ingest" with "dev location we actively work in." This store
is developer's own registry of repos it actively develops against —
distinct from researcher's ingestion-focused registry and from
:class:`developer.project_store.ProjectStore` (which models a project as
1..N repos plus domain/config for the multi-project productization
effort). ``DevRepoStore`` is a flatter, dev-lifecycle-scoped registry
keyed by the same project slug convention so the two can be cross-
referenced by slug, without either owning the other's schema.

Mirrors :mod:`developer.project_store` on storage: one ``Tier.DERIVED``
``KnowledgeEntry`` per repo, JSON-serialized in ``content``, tagged
``dev_repo`` so it coexists in the same ``developer.db`` without
cross-talk with FR / bug / dogfood / milestone / project records.

Schema (per ``fr_developer_5f3dc62e``)::

    project                    — canonical short name / slug (id key)
    repo_path                  — absolute filesystem path to the checkout
    default_branch             — default ``main``
    remote_url                 — optional
    test_command                — optional
    compile_command             — optional
    reviewer_convention        — free-form string
    owning_agents               — list[str] of bus agent_ids
    last_hygiene_audit_at       — epoch seconds, optional (0.0 = unset)
    last_hygiene_disposition    — optional free-form string
    in_flight_pr_numbers        — list[int], computed/cached by callers
    created_at / updated_at     — epoch seconds

Secondary pieces explicitly out of scope for this pass (documented as
follow-up, not half-implemented): ``walk_dependencies`` (dep-graph
walk) and the bus event sync (``dev_repo.registered`` emit /
``evidence_source.registered`` subscribe). ``audit_repo_hygiene_all``
and ``git_status_all`` are thin wrappers living at the agent-handler
level (they need the existing single-repo ``audit_repo_hygiene`` /
:class:`developer.git_client.GitClient` primitives, not store state) —
see ``developer/agent.py``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from khonliang.knowledge.store import (
    EntryStatus,
    KnowledgeEntry,
    KnowledgeStore,
    Tier,
)

# Slug rules mirror ProjectStore's _SLUG_RE: lowercase ascii
# alphanumerics, dashes, underscores, 1-64 chars, starting alphanumeric.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

ENTRY_TAG = "dev_repo"


class DevRepoError(ValueError):
    """Raised on invalid dev-repo registry operations."""


def _validate_project(project: str) -> None:
    if not isinstance(project, str) or not _SLUG_RE.match(project):
        raise DevRepoError(
            f"project {project!r} must be lowercase ascii alphanumeric/dash/"
            "underscore, 1-64 chars, starting with alphanumeric"
        )


def _normalize_str_list(value: Any) -> list[str]:
    """Coerce a caller-supplied list-ish arg, guarding the bare-str footgun.

    Mirrors ``milestone_store._normalize_fr_ids``: a bare ``str`` is a
    valid single-item value, but it's also ``Iterable[str]`` over its
    own characters — without this guard ``owning_agents="researcher"``
    would silently become ``["r", "e", "s", ...]``.
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value if str(v).strip()]


def _normalize_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, (str, int)):
        value = [value]
    out: list[int] = []
    for v in value:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


@dataclass
class DevRepo:
    """A registered developer-lifecycle repo location."""

    project: str
    repo_path: str
    default_branch: str = "main"
    remote_url: str = ""
    test_command: str = ""
    compile_command: str = ""
    reviewer_convention: str = ""
    owning_agents: list[str] = field(default_factory=list)
    last_hygiene_audit_at: float = 0.0
    last_hygiene_disposition: str = ""
    in_flight_pr_numbers: list[int] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def id(self) -> str:
        return f"dev_repo_{self.project}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "repo_path": self.repo_path,
            "default_branch": self.default_branch,
            "remote_url": self.remote_url,
            "test_command": self.test_command,
            "compile_command": self.compile_command,
            "reviewer_convention": self.reviewer_convention,
            "owning_agents": list(self.owning_agents),
            "last_hygiene_audit_at": self.last_hygiene_audit_at,
            "last_hygiene_disposition": self.last_hygiene_disposition,
            "in_flight_pr_numbers": list(self.in_flight_pr_numbers),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DevRepo":
        return cls(
            project=str(data.get("project", "")),
            repo_path=str(data.get("repo_path", "")),
            default_branch=str(data.get("default_branch") or "main"),
            remote_url=str(data.get("remote_url") or ""),
            test_command=str(data.get("test_command") or ""),
            compile_command=str(data.get("compile_command") or ""),
            reviewer_convention=str(data.get("reviewer_convention") or ""),
            owning_agents=_normalize_str_list(data.get("owning_agents")),
            last_hygiene_audit_at=float(data.get("last_hygiene_audit_at") or 0.0),
            last_hygiene_disposition=str(data.get("last_hygiene_disposition") or ""),
            in_flight_pr_numbers=_normalize_int_list(data.get("in_flight_pr_numbers")),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
        )


class DevRepoStore:
    """Persistent store for :class:`DevRepo` records.

    Backed by :class:`khonliang.knowledge.store.KnowledgeStore`. One
    ``KnowledgeEntry`` per registered repo, tagged :data:`ENTRY_TAG`.
    """

    def __init__(self, knowledge: KnowledgeStore) -> None:
        self.knowledge = knowledge

    # ---- internal ---------------------------------------------------------

    def _deserialize(self, entry: KnowledgeEntry) -> DevRepo:
        import json

        raw = entry.content or "{}"
        data = json.loads(raw)
        repo = DevRepo.from_dict(data)
        # KnowledgeStore.add() stamps its own updated_at on every write;
        # prefer the entry's timestamps over whatever was serialized into
        # the content blob (same pattern as ProjectStore._deserialize).
        if entry.created_at:
            repo.created_at = float(entry.created_at)
        if entry.updated_at:
            repo.updated_at = float(entry.updated_at)
        return repo

    def _serialize(self, repo: DevRepo) -> str:
        import json

        return json.dumps(repo.to_dict(), sort_keys=True)

    def _put(self, repo: DevRepo) -> None:
        entry = KnowledgeEntry(
            id=repo.id,
            title=repo.project,
            content=self._serialize(repo),
            tier=Tier.DERIVED,
            status=EntryStatus.DISTILLED,
            tags=[ENTRY_TAG, f"project:{repo.project}"],
            created_at=repo.created_at,
            updated_at=repo.updated_at,
        )
        self.knowledge.add(entry)
        persisted = self.knowledge.get(repo.id)
        if persisted is not None:
            if persisted.created_at:
                repo.created_at = float(persisted.created_at)
            if persisted.updated_at:
                repo.updated_at = float(persisted.updated_at)

    # ---- public API -------------------------------------------------------

    def register(
        self,
        project: str,
        repo_path: str,
        *,
        default_branch: str = "main",
        remote_url: str = "",
        test_command: str = "",
        compile_command: str = "",
        reviewer_convention: str = "",
        owning_agents: Optional[Iterable[str]] = None,
    ) -> DevRepo:
        """Idempotent upsert of a dev-repo registry entry.

        Re-registering an existing ``project`` overwrites the caller-
        supplied fields (this call is the source of truth for them) but
        preserves the computed/cached fields (``last_hygiene_audit_at``,
        ``last_hygiene_disposition``, ``in_flight_pr_numbers``) and
        ``created_at`` — those are populated by other operations
        (``audit_repo_hygiene_all``, PR-fleet sync), not by this
        upsert, so a routine re-register (e.g. picking up a changed
        ``test_command``) must not wipe them.
        """
        _validate_project(project)
        repo_path = str(repo_path or "").strip()
        if not repo_path:
            raise DevRepoError("repo_path is required")
        # Normalize to an absolute path at registration time. Storing a
        # relative path (".", "../repo") verbatim makes the record
        # unstable — git_status_all / audit_repo_hygiene_all reopen the
        # repo relative to whatever the agent's cwd happens to be at
        # call time, so the same record can resolve to the wrong repo
        # or fail entirely after a restart or cwd change. codex review
        # on PR #87.
        repo_path = str(Path(repo_path).expanduser().resolve())

        now = time.time()
        existing = self.get(project)
        repo = DevRepo(
            project=project,
            repo_path=repo_path,
            default_branch=str(default_branch or "main").strip() or "main",
            remote_url=str(remote_url or "").strip(),
            test_command=str(test_command or "").strip(),
            compile_command=str(compile_command or "").strip(),
            reviewer_convention=str(reviewer_convention or "").strip(),
            owning_agents=_normalize_str_list(owning_agents),
            last_hygiene_audit_at=existing.last_hygiene_audit_at if existing else 0.0,
            last_hygiene_disposition=existing.last_hygiene_disposition if existing else "",
            in_flight_pr_numbers=list(existing.in_flight_pr_numbers) if existing else [],
            created_at=existing.created_at if existing else 0.0,
            updated_at=now,
        )
        self._put(repo)
        return repo

    def get(self, project: str) -> Optional[DevRepo]:
        """Return the registered repo for ``project``, or ``None`` if absent.

        Tag-gated (same rationale as ``ProjectStore.get``): a record at
        this id without :data:`ENTRY_TAG` is treated as absent rather
        than cross-store bleed-through.
        """
        _validate_project(project)
        entry = self.knowledge.get(f"dev_repo_{project}")
        if entry is None:
            return None
        if ENTRY_TAG not in (entry.tags or []):
            return None
        return self._deserialize(entry)

    def list(self, *, scope: Optional[Iterable[str]] = None) -> list[DevRepo]:
        """Return registered repos, optionally scoped to a set of projects.

        ``scope=None`` (default) returns every registered repo. Passing
        an iterable of project slugs filters to those (missing slugs are
        silently omitted, not errors — a caller scoping to a mix of
        registered/unregistered projects gets back only what exists).
        """
        scope_set: Optional[set[str]] = None
        if scope is not None:
            scope_set = set(_normalize_str_list(scope))

        out: list[DevRepo] = []
        for entry in self.knowledge.get_by_tier(Tier.DERIVED):
            if ENTRY_TAG not in (entry.tags or []):
                continue
            try:
                repo = self._deserialize(entry)
            except Exception:
                # Skip corrupt entries rather than failing the whole
                # list — same forgiving read posture as project_store.
                continue
            if scope_set is not None and repo.project not in scope_set:
                continue
            out.append(repo)
        out.sort(key=lambda r: r.project)
        return out

    def record_hygiene_audit(
        self, project: str, *, disposition: str = "", audited_at: Optional[float] = None,
    ) -> DevRepo:
        """Stamp the cached hygiene-audit fields after an audit run.

        Thin mutator used by ``audit_repo_hygiene_all`` at the agent
        level so the registry reflects "when was this last audited and
        what came of it" without the store itself running audits.
        """
        repo = self.get(project)
        if repo is None:
            raise DevRepoError(f"unknown project: {project!r}")
        repo.last_hygiene_audit_at = float(audited_at) if audited_at is not None else time.time()
        repo.last_hygiene_disposition = str(disposition or "").strip()
        repo.updated_at = time.time()
        self._put(repo)
        return repo

    def set_in_flight_prs(self, project: str, pr_numbers: Iterable[int]) -> DevRepo:
        """Overwrite the cached ``in_flight_pr_numbers`` for ``project``."""
        repo = self.get(project)
        if repo is None:
            raise DevRepoError(f"unknown project: {project!r}")
        repo.in_flight_pr_numbers = _normalize_int_list(list(pr_numbers))
        repo.updated_at = time.time()
        self._put(repo)
        return repo


# ---------------------------------------------------------------------------
# Cross-store query helper (fr_developer_5f3dc62e)
# ---------------------------------------------------------------------------


def resolve_fr_target(fr_store: Any, dev_repos: DevRepoStore, fr_id: str) -> dict[str, Any]:
    """Resolve an FR's ``target`` to a registered dev-repo path.

    Query helper over :class:`developer.fr_store.FRStore` +
    :class:`DevRepoStore` — deliberately NOT a schema change to
    ``FRStore``; ``fr_store`` is duck-typed (only ``.get(fr_id)`` is
    used) so this module stays free of a hard import-time dependency
    on ``fr_store``, mirroring ``milestone_store``'s documented
    rationale for the same pattern.

    Returns a structured result rather than raising for the "FR
    exists but its target isn't registered" case — that's an expected,
    common outcome (most targets aren't dev repos this instance
    develops against) and callers need to branch on it, not catch it.
    """
    fr_id = str(fr_id or "").strip()
    if not fr_id:
        raise DevRepoError("fr_id is required")

    fr = fr_store.get(fr_id)
    if fr is None:
        return {
            "fr_id": fr_id,
            "target": None,
            "resolved": False,
            "reason": "unknown fr id",
            "repo": None,
        }

    # FRStore tolerates padded targets elsewhere (an FR created with
    # target=" developer " behaves normally in other FR query paths),
    # so strip here too — otherwise this resolver would report "not
    # registered" for a padded target even when the stripped slug is
    # registered. codex review on PR #87.
    target = str(getattr(fr, "target", "") or "").strip()
    if not target:
        return {
            "fr_id": fr_id,
            "target": "",
            "resolved": False,
            "reason": "fr has no target",
            "repo": None,
        }

    try:
        repo = dev_repos.get(target)
    except DevRepoError:
        # target isn't a valid dev-repo slug shape at all — still a
        # clean "not registered" outcome, not an error, from this
        # helper's point of view.
        repo = None

    if repo is None:
        return {
            "fr_id": fr_id,
            "target": target,
            "resolved": False,
            "reason": "target not registered in dev_repos",
            "repo": None,
        }

    return {
        "fr_id": fr_id,
        "target": target,
        "resolved": True,
        "reason": "",
        "repo": repo.to_dict(),
    }

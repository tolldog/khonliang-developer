"""Project ecosystem introspection — read-only view of a project's shape.

Implements ``fr_developer_5564b81f`` (Phase 1: heuristic-discovery path).
Formalizes the manual cross-section that the 2026-04-24 architecture
review produced by hand. Output mirrors the shape of ``inventory.md §1-2``.

Two discovery backends:

- **Heuristic** (ships here): walk up from a starting dir to find a
  ``pyproject.toml``, inspect its install / import name, discover sibling
  repos by shared top-level prefix, infer per-repo role from name, read
  declared ecosystem deps from each pyproject. Works today without any
  durable project record.
- **Project record** (later FR): once :class:`ProjectStore` lands skills
  + records (``fr_developer_5d0a8711``), callers pass ``project=<slug>``
  and the skill resolves against the store. Bypasses heuristics.

Live-agent state (who's currently registered on the bus) is an optional
overlay — fetched from ``{bus_url}/v1/services``. Absence of a bus URL
(or a fetch failure) degrades cleanly to an empty ``agents.live`` list.

Role inference rules (heuristic):

- ``*-lib``                 → ``library``
- ``*-bus`` (service)       → ``service``
- ``*-developer`` / ``*-researcher`` / ``*-reviewer`` / ``*-librarian``
                            → ``agent``
- everything else           → ``app``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import tomllib  # py3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Descriptors
# ---------------------------------------------------------------------------


ROLE_LIBRARY = "library"
ROLE_AGENT = "agent"
ROLE_SERVICE = "service"
ROLE_APP = "app"


@dataclass
class RepoDescriptor:
    path: str
    install_name: str
    import_name: str
    role: str = ROLE_APP
    ecosystem_deps: list[str] = field(default_factory=list)

    def to_dict(self, *, detail: str = "brief") -> dict[str, Any]:
        data: dict[str, Any] = {
            "path": self.path,
            "role": self.role,
        }
        if detail in ("brief", "full"):
            data["install_name"] = self.install_name
            data["import_name"] = self.import_name
        if detail == "full":
            data["ecosystem_deps"] = list(self.ecosystem_deps)
        return data


@dataclass
class LiveAgent:
    agent_id: str
    agent_type: str
    version: str = ""
    skill_count: int = 0
    healthy: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "version": self.version,
            "skill_count": self.skill_count,
            "healthy": self.healthy,
        }


@dataclass
class EcosystemView:
    project: str  # slug / name
    domain: str
    repos: list[RepoDescriptor] = field(default_factory=list)
    agents_live: list[LiveAgent] = field(default_factory=list)
    agents_declared: list[str] = field(default_factory=list)
    health_summary: str = ""

    def to_dict(self, *, detail: str = "brief") -> dict[str, Any]:
        if detail == "compact":
            return {
                "project": self.project,
                "repos": [r.path for r in self.repos],
                "repo_count": len(self.repos),
                "live_agent_count": len(self.agents_live),
            }
        data: dict[str, Any] = {
            "project": self.project,
            "domain": self.domain,
            "repos": [r.to_dict(detail=detail) for r in self.repos],
            "agents": {
                "declared": list(self.agents_declared),
                "live": [a.to_dict() for a in self.agents_live],
            },
            "health_summary": self.health_summary,
        }
        if detail == "full":
            data["repo_count"] = len(self.repos)
        return data


# ---------------------------------------------------------------------------
# Role inference
# ---------------------------------------------------------------------------


def infer_role(install_name: str, import_name: str) -> str:
    """Heuristic role for a khonliang-shaped repo.

    Priority: *-lib → library; service-named → service; known agent
    suffixes → agent; else app. Deliberately loose; callers can override
    with explicit project records later.
    """

    name = (install_name or import_name or "").lower()
    if not name:
        return ROLE_APP

    if name.endswith("-lib"):
        return ROLE_LIBRARY
    # Pure-service names (bus is the canonical one); extend carefully.
    if name.endswith("-bus") or name.endswith("-scheduler"):
        return ROLE_SERVICE
    if name.endswith(("-developer", "-researcher", "-reviewer", "-librarian")):
        return ROLE_AGENT
    return ROLE_APP


# ---------------------------------------------------------------------------
# Heuristic discovery
# ---------------------------------------------------------------------------


def find_pyproject(start: Path) -> Optional[Path]:
    """Walk up from ``start`` looking for a ``pyproject.toml``.

    Returns the pyproject path, or ``None`` if none found before the
    filesystem root.
    """

    cur = start.resolve()
    while True:
        candidate = cur / "pyproject.toml"
        if candidate.is_file():
            return candidate
        if cur.parent == cur:
            return None
        cur = cur.parent


def read_pyproject(path: Path) -> dict[str, Any]:
    """Parse pyproject.toml and return its dict. Empty dict on parse error."""

    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def install_name_of(pyproject_data: dict[str, Any]) -> str:
    return str(pyproject_data.get("project", {}).get("name") or "")


def import_name_of(pyproject_data: dict[str, Any]) -> str:
    """Best-effort import-name from setuptools ``find.include``.

    setuptools' ``find.include = ['foo*']`` → import name is ``foo``.
    Falls back to install_name with dashes replaced by underscores when
    no ``find.include`` config is present. ``find.where`` is intentionally
    not consulted — the first include pattern is sufficient for every
    khonliang-style package laid out as ``<pkg>/`` at the repo root.
    """

    find = (
        pyproject_data.get("tool", {})
        .get("setuptools", {})
        .get("packages", {})
        .get("find", {})
    )
    includes = find.get("include") or []
    for pattern in includes:
        # Strip wildcards, take the leading identifier.
        cleaned = pattern.rstrip("*")
        if cleaned:
            return cleaned
    # Fallback: install-name with dashes → underscores.
    install = install_name_of(pyproject_data)
    return install.replace("-", "_") if install else ""


def extract_ecosystem_deps(pyproject_data: dict[str, Any], prefix: str) -> list[str]:
    """Declared runtime deps whose **distribution name** starts with ``prefix``.

    Strips PEP 508 decorations so the result is comparable against discovered
    ``install_name`` values:

    - version specifiers: ``pkg>=1.2`` / ``pkg==0.3`` / ``pkg<=2`` / ``pkg~=0.1``
    - extras: ``pkg[extra1,extra2]``
    - environment markers: ``pkg; python_version>='3.11'``
    - direct URL refs: ``pkg @ git+https://...``
    - leading whitespace
    """

    import re

    deps = pyproject_data.get("project", {}).get("dependencies") or []
    out: list[str] = []
    # Distribution-name tokens per PEP 508: letters, digits, dot, dash,
    # underscore. Anything after that terminates the name.
    NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
    for raw in deps:
        text = str(raw)
        # `pkg @ git+...` — the name is everything before the `@`.
        name_chunk = text.split("@", 1)[0] if "@" in text else text
        # Strip extras / markers / versions — take only the leading name.
        match = NAME_RE.match(name_chunk)
        if not match:
            continue
        name = match.group(1).strip()
        if name.startswith(prefix):
            out.append(name)
    return sorted(set(out))


def discover_siblings(
    anchor_dir: Path,
    *,
    prefix: str,
    max_repos: int = 64,
) -> list[Path]:
    """Find sibling directories that share ``prefix`` and have a pyproject.

    ``anchor_dir`` is the directory CONTAINING the starting repo. Returns
    repo root paths (dirs, not pyprojects), sorted alphabetically.

    Read-only introspection — degrades cleanly on filesystem errors
    (``PermissionError`` / ``OSError`` on ``iterdir``, or on per-child
    ``is_dir`` / ``is_file`` checks) by returning whatever was collected
    so far rather than propagating the exception.
    """

    if not anchor_dir.is_dir():
        return []
    try:
        children = sorted(anchor_dir.iterdir())
    except (PermissionError, OSError):
        # Can't list — treat as no siblings. Don't fail the whole skill.
        return []
    hits: list[Path] = []
    for child in children:
        try:
            if not child.is_dir():
                continue
            if not child.name.startswith(prefix):
                continue
            if (child / "pyproject.toml").is_file():
                hits.append(child.resolve())
        except (PermissionError, OSError):
            # Per-entry stat failure — skip this one, keep going.
            continue
        if len(hits) >= max_repos:
            break
    return hits


def discover_project(
    start_dir: Path,
    *,
    sibling_prefix: Optional[str] = None,
    domain: str = "generic",
) -> EcosystemView:
    """Heuristic project view from a starting directory.

    1. Walk up from ``start_dir`` to find a pyproject.
    2. Read the repo's install + import name; use install-name as project
       slug by default (caller may override).
    3. If ``sibling_prefix`` is not given, derive from the repo's
       install-name's leading hyphen-prefix (e.g. ``khonliang-developer``
       → ``khonliang-``). Discover sibling repos with the same prefix.
    4. For each repo (anchor + siblings), build a :class:`RepoDescriptor`.
    5. Return a populated :class:`EcosystemView`. No bus queries here —
       ``agents_live`` stays empty.
    """

    pyproject = find_pyproject(start_dir.resolve())
    if pyproject is None:
        return EcosystemView(project="unknown", domain=domain, health_summary="no pyproject.toml found above starting dir")

    anchor_data = read_pyproject(pyproject)
    anchor_install = install_name_of(anchor_data)
    anchor_import = import_name_of(anchor_data)

    # Infer a prefix if not given: everything up to and including the
    # first hyphen of the install-name (e.g. "khonliang-developer"
    # → "khonliang-"). Fall back to the full install name if it has
    # no hyphen.
    if sibling_prefix is None:
        if "-" in anchor_install:
            sibling_prefix = anchor_install.split("-", 1)[0] + "-"
        else:
            sibling_prefix = anchor_install or ""

    siblings = discover_siblings(pyproject.parent.parent, prefix=sibling_prefix) if sibling_prefix else []

    # De-duplicate the anchor against siblings.
    anchor_dir = pyproject.parent.resolve()
    repos: list[RepoDescriptor] = []
    seen: set[Path] = set()
    for repo_dir in [anchor_dir, *siblings]:
        if repo_dir in seen:
            continue
        seen.add(repo_dir)
        if repo_dir == anchor_dir:
            data = anchor_data
        else:
            data = read_pyproject(repo_dir / "pyproject.toml")
        install = install_name_of(data)
        import_name = import_name_of(data)
        deps = extract_ecosystem_deps(data, sibling_prefix or "") if sibling_prefix else []
        repos.append(
            RepoDescriptor(
                path=str(repo_dir),
                install_name=install,
                import_name=import_name,
                role=infer_role(install, import_name),
                ecosystem_deps=deps,
            )
        )
    repos.sort(key=lambda r: r.path)

    declared_agents = sorted({r.install_name for r in repos if r.role == ROLE_AGENT})

    summary = f"{len(repos)} repos discovered via heuristic ({sibling_prefix or 'no-prefix'})"

    return EcosystemView(
        project=(anchor_install or "unknown"),
        domain=domain,
        repos=repos,
        agents_declared=declared_agents,
        agents_live=[],
        health_summary=summary,
    )


# ---------------------------------------------------------------------------
# Live-agent overlay
# ---------------------------------------------------------------------------


def parse_live_agents(services_payload: Any) -> list[LiveAgent]:
    """Parse the response body of ``GET /v1/services`` into :class:`LiveAgent`s.

    Accepts either a plain list of dicts (current bus shape) or a dict
    with an ``agents`` key, for forward-compat.
    """

    raw: Iterable[Any]
    if isinstance(services_payload, list):
        raw = services_payload
    elif isinstance(services_payload, dict) and isinstance(services_payload.get("agents"), list):
        raw = services_payload["agents"]
    else:
        return []

    out: list[LiveAgent] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        # Skip partial rows that lack both key identifiers — emitting them
        # as empty-string agents pollutes the list and surprises callers.
        agent_id = str(item.get("id") or "").strip()
        agent_type = str(item.get("agent_type") or "").strip()
        if not agent_id and not agent_type:
            continue
        # Healthy-by-default semantics: only explicit "unhealthy" (or a
        # known-bad synonym) flips the bit off. Missing status → healthy
        # (matches the bus schema's implicit default and keeps this parser
        # forward-compatible with future shapes that may omit the field).
        status = item.get("status")
        normalized = status.strip().lower() if isinstance(status, str) else None
        unhealthy = normalized in {"unhealthy", "failed", "down"}
        # Guard the skill_count cast — a non-numeric value in one row
        # shouldn't crash the whole skill. Default to 0 instead.
        try:
            skill_count = int(item.get("skill_count") or 0)
        except (TypeError, ValueError):
            skill_count = 0
        out.append(
            LiveAgent(
                agent_id=agent_id,
                agent_type=agent_type,
                version=str(item.get("version") or ""),
                skill_count=skill_count,
                healthy=not unhealthy,
            )
        )
    return out


def apply_live_overlay(view: EcosystemView, services_payload: Any) -> None:
    """Mutate ``view`` in place to attach the parsed live-agent list."""

    view.agents_live = parse_live_agents(services_payload)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_view(
    start_dir: Path,
    *,
    sibling_prefix: Optional[str] = None,
    domain: str = "generic",
    services_payload: Any = None,
) -> EcosystemView:
    """Build an :class:`EcosystemView` from a starting dir + optional live state.

    Caller responsibilities:
    - Resolve ``start_dir`` to whatever "this project" means for their
      context (e.g. cwd of the agent, or a repo path supplied by the
      user).
    - Fetch ``services_payload`` out-of-band (e.g. ``httpx.get(f"{bus_url}/v1/services")``)
      if the skill wants to overlay live-agent state. ``None`` skips the
      overlay cleanly.
    """

    view = discover_project(start_dir, sibling_prefix=sibling_prefix, domain=domain)
    if services_payload is not None:
        apply_live_overlay(view, services_payload)
    return view

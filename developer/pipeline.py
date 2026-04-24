"""Pipeline composition for the developer MCP server.

Mirrors khonliang-researcher's ``create_pipeline`` factory pattern but
holds developer-only stores and keeps :class:`ResearcherClient` limited
to evidence/context calls instead of sharing storage with researcher.

The :meth:`Pipeline.from_config` factory **enforces store isolation**:
it asserts that ``KnowledgeStore``, ``TripleStore`` and ``DigestStore``
all point at the resolved ``developer.db`` path and refuses to start if
any store points elsewhere. This is the runtime guarantee behind
acceptance criterion #9.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from khonliang.digest.store import DigestStore
from khonliang.knowledge.store import KnowledgeStore
from khonliang.knowledge.triples import TripleStore
from khonliang_researcher.doc_reader import LocalDocReader

from developer.bug_store import BugStore
from developer.config import Config
from developer.dogfood_store import DogfoodStore
from developer.fr_store import FRStore
from developer.milestone_store import MilestoneStore
from developer.project_store import ProjectStore
from developer.researcher_client import ResearcherClient
from developer.specs import FR_ID_PATTERN, SpecReader


class PipelineIsolationError(RuntimeError):
    """Raised when a store points at a DB other than developer.db.

    This guards spec rev 2's architectural decision: developer never
    shares a SQLite file with researcher. If construction violates that,
    the server refuses to start.
    """


@dataclass
class Pipeline:
    """Wired developer pipeline. Construct via :meth:`from_config`."""

    config: Config
    knowledge: KnowledgeStore
    triples: TripleStore
    digest: DigestStore
    reader: LocalDocReader
    specs: SpecReader
    researcher: ResearcherClient
    developer_guide_text: str
    frs: FRStore
    milestones: MilestoneStore
    bugs: BugStore
    dogfood: DogfoodStore
    projects: ProjectStore

    @classmethod
    def from_config(cls, config: Config) -> "Pipeline":
        """Wire all components from a loaded :class:`Config`.

        Steps:
          1. Construct the three stores against ``config.db_path``.
          2. **Assert store isolation** — every store's ``db_path`` must
             equal the resolved ``developer.db`` path. Refuse to start
             otherwise (acceptance #9).
          3. Construct the LocalDocReader, FRStore, ResearcherClient, SpecReader.
          4. Load ``prompts/developer_guide.md`` into ``developer_guide_text``
             at startup so the ``developer_guide`` MCP tool can return it
             without re-reading the file on every call.

        Note: ``ResearcherClient`` is wired here for researcher evidence
        calls in the MCP server path. The bus-agent path
        (``DeveloperAgent``) uses ``self.request()`` from bus-lib for
        cross-agent calls instead.
        """
        db_path = str(config.db_path)

        knowledge = KnowledgeStore(db_path)
        triples = TripleStore(db_path)
        digest = DigestStore(db_path)

        _assert_stores_isolated(
            expected=db_path, knowledge=knowledge, triples=triples, digest=digest
        )

        # FR_ID_PATTERN is imported from specs and applied to LocalDocReader so that
        # reference extraction doesn't pick up python identifiers like ``fr_status``
        # from prose. It matches only ``fr_<target>_<8 hex chars>``.
        reader = LocalDocReader(reference_pattern=FR_ID_PATTERN)
        frs = FRStore(knowledge=knowledge)
        # ResearcherClient remains only for evidence/context calls. FR
        # lifecycle and FR-id resolution are developer-owned.
        researcher = ResearcherClient(bus_url=config.bus.url or "http://localhost:8787")
        specs = SpecReader(
            reader=reader,
            projects=config.projects,
            fr_store=frs,
        )

        guide_text = _load_developer_guide(config.prompts_dir)

        milestones = MilestoneStore(knowledge=knowledge)

        # Tracking-infrastructure stores (Phase 1: CRUD-only slice).
        # Seed-on-construction writes curated entries from the FR bodies
        # on a fresh DB; subsequent inits are no-ops once the rows exist.
        bugs = BugStore(knowledge=knowledge)
        dogfood = DogfoodStore(knowledge=knowledge)

        # Project store (fr_developer_5d0a8711 Phase 2). Landed empty; the
        # multi-project productization path populates it via project_init
        # skills. Existing FR / milestone / spec / bug / dogfood records
        # remain project-implicit — Phase 3 migrates them.
        projects = ProjectStore(knowledge_store=knowledge)

        return cls(
            config=config,
            knowledge=knowledge,
            triples=triples,
            digest=digest,
            reader=reader,
            specs=specs,
            researcher=researcher,
            developer_guide_text=guide_text,
            frs=frs,
            milestones=milestones,
            bugs=bugs,
            dogfood=dogfood,
            projects=projects,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_stores_isolated(
    *,
    expected: str,
    knowledge: KnowledgeStore,
    triples: TripleStore,
    digest: DigestStore,
) -> None:
    """Refuse to start if any store points at a DB other than ``expected``.

    This is the runtime arm of spec rev 2's Architecture A — the static
    arm is the absence of any researcher.db reference in this codebase.
    Together they guarantee acceptance #9: no file handles are shared
    between developer and researcher processes.
    """
    expected_resolved = str(Path(expected).resolve())
    mismatches: list[str] = []
    for name, store in (
        ("knowledge", knowledge),
        ("triples", triples),
        ("digest", digest),
    ):
        actual = str(Path(store.db_path).resolve())
        if actual != expected_resolved:
            mismatches.append(f"{name}.db_path={actual!r}")
    if mismatches:
        raise PipelineIsolationError(
            "developer pipeline refused to start: stores point at unexpected "
            f"databases (expected {expected_resolved!r}). Mismatches: "
            + ", ".join(mismatches)
        )


def _load_developer_guide(prompts_dir: Path) -> str:
    """Load ``developer_guide.md`` if it exists; return placeholder otherwise.

    The server should still boot if a local checkout is missing prompt files.
    The ``developer_guide`` tool returns this placeholder so the failure is
    visible without blocking unrelated health checks.
    """
    guide_path = prompts_dir / "developer_guide.md"
    if guide_path.exists():
        return guide_path.read_text(encoding="utf-8")
    return (
        "# Developer guide unavailable\n\n"
        f"Expected at {guide_path}, but the file does not exist. "
        "This is the placeholder returned when prompts/developer_guide.md "
        "has not been created yet."
    )

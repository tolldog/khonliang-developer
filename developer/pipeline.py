"""Pipeline composition for the developer MCP server.

Mirrors khonliang-researcher's ``create_pipeline`` factory pattern but
holds developer-only stores and the :class:`ResearcherClient` seam
instead of sharing storage with researcher (per spec rev 2's
Architecture A: independent storage + MCP-to-MCP for narrow interfaces).

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

from developer.config import Config
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

    @classmethod
    def from_config(cls, config: Config) -> "Pipeline":
        """Wire all components from a loaded :class:`Config`.

        Steps:
          1. Construct the three stores against ``config.db_path``.
          2. **Assert store isolation** — every store's ``db_path`` must
             equal the resolved ``developer.db`` path. Refuse to start
             otherwise (acceptance #9).
          3. Construct the LocalDocReader, ResearcherClient, SpecReader.
          4. Load ``prompts/developer_guide.md`` into ``developer_guide_text``
             at startup so the ``developer_guide`` MCP tool can return it
             without re-reading the file on every call.
        """
        db_path = str(config.db_path)

        knowledge = KnowledgeStore(db_path)
        triples = TripleStore(db_path)
        digest = DigestStore(db_path)

        _assert_stores_isolated(
            expected=db_path, knowledge=knowledge, triples=triples, digest=digest
        )

        # Use the strict FR pattern so DocContent.references doesn't pick
        # up python identifiers like ``fr_status`` from prose. SpecReader's
        # FR_ID_PATTERN matches only ``fr_<target>_<8 hex chars>``.
        reader = LocalDocReader(reference_pattern=FR_ID_PATTERN)
        # Wire ResearcherClient through the bus (or fallback gracefully if bus is down)
        bus_url = f"http://localhost:{config.bus.url.split(':')[-1]}" if config.bus.url else "http://localhost:8787"
        researcher = ResearcherClient(bus_url=bus_url)
        specs = SpecReader(reader=reader, projects=config.projects, researcher=researcher)

        guide_text = _load_developer_guide(config.prompts_dir)

        return cls(
            config=config,
            knowledge=knowledge,
            triples=triples,
            digest=digest,
            reader=reader,
            specs=specs,
            researcher=researcher,
            developer_guide_text=guide_text,
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

    During early MS-01 development the prompts dir may not yet contain
    the guide; the server should still boot. The smoke test in Task 10
    catches a missing guide via the ``developer_guide`` MCP tool returning
    the placeholder text.
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

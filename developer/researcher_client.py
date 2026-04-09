"""Thin client wrapping researcher's data as remote objects.

This is the seam between developer and researcher per spec rev 2's
Architecture A. MS-01 stubs every method so the call paths are exercised
without any cross-app coupling. Real implementations land in:

- MS-02: ``get_paper_context`` (spec evaluation needs evidence)
- MS-02/03: ``get_fr`` / ``list_frs`` (FR lifecycle needs FR records)
- MS-03: ``update_fr_status`` (FR lifecycle status writes)
- MS-06: bus-backed cache invalidation on top of all of the above

The transport choice (MCP-to-MCP vs direct read) is deferred to MS-02.
The stub is transport-agnostic so changing it later does not require
touching any caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from developer.config import ResearcherMCPConfig


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class FRRecord:
    """A single feature request record as exposed to developer.

    Mirrors the shape researcher stores in its ``KnowledgeStore``
    (``Tier.DERIVED`` entries with ``"fr"`` and ``"target:<name>"`` tags,
    status in ``entry.metadata["fr_status"]``). MS-01 never instantiates
    one of these — the stub always returns ``None``. MS-02 will populate
    them from real lookups.
    """

    fr_id: str
    title: str
    target: str
    priority: str
    status: str  # open | planned | in_progress | review | completed
    description: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ResearcherClient:
    """MS-01 stub. Interface-complete; methods return placeholder values.

    The constructor accepts the parsed ``researcher_mcp`` config block but
    does **not** open any connection. This guarantees that booting the
    developer MCP server in MS-01 cannot accidentally reach into
    researcher's storage or process — the architectural isolation from
    spec rev 2 is enforced at the seam itself.
    """

    def __init__(self, config: ResearcherMCPConfig):
        self._config = config

    @property
    def config(self) -> ResearcherMCPConfig:
        return self._config

    async def get_fr(self, fr_id: str) -> FRRecord | None:
        """Look up a single FR by ID. MS-01 stub: always returns None.

        MS-02 wires this to either ``researcher.feature_requests`` over
        MCP or a direct ``KnowledgeStore.get(fr_id)`` against researcher's
        DB (transport TBD per spec §Open questions).
        """
        return None

    async def list_frs(self, target: str) -> list[FRRecord]:
        """List all FRs for a target project. MS-01 stub: always returns []."""
        return []

    async def get_paper_context(self, query: str, max_papers: int = 5) -> str:
        """Build a paper-context string for spec evaluation. MS-01 stub: empty."""
        return ""

    async def update_fr_status(self, fr_id: str, status: str) -> bool:
        """Promote/demote an FR through its lifecycle. Not in MS-01."""
        raise NotImplementedError(
            "ResearcherClient.update_fr_status lands in MS-03 (FR lifecycle)"
        )

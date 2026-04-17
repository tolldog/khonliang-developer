"""Client for researcher evidence/context calls through the bus.

Developer owns FR lifecycle locally. Researcher calls through this
client are limited to evidence/context retrieval, and all communication
goes through the bus.

When the bus isn't available (e.g., running developer standalone
for local spec reading), the client falls back to returning empty
results so local-only tools still work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class FRRecord:
    """A single feature request record as exposed to developer."""

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
    """Calls researcher evidence/context skills through the bus.

    Uses the bus's ``/v1/request`` endpoint to route requests to the
    researcher agent. The bus handles agent discovery, WebSocket routing,
    retry, and tracing.

    If the bus is unreachable, methods return empty/None results instead
    of crashing — this lets local-only developer tools (read_spec,
    list_specs) still work without the bus running.
    """

    def __init__(self, bus_url: str, researcher_id: str = "researcher-primary"):
        self.bus_url = bus_url.rstrip("/")
        self.researcher_id = researcher_id
        self._http = httpx.AsyncClient(timeout=30.0)

    async def _request(self, operation: str, args: dict[str, Any] | None = None) -> dict:
        """Send a request to the researcher agent via the bus."""
        try:
            r = await self._http.post(
                f"{self.bus_url}/v1/request",
                json={
                    "agent_id": self.researcher_id,
                    "operation": operation,
                    "args": args or {},
                    "timeout": 30,
                },
            )
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                logger.warning("Researcher request %s failed: %s", operation, data["error"])
                return {}
            return data.get("result", {})
        except Exception as e:
            logger.warning("Bus unreachable for %s: %s", operation, e)
            return {}

    async def get_paper_context(self, query: str, max_papers: int = 5) -> str:
        """Build a paper-context string for spec evaluation."""
        result = await self._request("paper_context", {
            "query": query,
            "detail": "full",
            "max_results": max_papers,
        })
        text = result.get("result", "") if isinstance(result, dict) else str(result)
        return text

    async def close(self) -> None:
        await self._http.aclose()

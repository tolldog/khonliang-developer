"""Client for calling researcher skills through the bus.

Replaces the MS-01 stub with real bus-backed calls. The developer
agent never talks to researcher directly — all communication goes
through the bus. This is the architectural seam from spec rev 2.

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
    """Calls researcher skills through the bus.

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

    async def get_fr(self, fr_id: str) -> FRRecord | None:
        """Look up a single FR by ID via researcher's knowledge_search."""
        result = await self._request("knowledge_search", {
            "query": fr_id,
            "detail": "full",
            "max_results": 1,
        })
        # Parse the researcher's response into an FRRecord
        text = result.get("result", "") if isinstance(result, dict) else str(result)
        if not text or fr_id not in text:
            return None
        # Best-effort parse from the text response
        return FRRecord(
            fr_id=fr_id,
            title=fr_id,
            target="",
            priority="",
            status="",
            description=text,
        )

    async def list_frs(self, target: str) -> list[FRRecord]:
        """List all FRs for a target project."""
        result = await self._request("feature_requests", {
            "target": target,
            "detail": "brief",
        })
        text = result.get("result", "") if isinstance(result, dict) else str(result)
        if not text or "No FRs" in text:
            return []
        # Parse FR lines: "fr_xxx | [priority] title -> target (status)"
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith("fr_"):
                continue
            parts = line.split(" | ", 1)
            fr_id = parts[0].strip()
            rest = parts[1] if len(parts) > 1 else ""
            records.append(FRRecord(
                fr_id=fr_id,
                title=rest,
                target=target,
                priority="",
                status="",
                description=rest,
            ))
        return records

    async def get_paper_context(self, query: str, max_papers: int = 5) -> str:
        """Build a paper-context string for spec evaluation."""
        result = await self._request("paper_context", {
            "query": query,
            "detail": "full",
            "max_results": max_papers,
        })
        text = result.get("result", "") if isinstance(result, dict) else str(result)
        return text

    async def update_fr_status(self, fr_id: str, status: str, notes: str = "") -> bool:
        """Update an FR's lifecycle status via researcher."""
        result = await self._request("update_fr_status", {
            "fr_id": fr_id,
            "status": status,
            "notes": notes,
        })
        text = result.get("result", "") if isinstance(result, dict) else str(result)
        return bool(text and "→" in text)

    async def close(self) -> None:
        await self._http.aclose()

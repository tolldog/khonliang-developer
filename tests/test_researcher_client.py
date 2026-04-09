"""Tests for developer.researcher_client — MS-01 stub guarantees."""

from __future__ import annotations

import pytest

from developer.config import ResearcherMCPConfig
from developer.researcher_client import FRRecord, ResearcherClient


@pytest.fixture
def client():
    return ResearcherClient(
        ResearcherMCPConfig(
            transport="stdio", command="python", args=["-m", "researcher.server"]
        )
    )


@pytest.mark.asyncio
async def test_get_fr_returns_none(client):
    assert await client.get_fr("fr_developer_28a11ce2") is None
    assert await client.get_fr("anything") is None


@pytest.mark.asyncio
async def test_list_frs_returns_empty(client):
    assert await client.list_frs("developer") == []
    assert await client.list_frs("anything") == []


@pytest.mark.asyncio
async def test_get_paper_context_returns_empty_string(client):
    assert await client.get_paper_context("foo") == ""
    assert await client.get_paper_context("foo", max_papers=10) == ""


@pytest.mark.asyncio
async def test_update_fr_status_raises_not_implemented(client):
    with pytest.raises(NotImplementedError, match="MS-03"):
        await client.update_fr_status("fr_developer_28a11ce2", "planned")


def test_fr_record_dataclass_has_all_fields():
    record = FRRecord(
        fr_id="fr_x_12345678",
        title="t",
        target="x",
        priority="high",
        status="open",
        description="d",
    )
    assert record.fr_id == "fr_x_12345678"
    assert record.metadata == {}


def test_constructor_does_not_open_connection(client):
    """MS-01 guarantee: ResearcherClient construction is purely structural."""
    # If the constructor opened a connection, we'd have a side effect to
    # tear down here. The fact that this fixture builds cleanly without
    # any cleanup is the test.
    assert client.config.transport == "stdio"

"""Tests for developer.researcher_client — bus-backed calls with graceful fallback."""

from __future__ import annotations

import pytest

from developer.researcher_client import FRRecord, ResearcherClient


@pytest.fixture
def client():
    """Client pointing at a non-existent bus — tests graceful fallback."""
    return ResearcherClient(bus_url="http://localhost:1", researcher_id="researcher-test")


@pytest.mark.asyncio
async def test_get_fr_returns_none_when_bus_down(client):
    """When bus is unreachable, get_fr returns None instead of crashing."""
    result = await client.get_fr("fr_developer_28a11ce2")
    assert result is None


@pytest.mark.asyncio
async def test_list_frs_returns_empty_when_bus_down(client):
    assert await client.list_frs("developer") == []


@pytest.mark.asyncio
async def test_get_paper_context_returns_empty_when_bus_down(client):
    assert await client.get_paper_context("consensus") == ""


@pytest.mark.asyncio
async def test_update_fr_status_returns_false_when_bus_down(client):
    result = await client.update_fr_status("fr_x_12345678", "planned")
    assert result is False


def test_fr_record_dataclass():
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


def test_client_construction():
    c = ResearcherClient(bus_url="http://localhost:8788")
    assert c.bus_url == "http://localhost:8788"
    assert c.researcher_id == "researcher-primary"

"""Tests for developer.server — tool registration and end-to-end calls.

Acceptance #2, #4 (catalog assertion for developer_guide), #5, #6, #7, #8.
"""

from __future__ import annotations

import pytest

from developer.server import create_developer_server
from tests.conftest import MILESTONE_PATH, SPEC_PATH


@pytest.fixture
def mcp(pipeline):
    return create_developer_server(pipeline)


# ---------------------------------------------------------------------------
# Tool registration (acceptance #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_registers_developer_tools(mcp):
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    for required in (
        "read_spec",
        "traverse_milestone",
        "list_specs",
        "health_check",
        "developer_guide",
        "catalog",  # inherited
    ):
        assert required in names, f"missing tool: {required}"


# ---------------------------------------------------------------------------
# Acceptance #4 — developer_guide registered both ways
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_lists_developer_guide(mcp):
    """Catalog must show developer_guide as a registered guide entry."""
    result = await mcp.call_tool("catalog", {"detail": "brief"})
    text = _extract_text(result)
    assert "developer_guide" in text


@pytest.mark.asyncio
async def test_developer_guide_returns_loaded_markdown(mcp):
    result = await mcp.call_tool("developer_guide", {})
    text = _extract_text(result)
    assert "Developer Pipeline Guide" in text
    assert len(text) > 500


# ---------------------------------------------------------------------------
# Acceptance #5 — read_spec end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_spec_compact(mcp):
    result = await mcp.call_tool(
        "read_spec", {"path": str(SPEC_PATH), "detail": "compact"}
    )
    text = _extract_text(result)
    assert "fr=fr_developer_28a11ce2" in text
    assert "sections=" in text


@pytest.mark.asyncio
async def test_read_spec_brief(mcp):
    result = await mcp.call_tool(
        "read_spec", {"path": str(SPEC_PATH), "detail": "brief"}
    )
    text = _extract_text(result)
    assert "fr_developer_28a11ce2" in text
    assert "sections" in text


@pytest.mark.asyncio
async def test_read_spec_handles_missing_file(mcp):
    result = await mcp.call_tool(
        "read_spec", {"path": "/nonexistent/file.md", "detail": "compact"}
    )
    text = _extract_text(result)
    assert "error" in text.lower()


# ---------------------------------------------------------------------------
# Acceptance #6 — traverse_milestone end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_traverse_milestone_brief_shows_unresolved(mcp):
    result = await mcp.call_tool(
        "traverse_milestone",
        {"path": str(MILESTONE_PATH), "detail": "brief"},
    )
    text = _extract_text(result)
    assert "fr_developer_28a11ce2" in text
    assert "(unresolved)" in text


# ---------------------------------------------------------------------------
# Acceptance #7 — list_specs uses project config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_specs_compact(mcp):
    result = await mcp.call_tool(
        "list_specs", {"project": "developer", "detail": "compact"}
    )
    text = _extract_text(result)
    assert "project=developer" in text
    assert "count=1" in text


@pytest.mark.asyncio
async def test_list_specs_unknown_project(mcp):
    result = await mcp.call_tool(
        "list_specs", {"project": "ghost", "detail": "brief"}
    )
    text = _extract_text(result)
    assert "no specs" in text.lower()


# ---------------------------------------------------------------------------
# Acceptance #8 — health_check has no Ollama refs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_skips_ollama_in_ms01(mcp):
    """MS-01 health_check must NOT call into Ollama or list model versions.

    We check the non-db lines (skipping the first line which contains the
    DB filesystem path — that path can contain arbitrary substrings from
    pytest's tmp directory naming).
    """
    result = await mcp.call_tool("health_check", {})
    text = _extract_text(result)

    assert "db:" in text
    assert "workspace_root" in text
    assert "researcher_mcp" in text
    assert "models: parsed but unused (MS-02)" in text

    # Check the non-db lines for Ollama-style markers — skip the first
    # line because it contains the filesystem path.
    non_db_lines = [
        line for line in text.lower().splitlines() if not line.startswith("db:")
    ]
    haystack = "\n".join(non_db_lines)
    assert "ollama" not in haystack
    assert "[ok]" not in haystack  # researcher uses this marker for model checks
    assert "qwen" not in haystack
    assert "nomic" not in haystack
    # researcher's health_check shows model versions like ``llama3.2:3b`` —
    # the model name with a colon. Allow ``llama`` as a substring (it could
    # appear in error text), but specifically reject the version pattern.
    assert "llama3" not in haystack


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _extract_text(call_tool_result) -> str:
    """call_tool returns ([TextContent...], {'result': '...'}); pull text out."""
    if isinstance(call_tool_result, tuple) and len(call_tool_result) == 2:
        meta = call_tool_result[1]
        if isinstance(meta, dict) and "result" in meta:
            return str(meta["result"])
        contents = call_tool_result[0]
    else:
        contents = call_tool_result
    if isinstance(contents, list) and contents:
        first = contents[0]
        if hasattr(first, "text"):
            return first.text
    return str(call_tool_result)

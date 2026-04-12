"""Tests for the native DeveloperAgent using the bus-lib testing harness."""

from __future__ import annotations

import pytest
from khonliang_bus.testing import AgentTestHarness
from developer.agent import DeveloperAgent


REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
CONFIG_PATH = str(REPO_ROOT / "config.yaml")


@pytest.fixture
def harness():
    return AgentTestHarness(DeveloperAgent, config_path=CONFIG_PATH)


# -- skills --

def test_skill_count(harness):
    assert len(harness.skills) == 5


def test_skills_registered(harness):
    expected = {"read_spec", "list_specs", "traverse_milestone", "health_check", "developer_guide"}
    assert harness.skill_names == expected


def test_read_spec_skill(harness):
    harness.assert_skill_exists("read_spec", description="spec file")


def test_list_specs_skill(harness):
    harness.assert_skill_exists("list_specs", description="Discover")


def test_traverse_milestone_skill(harness):
    harness.assert_skill_exists("traverse_milestone", description="milestone")


# -- collaborations --

def test_collaborations_declared(harness):
    assert "evaluate_spec_against_corpus" in harness.collaboration_names
    assert "full_fr_review" in harness.collaboration_names


def test_evaluate_spec_requires_researcher(harness):
    harness.assert_collaboration_exists(
        "evaluate_spec_against_corpus",
        requires={"researcher": ">=0.1.0"},
    )


def test_full_fr_review_requires_researcher(harness):
    harness.assert_collaboration_exists(
        "full_fr_review",
        requires={"researcher": ">=0.1.0"},
    )


# -- handlers --

@pytest.mark.asyncio
async def test_read_spec_handler(harness):
    from tests.conftest import SPEC_PATH
    result = await harness.call("read_spec", {"path": str(SPEC_PATH)})
    assert result["fr"] == "fr_developer_28a11ce2"
    assert result["section_count"] > 0
    assert "MS-01" in result["title"]


@pytest.mark.asyncio
async def test_list_specs_handler(harness):
    result = await harness.call("list_specs", {"project": "developer"})
    assert result["count"] >= 1
    assert result["project"] == "developer"


@pytest.mark.asyncio
async def test_list_specs_unknown_project(harness):
    result = await harness.call("list_specs", {"project": "nonexistent"})
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_traverse_milestone_handler(harness):
    from tests.conftest import MILESTONE_PATH
    result = await harness.call("traverse_milestone", {"path": str(MILESTONE_PATH)})
    assert "fr_developer_28a11ce2" in result["fr"]
    assert len(result["specs"]) >= 1
    assert len(result["frs"]) >= 1


@pytest.mark.asyncio
async def test_health_check_handler(harness):
    result = await harness.call("health_check", {})
    assert "db_path" in result
    assert "projects" in result
    assert result["agent_id"] == "developer-test"


@pytest.mark.asyncio
async def test_developer_guide_handler(harness):
    result = await harness.call("developer_guide", {})
    assert "Developer Pipeline Guide" in result["guide"]


# -- structured returns --

@pytest.mark.asyncio
async def test_read_spec_returns_dict(harness):
    """Native handlers return structured dicts, not MCP-formatted strings."""
    from tests.conftest import SPEC_PATH
    result = await harness.call("read_spec", {"path": str(SPEC_PATH)})
    assert isinstance(result, dict)
    assert isinstance(result["sections"], list)
    assert isinstance(result["references"], list)


# -- registration --

def test_registration_metadata(harness):
    reg = harness.registration
    assert reg.agent_type == "developer"
    assert len(reg.skills) == 5
    assert len(reg.collaborations) == 2

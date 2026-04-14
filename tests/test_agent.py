"""Tests for the native DeveloperAgent using the bus-lib testing harness."""

from __future__ import annotations

import pytest
from khonliang_bus.testing import AgentTestHarness
from developer.agent import DeveloperAgent


@pytest.fixture
def harness(temp_config_file):
    return AgentTestHarness(DeveloperAgent, config_path=str(temp_config_file()))


# -- skills --

def test_skill_count(harness):
    # 11 (MS-01 skills + bus proxies) + 5 FR-lifecycle (PR #10) + 1 merge_frs
    # (PR #11) + 5 git_* (PR #12) + 2 PM edit/picker (this PR) = 24
    assert len(harness.skills) == 24


def test_skills_registered(harness):
    expected = {
        "read_spec", "list_specs", "traverse_milestone",
        "health_check", "developer_guide",
        "get_fr", "list_frs", "get_paper_context",
        "next_work_unit", "work_units",
        "run_tests",
        # developer-owned FR lifecycle (PR #10)
        "promote_fr", "update_fr_status", "set_fr_dependency",
        "get_fr_local", "list_frs_local",
        # merge write op (PR #11)
        "merge_frs",
        # in-place edit + next-to-work picker (this PR)
        "update_fr", "next_fr_local",
        # native git operations (fr_developer_e778b9bf)
        "git_status", "git_log", "git_diff", "git_branches", "git_commit",
    }
    assert harness.skill_names == expected


def test_read_spec_skill(harness):
    harness.assert_skill_exists("read_spec", description="spec file")


def test_list_specs_skill(harness):
    harness.assert_skill_exists("list_specs", description="Discover")


def test_traverse_milestone_skill(harness):
    harness.assert_skill_exists("traverse_milestone", description="milestone")


def test_get_fr_skill(harness):
    harness.assert_skill_exists("get_fr", description="researcher")


def test_list_frs_skill(harness):
    harness.assert_skill_exists("list_frs", description="researcher")


def test_get_paper_context_skill(harness):
    harness.assert_skill_exists("get_paper_context", description="researcher")


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


# -- git_* handler tests (fr_developer_e778b9bf) --
#
# Integration-style: use a temp git repo fixture and drive the full handler
# path (arg parsing, GitClient invocation, error mapping, dict return).


@pytest.fixture
def git_repo(tmp_path):
    # Use a subdir so we don't collide with the temp_config_file fixture
    # which writes config.yaml into the same tmp_path; git would see it
    # as untracked and the status tests would fail spuriously.
    import subprocess as _sub
    repo_dir = tmp_path / "gitrepo"
    repo_dir.mkdir()

    def _g(*args):
        _sub.run(["git", *args], cwd=str(repo_dir), check=True,
                 stdout=_sub.DEVNULL, stderr=_sub.DEVNULL)

    _g("init", "-b", "main")
    _g("config", "user.email", "t@example.com")
    _g("config", "user.name", "T")
    (repo_dir / "a.txt").write_text("first\n")
    _g("add", "a.txt")
    _g("commit", "-m", "initial")
    return repo_dir


@pytest.mark.asyncio
async def test_git_status_handler_clean(harness, git_repo):
    result = await harness.call("git_status", {"cwd": str(git_repo)})
    assert result["branch"] == "main"
    assert result["is_dirty"] is False
    assert result["modified"] == []


@pytest.mark.asyncio
async def test_git_status_handler_requires_cwd(harness):
    result = await harness.call("git_status", {"cwd": ""})
    assert "error" in result
    assert "cwd is required" in result["error"]


@pytest.mark.asyncio
async def test_git_status_handler_reports_error_for_non_repo(harness, tmp_path):
    result = await harness.call("git_status", {"cwd": str(tmp_path)})
    assert "error" in result
    assert "not a git repository" in result["error"]


@pytest.mark.asyncio
async def test_git_log_handler(harness, git_repo):
    result = await harness.call("git_log", {"cwd": str(git_repo), "limit": 5})
    assert result["count"] == 1
    assert result["commits"][0]["message"] == "initial"


@pytest.mark.asyncio
async def test_git_log_handler_non_int_limit_defaults(harness, git_repo):
    """Non-integer limit shouldn't crash the handler."""
    result = await harness.call("git_log", {"cwd": str(git_repo), "limit": "not-a-number"})
    assert "commits" in result  # returned successfully with default


@pytest.mark.asyncio
async def test_git_branches_handler(harness, git_repo):
    result = await harness.call("git_branches", {"cwd": str(git_repo)})
    assert result["count"] >= 1
    names = {b["name"] for b in result["branches"]}
    assert "main" in names


@pytest.mark.asyncio
async def test_git_diff_handler(harness, git_repo):
    (git_repo / "a.txt").write_text("changed\n")
    result = await harness.call("git_diff", {"cwd": str(git_repo)})
    assert "a.txt" in result["diff"]


@pytest.mark.asyncio
async def test_git_commit_handler(harness, git_repo):
    (git_repo / "b.txt").write_text("new\n")
    import subprocess as _sub
    _sub.run(["git", "add", "b.txt"], cwd=str(git_repo), check=True)
    result = await harness.call("git_commit", {
        "cwd": str(git_repo), "message": "add b",
    })
    assert result["message"] == "add b"
    assert result["sha"]


@pytest.mark.asyncio
async def test_git_commit_handler_empty_staging_returns_error(harness, git_repo):
    result = await harness.call("git_commit", {
        "cwd": str(git_repo), "message": "nothing",
    })
    assert "error" in result
    assert "no staged" in result["error"].lower()


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


@pytest.mark.asyncio
async def test_read_spec_full_detail(harness):
    """detail='full' adds 'text' key with the raw markdown body."""
    from tests.conftest import SPEC_PATH
    result = await harness.call("read_spec", {"path": str(SPEC_PATH), "detail": "full"})
    assert "text" in result
    assert isinstance(result["text"], str)
    assert len(result["text"]) > 0


@pytest.mark.asyncio
async def test_read_spec_brief_detail_omits_text(harness):
    """default (brief) detail does not include the raw body."""
    from tests.conftest import SPEC_PATH
    result = await harness.call("read_spec", {"path": str(SPEC_PATH)})
    assert "text" not in result


# -- registration --

def test_registration_metadata(harness):
    reg = harness.registration
    assert reg.agent_type == "developer"
    assert len(reg.skills) == 24
    assert len(reg.collaborations) == 2


# -- cluster parsing + ranking --

def test_parse_clusters():
    from developer.agent import DeveloperAgent
    agent = DeveloperAgent(agent_id="test", bus_url="http://x", config_path="")
    text = """# FR Clusters (3 clusters, 10 FRs)

## Cluster 1 (4 FRs, targets: khonliang,developer)
  [fr_a_12345678] Implement AutoGen Framework → khonliang [high]
  [fr_b_12345678] Build Developer AutoGen → developer [high]
  [fr_c_12345678] Create Template → developer [medium]
  [fr_d_12345678] Domain Agents → developer [medium]

## Cluster 2 (3 FRs, targets: khonliang)
  [fr_e_12345678] EvoAgent Generation → khonliang [high]
  [fr_f_12345678] EvoAgent Optimization → khonliang [medium]
  [fr_g_12345678] Dynamic Optimization → khonliang [medium]

## Cluster 3 (3 FRs, targets: khonliang)
  [fr_h_12345678] GRA Framework → khonliang [high]
  [fr_i_12345678] GRA Collaborative → khonliang [medium]
  [fr_j_12345678] GRA Synthesis → khonliang [medium]
"""
    units = agent._parse_and_rank_clusters(text, "developer")
    assert len(units) == 3
    # Cluster 1 should rank highest: same max priority (high), largest (4), most targets (2), matches target
    assert units[0]["size"] == 4
    assert "developer" in units[0]["targets"]
    assert units[0]["rank"] == 1


def test_parse_clusters_ranks_by_priority():
    from developer.agent import DeveloperAgent
    agent = DeveloperAgent(agent_id="test", bus_url="http://x", config_path="")
    text = """## Cluster 1 (2 FRs, targets: khonliang)
  [fr_a_12345678] Low thing → khonliang [low]
  [fr_b_12345678] Low thing 2 → khonliang [low]

## Cluster 2 (2 FRs, targets: khonliang)
  [fr_c_12345678] High thing → khonliang [high]
  [fr_d_12345678] Medium thing → khonliang [medium]
"""
    units = agent._parse_and_rank_clusters(text, "")
    assert units[0]["max_priority"] == "high"
    assert units[1]["max_priority"] == "low"


def test_parse_empty_clusters():
    from developer.agent import DeveloperAgent
    agent = DeveloperAgent(agent_id="test", bus_url="http://x", config_path="")
    units = agent._parse_and_rank_clusters("No clusters found", "")
    assert units == []


def test_work_units_skill_exists(harness):
    harness.assert_skill_exists("work_units")


def test_next_work_unit_skill_exists(harness):
    harness.assert_skill_exists("next_work_unit")


# -- handler flow tests (mock self.request) --

@pytest.mark.asyncio
async def test_work_units_with_clusters(harness):
    """work_units returns ranked clusters when researcher provides them."""
    cluster_response = {"result": {"result": """## Cluster 1 (3 FRs, targets: khonliang)
  [fr_a_12345678] Thing A → khonliang [high]
  [fr_b_12345678] Thing B → khonliang [medium]
  [fr_c_12345678] Thing C → khonliang [low]
"""}}

    async def mock_request(**kwargs):
        if kwargs.get("operation") == "cluster_frs":
            return cluster_response
        return {"result": {}}

    harness.agent.request = mock_request
    result = await harness.call("work_units", {"target": "khonliang"})
    assert result["source"] == "clusters"
    assert result["count"] == 1
    assert result["work_units"][0]["size"] == 3


@pytest.mark.asyncio
async def test_work_units_falls_back_to_flat(harness):
    """work_units falls back to flat FR list when no clusters found."""
    async def mock_request(**kwargs):
        if kwargs.get("operation") == "cluster_frs":
            return {"result": {"result": "No clusters of 2+ FRs at 85% threshold."}}
        if kwargs.get("operation") == "feature_requests":
            return {"result": {"result": "fr_x | [high] Some FR"}}
        return {"result": {}}

    harness.agent.request = mock_request
    result = await harness.call("work_units", {"target": "developer"})
    assert result["source"] == "flat_list"


@pytest.mark.asyncio
async def test_next_work_unit_returns_top(harness):
    """next_work_unit returns the highest-ranked cluster."""
    cluster_response = {"result": {"result": """## Cluster 1 (2 FRs, targets: khonliang)
  [fr_a_12345678] Low → khonliang [low]
  [fr_b_12345678] Low → khonliang [low]

## Cluster 2 (2 FRs, targets: developer)
  [fr_c_12345678] High → developer [high]
  [fr_d_12345678] Med → developer [medium]
"""}}

    async def mock_request(**kwargs):
        return cluster_response

    harness.agent.request = mock_request
    result = await harness.call("next_work_unit", {"target": "developer"})
    assert "work_unit" in result
    assert result["work_unit"]["max_priority"] == "high"
    assert result["remaining"] == 1


@pytest.mark.asyncio
async def test_work_units_no_clusters_and_no_frs(harness):
    """Returns source='none' with error when both cluster and FR requests fail."""
    async def mock_request(**kwargs):
        if kwargs.get("operation") == "cluster_frs":
            return {"result": None}  # empty result, no exception
        raise RuntimeError("researcher unavailable")

    harness.agent.request = mock_request
    result = await harness.call("work_units", {})
    assert result["source"] == "none"
    assert "error" in result


@pytest.mark.asyncio
async def test_next_work_unit_no_units_returns_error(harness):
    """next_work_unit returns an error dict when no work units are available."""
    async def mock_request(**kwargs):
        if kwargs.get("operation") == "cluster_frs":
            return {"result": None}
        raise RuntimeError("no FRs")

    harness.agent.request = mock_request
    result = await harness.call("next_work_unit", {})
    assert "error" in result

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
    # 34 existing skills + 6 milestone/handoff skills + concept FR candidates.
    assert len(harness.skills) == 41


def test_skills_registered(harness):
    expected = {
        "read_spec", "list_specs", "traverse_milestone",
        "health_check", "developer_guide",
        "get_fr", "list_frs", "get_paper_context",
        "fr_candidates_from_concepts",
        "next_work_unit", "work_units",
        "propose_milestone_from_work_unit", "get_milestone",
        "list_milestones", "draft_spec_from_milestone", "review_milestone_scope",
        "prepare_development_handoff",
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
        "git_stage", "git_unstage", "git_checkout", "git_create_branch",
        "git_delete_branch", "git_fetch", "git_pull", "git_push",
        "git_show", "git_rev_parse",
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


def test_fr_candidates_from_concepts_skill(harness):
    harness.assert_skill_exists("fr_candidates_from_concepts", description="concept bundles")


def test_milestone_skills(harness):
    harness.assert_skill_exists("propose_milestone_from_work_unit", description="milestone")
    harness.assert_skill_exists("get_milestone", description="milestone")
    harness.assert_skill_exists("list_milestones", description="milestone")
    harness.assert_skill_exists("draft_spec_from_milestone", description="draft spec")
    harness.assert_skill_exists("review_milestone_scope", description="duplicate")
    harness.assert_skill_exists("prepare_development_handoff", description="handoff")


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


@pytest.mark.asyncio
async def test_git_stage_and_unstage_handlers(harness, git_repo):
    (git_repo / "stage-me.txt").write_text("x\n")
    staged = await harness.call("git_stage", {
        "cwd": str(git_repo),
        "paths": "stage-me.txt",
    })
    assert staged["staged"] == ["stage-me.txt"]

    status = await harness.call("git_status", {"cwd": str(git_repo)})
    assert "stage-me.txt" in status["staged"]

    unstaged = await harness.call("git_unstage", {
        "cwd": str(git_repo),
        "paths": ["stage-me.txt"],
    })
    assert unstaged["unstaged"] == ["stage-me.txt"]

    status = await harness.call("git_status", {"cwd": str(git_repo)})
    assert "stage-me.txt" in status["untracked"]


@pytest.mark.asyncio
async def test_git_checkout_create_and_delete_branch_handlers(harness, git_repo):
    created = await harness.call("git_create_branch", {
        "cwd": str(git_repo),
        "name": "feat/created",
    })
    assert created["branch"] == "feat/created"

    checked = await harness.call("git_checkout", {
        "cwd": str(git_repo),
        "ref": "feat/switched",
        "new_branch": True,
    })
    assert checked["branch"] == "feat/switched"

    await harness.call("git_checkout", {"cwd": str(git_repo), "ref": "main"})
    deleted = await harness.call("git_delete_branch", {
        "cwd": str(git_repo),
        "name": "feat/switched",
    })
    assert deleted == {"deleted": "feat/switched", "force": False}


@pytest.mark.asyncio
async def test_git_show_and_rev_parse_handlers(harness, git_repo):
    shown = await harness.call("git_show", {"cwd": str(git_repo), "ref": "HEAD"})
    assert shown["message"] == "initial"
    assert shown["sha"]

    parsed = await harness.call("git_rev_parse", {"cwd": str(git_repo), "ref": "HEAD"})
    assert parsed["ref"] == "HEAD"
    assert parsed["sha"] == shown["sha"]


@pytest.mark.asyncio
async def test_git_fetch_handler_with_local_remote(harness, git_repo, tmp_path):
    import subprocess as _sub

    remote = tmp_path / "remote.git"
    _sub.run(["git", "init", "--bare", str(remote)], check=True,
             stdout=_sub.DEVNULL, stderr=_sub.DEVNULL)
    _sub.run(["git", "remote", "add", "origin", str(remote)], cwd=str(git_repo), check=True)

    result = await harness.call("git_fetch", {"cwd": str(git_repo)})
    assert result == {"remote": "origin"}


@pytest.mark.asyncio
async def test_git_pull_and_push_handlers(harness, git_repo, monkeypatch):
    from git.cmd import Git as _GitCmd

    pull_calls: list[tuple] = []
    push_calls: list[tuple] = []

    def fake_pull(self_git, *args, **kwargs):
        pull_calls.append(args)
        return ""

    def fake_push(self_git, *args, **kwargs):
        push_calls.append(args)
        return ""

    monkeypatch.setattr(_GitCmd, "pull", fake_pull, raising=False)
    monkeypatch.setattr(_GitCmd, "push", fake_push, raising=False)

    pulled = await harness.call("git_pull", {
        "cwd": str(git_repo),
        "remote": "origin",
        "branch": "main",
    })
    assert pulled["branch"] == "main"
    assert pull_calls == [("--ff-only", "origin", "main")]

    pushed = await harness.call("git_push", {
        "cwd": str(git_repo),
        "remote": "origin",
        "branch": "main",
        "set_upstream": True,
    })
    assert pushed == {
        "remote": "origin",
        "branch": "main",
        "force": False,
        "set_upstream": True,
    }
    assert push_calls == [("-u", "origin", "main")]


@pytest.mark.asyncio
async def test_git_stage_handler_requires_paths(harness, git_repo):
    result = await harness.call("git_stage", {"cwd": str(git_repo), "paths": ""})
    assert "error" in result
    assert "paths are required" in result["error"]


@pytest.mark.asyncio
async def test_git_stage_handler_treats_none_paths_as_missing(harness, git_repo):
    result = await harness.call("git_stage", {"cwd": str(git_repo), "paths": None})
    assert "error" in result
    assert "paths are required" in result["error"]


@pytest.mark.asyncio
async def test_git_push_rejects_implicit_detached_head(harness, git_repo, monkeypatch):
    import subprocess as _sub
    from git.cmd import Git as _GitCmd

    push_calls: list[tuple] = []

    def fake_push(self_git, *args, **kwargs):
        push_calls.append(args)
        return ""

    monkeypatch.setattr(_GitCmd, "push", fake_push, raising=False)
    _sub.run(["git", "checkout", "--detach", "HEAD"], cwd=str(git_repo), check=True,
             stdout=_sub.DEVNULL, stderr=_sub.DEVNULL)

    result = await harness.call("git_push", {
        "cwd": str(git_repo),
        "remote": "origin",
    })
    assert "error" in result
    assert "detached HEAD" in result["error"]
    assert push_calls == []


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
    assert len(reg.skills) == 41
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


def test_parse_concept_bundles():
    from developer.agent import _parse_concept_bundles
    text = """2 concept bundles from 10 papers

Multi-Agent Systems for LLMs (strength: 100%)
  concepts: multi-agent, AutoGen
  Leveraging multi-agent systems to improve LLM performance.

Thought Communication (strength: 60%)
  concepts: thought communication
  Improving multi-agent conversations.
"""
    bundles = _parse_concept_bundles(text)

    assert bundles == [
        {
            "title": "Multi-Agent Systems for LLMs",
            "strength": 100,
            "concepts": ["multi-agent", "AutoGen"],
            "summary": "Leveraging multi-agent systems to improve LLM performance.",
        },
        {
            "title": "Thought Communication",
            "strength": 60,
            "concepts": ["thought communication"],
            "summary": "Improving multi-agent conversations.",
        },
    ]


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


@pytest.mark.asyncio
async def test_fr_candidates_from_concepts_returns_fr_aware_diff(harness):
    harness.agent.pipeline.frs.promote(
        target="developer",
        title="Existing Multi-Agent Workflow",
        description="Apply multi-agent systems to developer workflow",
        priority="high",
        concept="multi-agent",
    )
    concept_response = {"result": {"result": """2 concept bundles from 10 papers

Multi-Agent Systems for LLMs (strength: 100%)
  concepts: multi-agent, AutoGen
  Leveraging multi-agent systems to improve LLM performance.

Thought Communication (strength: 60%)
  concepts: thought communication
  Improving multi-agent conversations.
"""}}

    async def mock_request(**kwargs):
        assert kwargs["operation"] == "synergize_concepts"
        assert kwargs["timeout"] == 90
        return concept_response

    harness.agent.request = mock_request
    result = await harness.call("fr_candidates_from_concepts", {"target": "developer"})

    assert result["source"] == "researcher.synergize_concepts"
    assert result["bundle_count"] == 2
    assert result["new_count"] == 1
    assert result["existing_match_count"] == 1
    assert result["candidates"][0]["status"] == "existing_match"
    assert result["candidates"][0]["existing_matches"][0]["shared_terms"] == [
        "agent",
        "multi",
        "systems",
    ]
    assert result["candidates"][1]["status"] == "new_candidate"
    assert result["candidates"][1]["priority"] == "medium"


@pytest.mark.asyncio
async def test_fr_candidates_from_concepts_allows_timeout_override(harness):
    concept_response = {"result": {"result": ""}}
    seen = {}

    async def mock_request(**kwargs):
        seen.update(kwargs)
        return concept_response

    harness.agent.request = mock_request
    await harness.call("fr_candidates_from_concepts", {"timeout_s": 12})

    assert seen["timeout"] == 12


@pytest.mark.asyncio
async def test_fr_candidates_from_concepts_rejects_bad_timeout(harness):
    result = await harness.call("fr_candidates_from_concepts", {"timeout_s": "slow"})

    assert result == {"error": "timeout_s must be a number, got 'slow'"}


@pytest.mark.asyncio
async def test_fr_candidates_from_concepts_rejects_non_positive_timeout(harness):
    result = await harness.call("fr_candidates_from_concepts", {"timeout_s": 0})

    assert result == {"error": "timeout_s must be greater than 0, got 0"}


@pytest.mark.asyncio
async def test_propose_milestone_from_top_work_unit(harness):
    cluster_response = {"result": {"result": """## Cluster 1 (2 FRs, targets: developer)
  [fr_developer_11111111] Milestone entity → developer [high]
  [fr_developer_22222222] Draft spec artifact → developer [medium]
"""}}

    async def mock_request(**kwargs):
        return cluster_response

    harness.agent.request = mock_request
    result = await harness.call("propose_milestone_from_work_unit", {"target": "developer"})

    milestone = result["milestone"]
    assert milestone["id"].startswith("ms_developer_")
    assert milestone["target"] == "developer"
    assert milestone["fr_ids"] == ["fr_developer_11111111", "fr_developer_22222222"]
    assert "Draft spec artifact" in milestone["draft_spec"]

    listed = await harness.call("list_milestones", {"target": "developer"})
    assert listed["count"] == 1
    assert listed["milestones"][0]["id"] == milestone["id"]

    fetched = await harness.call("get_milestone", {"milestone_id": milestone["id"]})
    assert fetched["milestone"]["id"] == milestone["id"]

    draft = await harness.call("draft_spec_from_milestone", {"milestone_id": milestone["id"]})
    assert draft["milestone_id"] == milestone["id"]
    assert "fr_developer_11111111" in draft["draft_spec"]

    review = await harness.call("review_milestone_scope", {"milestone_id": milestone["id"]})
    assert review["recommendation"] == "ready_for_spec"


@pytest.mark.asyncio
async def test_propose_milestone_accepts_json_work_unit(harness):
    work_unit = """{
      "name": "Cluster 9 (1 FR, targets: developer)",
      "targets": ["developer"],
      "frs": [{"fr_id": "fr_developer_33333333", "description": "Small slice", "priority": "high"}]
    }"""

    result = await harness.call("propose_milestone_from_work_unit", {
        "work_unit": work_unit,
        "title": "Small milestone",
    })

    assert result["milestone"]["title"] == "Small milestone"
    assert result["milestone"]["fr_ids"] == ["fr_developer_33333333"]


@pytest.mark.asyncio
async def test_prepare_development_handoff_from_top_work_unit(harness):
    cluster_response = {"result": {"result": """## Cluster 1 (2 FRs, targets: developer)
  [fr_developer_11111111] Milestone entity → developer [high]
  [fr_developer_22222222] Draft spec artifact → developer [medium]
"""}}

    async def mock_request(**kwargs):
        return cluster_response

    harness.agent.request = mock_request
    result = await harness.call("prepare_development_handoff", {"target": "developer"})

    assert result["status"] == "ready"
    assert result["source"] == "clusters"
    assert result["remaining_work_units"] == 0
    assert result["milestone"]["id"].startswith("ms_developer_")
    assert result["milestone"]["fr_ids"] == ["fr_developer_11111111", "fr_developer_22222222"]
    assert result["work_unit"]["name"] == "Cluster 1 (2 FRs, targets: developer)"
    assert result["scope_review"]["recommendation"] == "ready_for_spec"
    assert "fr_developer_11111111" in result["draft_spec"]
    assert result["suggested_next_actions"][1].endswith("review_terms=AutoGen,GRA")
    assert result["suggested_next_actions"][-1] == (
        "create implementation branch and start the scoped milestone"
    )


@pytest.mark.asyncio
async def test_prepare_development_handoff_flags_review_terms(harness):
    work_unit = """{
      "name": "Cluster 9 (1 FR, targets: developer)",
      "targets": ["developer"],
      "frs": [{"fr_id": "fr_developer_33333333", "description": "Create AutoGen template", "priority": "high"}]
    }"""

    result = await harness.call("prepare_development_handoff", {
        "work_unit": work_unit,
        "review_terms": "AutoGen",
    })

    assert result["status"] == "needs_review"
    assert "remaining_work_units" not in result
    assert result["scope_review"]["recommendation"] == "refine_before_implementation"
    assert result["suggested_next_actions"][1].endswith("review_terms=AutoGen")
    assert result["suggested_next_actions"][-1] == (
        "refine or split the milestone before implementation"
    )

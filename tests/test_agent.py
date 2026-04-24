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
    # 34 existing skills + 7 milestone/handoff/migration skills
    # + concept FR candidates + PR readiness + session checkpoint/resume
    # + repo hygiene audit/apply
    # + PR fleet watcher trio (watch/list/stop)
    # + pr_fleet_status snapshot (fr_developer_fafb36f1).
    # + tracking-infrastructure Phase 1 (BugStore 6 + DogfoodStore 3 = 9).
    # + tracking-infrastructure Phase 2A (triage_bug / link_bug_fr /
    #   triage_dogfood / dogfood_triage_queue / report_gap = 5).
    # + milestone lifecycle (fr_developer_91a5a072): update_status /
    #   supersede / update_frs / delete = 4.
    # + integration-point scanner (fr_developer_82fe7309): suggest +
    #   distill = 2.
    # + project ecosystem introspection (fr_developer_5564b81f): 1.
    # + project lifecycle (fr_developer_5d0a8711 Phase 2):
    #   project_init / list_projects / get_project = 3.
    # + pre-push review orchestration (fr_developer_6ecd0c01):
    #   review_staged_diff = 1.
    assert len(harness.skills) == 76


def test_skills_registered(harness):
    expected = {
        "read_spec", "list_specs", "traverse_milestone",
        "health_check", "developer_guide",
        "get_fr", "list_frs", "get_paper_context",
        "fr_candidates_from_concepts",
        "pr_ready",
        "next_work_unit", "work_units",
        "propose_milestone_from_work_unit", "get_milestone",
        "list_milestones", "draft_spec_from_milestone", "review_milestone_scope",
        "prepare_development_handoff",
        "migrate_frs_from_researcher",
        "create_session_checkpoint", "resume_session_checkpoint",
        "audit_repo_hygiene", "apply_repo_hygiene_plan",
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
        # long-running PR fleet watcher (fr_developer_6c8ec260)
        "watch_pr_fleet", "list_pr_watchers", "stop_pr_watcher",
        # fleet-digest snapshot (fr_developer_fafb36f1)
        "pr_fleet_status",
        # tracking-infrastructure Phase 1 (fr_developer_f669bd33 +
        # fr_developer_1324440c): BugStore + DogfoodStore CRUD.
        "file_bug", "list_bugs", "get_bug", "update_bug_status",
        "link_bug_pr", "close_bug",
        "log_dogfood", "list_dogfood", "get_dogfood",
        # tracking-infrastructure Phase 2A: triage loop + report_gap hook.
        # Phase 2B (GH issue ingest, fr_developer_47271f34) follows.
        "triage_bug", "link_bug_fr", "triage_dogfood",
        "dogfood_triage_queue", "report_gap",
        # milestone lifecycle (fr_developer_91a5a072)
        "update_milestone_status", "supersede_milestone",
        "update_milestone_frs", "delete_milestone",
        # integration-point scanner (fr_developer_82fe7309)
        "suggest_integration_points", "distill_integration_points",
        # project ecosystem introspection (fr_developer_5564b81f)
        "project_ecosystem",
        # project lifecycle (fr_developer_5d0a8711 Phase 2)
        "project_init", "list_projects", "get_project",
        # pre-push review orchestration (fr_developer_6ecd0c01)
        "review_staged_diff",
    }
    assert harness.skill_names == expected


def test_read_spec_skill(harness):
    harness.assert_skill_exists("read_spec", description="spec file")


def test_list_specs_skill(harness):
    harness.assert_skill_exists("list_specs", description="Discover")


def test_traverse_milestone_skill(harness):
    harness.assert_skill_exists("traverse_milestone", description="milestone")


def test_get_fr_skill(harness):
    harness.assert_skill_exists("get_fr", description="developer")


def test_list_frs_skill(harness):
    harness.assert_skill_exists("list_frs", description="developer")


def test_get_paper_context_skill(harness):
    harness.assert_skill_exists("get_paper_context", description="researcher")


def test_fr_candidates_from_concepts_skill(harness):
    harness.assert_skill_exists("fr_candidates_from_concepts", description="concept bundles")


def test_integration_scanner_skills(harness):
    harness.assert_skill_exists(
        "suggest_integration_points", description="adoption sites",
    )
    harness.assert_skill_exists(
        "distill_integration_points", description="Re-project",
    )


def test_project_ecosystem_skill_registered(harness):
    harness.assert_skill_exists("project_ecosystem", description="ecosystem")


@pytest.mark.asyncio
async def test_project_ecosystem_handler_brief_no_live(harness, tmp_path):
    # Exercise the handler end-to-end with `include_live=False` so we
    # don't hit the bus — covers: detail normalization, start_dir resolution,
    # sibling-prefix derivation, response shape.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo-root"\n'
        '[tool.setuptools.packages.find]\ninclude = ["demo_root*"]\n'
    )
    result = await harness.call("project_ecosystem", {
        "start_dir": str(tmp_path),
        "include_live": False,
        "detail": "  BRIEF  ",  # also tests detail normalization
    })
    assert result["project"] == "demo-root"
    assert "repos" in result
    assert result["agents"]["live"] == []  # live was skipped
    assert "health_summary" in result


@pytest.mark.asyncio
async def test_project_ecosystem_handler_rejects_unknown_detail(harness, tmp_path):
    # Unknown detail must fall back to 'brief' (not error, not produce
    # undocumented response shape).
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "d"\n')
    result = await harness.call("project_ecosystem", {
        "start_dir": str(tmp_path),
        "include_live": False,
        "detail": "nonsense",
    })
    # brief shape has the top-level keys:
    assert "project" in result
    assert "domain" in result
    assert "agents" in result


@pytest.mark.asyncio
async def test_project_ecosystem_handler_include_live_string_false(harness, tmp_path):
    # _bool_arg regression: include_live='false' should NOT trigger the
    # bus fetch path. If this broke, the fetch would either succeed
    # (against dev bus) or fail noisily.
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    for falsey in ("false", "0", ""):
        result = await harness.call("project_ecosystem", {
            "start_dir": str(tmp_path),
            "include_live": falsey,
        })
        assert result["agents"]["live"] == []


@pytest.mark.asyncio
async def test_project_ecosystem_handler_strips_whitespace_args(harness, tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "whitespace-strip"\n')
    # Whitespace-only start_dir / sibling_prefix must be treated as unset,
    # not as literal paths.
    result = await harness.call("project_ecosystem", {
        "start_dir": "   ",
        "sibling_prefix": "   ",
        "include_live": False,
    })
    # With start_dir stripped to empty, the handler falls back to
    # config.projects['developer'].repo (or cwd). Either way it finds A
    # pyproject — the test just asserts we don't crash and produce a
    # structured response.
    assert "project" in result


def test_pr_ready_skill(harness):
    harness.assert_skill_exists("pr_ready", description="merge readiness")


def test_milestone_skills(harness):
    harness.assert_skill_exists("propose_milestone_from_work_unit", description="milestone")
    harness.assert_skill_exists("get_milestone", description="milestone")
    harness.assert_skill_exists("list_milestones", description="milestone")
    harness.assert_skill_exists("draft_spec_from_milestone", description="draft spec")
    harness.assert_skill_exists("review_milestone_scope", description="duplicate")
    harness.assert_skill_exists("prepare_development_handoff", description="handoff")
    harness.assert_skill_exists("migrate_frs_from_researcher", description="migration")


def test_milestone_lifecycle_skills(harness):
    harness.assert_skill_exists("update_milestone_status", description="lifecycle")
    harness.assert_skill_exists("supersede_milestone", description="superseded")
    harness.assert_skill_exists("update_milestone_frs", description="bundle")
    harness.assert_skill_exists("delete_milestone", description="delete")


def test_session_checkpoint_skills(harness):
    harness.assert_skill_exists("create_session_checkpoint", description="checkpoint")
    harness.assert_skill_exists("resume_session_checkpoint", description="launch briefing")


def test_repo_hygiene_skills(harness):
    harness.assert_skill_exists("audit_repo_hygiene", description="hygiene")
    harness.assert_skill_exists("apply_repo_hygiene_plan", description="hygiene")


# -- collaborations --

def test_collaborations_declared(harness):
    assert "evaluate_spec_against_corpus" in harness.collaboration_names


def test_evaluate_spec_requires_researcher(harness):
    harness.assert_collaboration_exists(
        "evaluate_spec_against_corpus",
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
    assert len(reg.skills) == 76
    assert len(reg.collaborations) == 1


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
async def test_get_and_list_frs_read_developer_store(harness):
    fr = harness.agent.pipeline.frs.promote(
        target="developer",
        title="Developer-owned FR",
        description="local FR",
        priority="high",
        concept="ownership",
    )

    fetched = await harness.call("get_fr", {"fr_id": fr.id})
    assert fetched["id"] == fr.id

    listed = await harness.call("list_frs", {"target": "developer"})
    assert listed["count"] == 1
    assert listed["frs"][0]["id"] == fr.id

@pytest.mark.asyncio
async def test_work_units_from_local_frs(harness):
    """work_units returns ranked units from developer-owned FRs."""
    harness.agent.pipeline.frs.promote(
        target="khonliang",
        title="Thing A",
        description="A",
        priority="high",
        concept="runtime",
    )
    harness.agent.pipeline.frs.promote(
        target="khonliang",
        title="Thing B",
        description="B",
        priority="medium",
        concept="runtime",
    )
    harness.agent.pipeline.frs.promote(
        target="khonliang",
        title="Thing C",
        description="C",
        priority="low",
        concept="runtime",
    )

    result = await harness.call("work_units", {"target": "khonliang"})
    assert result["source"] == "developer_local"
    assert result["count"] == 1
    assert result["work_units"][0]["size"] == 3
    assert result["work_units"][0]["max_priority"] == "high"


@pytest.mark.asyncio
async def test_work_units_are_deterministic_for_equal_rank_groups(harness):
    """Equal-priority work units sort by stable target/concept keys."""
    harness.agent.pipeline.frs.promote(
        target="developer",
        title="Zeta",
        description="Z",
        priority="medium",
        concept="zeta",
    )
    harness.agent.pipeline.frs.promote(
        target="developer",
        title="Alpha",
        description="A",
        priority="medium",
        concept="alpha",
    )

    result = await harness.call("work_units", {"target": "developer"})
    units = result["work_units"]
    assert [u["concept"] for u in units] == ["alpha", "zeta"]
    assert units[0]["name"] == "Developer FR work unit (1 FRs, target: developer, concept: alpha)"
    assert units[1]["name"] == "Developer FR work unit (1 FRs, target: developer, concept: zeta)"
    assert [u["rank"] for u in units] == [1, 2]


@pytest.mark.asyncio
async def test_work_units_no_local_frs(harness):
    """work_units no longer falls back to researcher-owned FRs."""
    result = await harness.call("work_units", {"target": "developer"})
    assert result["source"] == "none"
    assert "developer-owned FRs" in result["error"]


@pytest.mark.asyncio
async def test_next_work_unit_returns_top(harness):
    """next_work_unit returns the highest-ranked developer-local unit."""
    harness.agent.pipeline.frs.promote(
        target="developer",
        title="Low",
        description="low",
        priority="low",
        concept="docs",
    )
    harness.agent.pipeline.frs.promote(
        target="developer",
        title="High",
        description="high",
        priority="high",
        concept="runtime",
    )
    harness.agent.pipeline.frs.promote(
        target="developer",
        title="Med",
        description="med",
        priority="medium",
        concept="runtime",
    )
    result = await harness.call("next_work_unit", {"target": "developer"})
    assert "work_unit" in result
    assert result["work_unit"]["max_priority"] == "high"
    assert result["remaining"] == 1


@pytest.mark.asyncio
async def test_work_units_bound_large_concept_groups(harness):
    """work_units splits same-concept FR groups into implementation-sized bundles."""
    for idx in range(6):
        harness.agent.pipeline.frs.promote(
            target="developer",
            title=f"Runtime slice {idx}",
            description=f"slice {idx}",
            priority="medium",
            concept="runtime",
        )

    result = await harness.call("work_units", {"target": "developer", "max_frs": 2})

    assert result["source"] == "developer_local"
    assert result["max_frs"] == 2
    assert result["count"] == 3
    assert [unit["size"] for unit in result["work_units"]] == [2, 2, 2]
    assert all(len(unit["frs"]) <= 2 for unit in result["work_units"])
    assert "slice 1" in result["work_units"][0]["name"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", [0, -1])
async def test_work_units_reject_bad_max_frs(harness, bad_value):
    result = await harness.call("work_units", {"max_frs": bad_value})

    assert result == {"error": f"max_frs must be a positive integer, got {bad_value}"}


@pytest.mark.asyncio
async def test_next_work_unit_honors_max_frs(harness):
    for idx in range(3):
        harness.agent.pipeline.frs.promote(
            target="developer",
            title=f"Runtime task {idx}",
            description=f"task {idx}",
            priority="high",
            concept="runtime",
        )

    result = await harness.call("next_work_unit", {"target": "developer", "max_frs": 2})

    assert result["work_unit"]["size"] == 2
    assert len(result["work_unit"]["frs"]) == 2
    assert result["remaining"] == 1
    assert result["max_frs"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", [0, -1])
async def test_next_work_unit_rejects_bad_max_frs(harness, bad_value):
    result = await harness.call("next_work_unit", {"max_frs": bad_value})

    assert result == {"error": f"max_frs must be a positive integer, got {bad_value}"}


@pytest.mark.asyncio
async def test_next_work_unit_no_units_returns_error(harness):
    """next_work_unit returns an error dict when no work units are available."""
    result = await harness.call("next_work_unit", {})
    assert "error" in result


@pytest.mark.asyncio
async def test_migrate_frs_from_researcher_skill_preserves_ids(harness, tmp_path):
    import sqlite3
    from tests.test_fr_migration import _SCHEMA, _seed_fr

    researcher_db = tmp_path / "researcher.db"
    conn = sqlite3.connect(str(researcher_db))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    _seed_fr(
        str(researcher_db),
        fr_id="fr_developer_aaaaaaaa",
        title="Migrated FR",
        content="description",
        target="developer",
        status_tags=[],
        metadata={
            "concept": "ownership",
            "classification": "app",
            "priority": "high",
            "target": "developer",
        },
    )

    result = await harness.call("migrate_frs_from_researcher", {
        "source_db": str(researcher_db),
        "apply": True,
    })

    assert result["frs_found"] == 1
    assert result["frs_migrated"] == 1
    migrated = await harness.call("get_fr", {"fr_id": "fr_developer_aaaaaaaa"})
    assert migrated["id"] == "fr_developer_aaaaaaaa"
    assert migrated["title"] == "Migrated FR"


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
    fr1 = harness.agent.pipeline.frs.promote(
        target="developer",
        title="Milestone entity",
        description="Milestone entity",
        priority="high",
        concept="milestones",
    )
    fr2 = harness.agent.pipeline.frs.promote(
        target="developer",
        title="Draft spec artifact",
        description="Draft spec artifact",
        priority="medium",
        concept="milestones",
    )
    result = await harness.call("propose_milestone_from_work_unit", {"target": "developer"})

    milestone = result["milestone"]
    assert milestone["id"].startswith("ms_developer_")
    assert milestone["target"] == "developer"
    assert milestone["fr_ids"] == [fr1.id, fr2.id]
    assert "Draft spec artifact" in milestone["draft_spec"]

    listed = await harness.call("list_milestones", {"target": "developer"})
    assert listed["count"] == 1
    assert listed["milestones"][0]["id"] == milestone["id"]

    fetched = await harness.call("get_milestone", {"milestone_id": milestone["id"]})
    assert fetched["milestone"]["id"] == milestone["id"]

    draft = await harness.call("draft_spec_from_milestone", {"milestone_id": milestone["id"]})
    assert draft["milestone_id"] == milestone["id"]
    assert fr1.id in draft["draft_spec"]

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
async def test_update_milestone_status_and_supersede_via_agent(harness):
    """Round-trip supersede through the handler layer.

    Also doubles as the documented smoke test for the first-run
    validation of the ``supersede_milestone`` skill: confirms the
    pointer lands on the superseded milestone and the superseder
    is untouched.
    """
    stale_wu = """{
      "name": "Stale cluster",
      "targets": ["developer"],
      "frs": [{"fr_id": "fr_developer_stale1", "description": "old scope", "priority": "high"}]
    }"""
    replacement_wu = """{
      "name": "Replacement cluster",
      "targets": ["developer"],
      "frs": [{"fr_id": "fr_developer_new1", "description": "new scope", "priority": "high"}]
    }"""

    stale = await harness.call("propose_milestone_from_work_unit", {
        "work_unit": stale_wu, "title": "Stale milestone",
    })
    replacement = await harness.call("propose_milestone_from_work_unit", {
        "work_unit": replacement_wu, "title": "Replacement milestone",
    })

    stale_id = stale["milestone"]["id"]
    replacement_id = replacement["milestone"]["id"]

    result = await harness.call("supersede_milestone", {
        "superseded_id": stale_id,
        "superseded_by_id": replacement_id,
        "rationale": "auto-selection bug; obsolete",
    })
    assert result["milestone"]["status"] == "superseded"
    assert result["milestone"]["superseded_by"] == replacement_id

    # Read surfaces expose the pointer.
    fetched = await harness.call("get_milestone", {"milestone_id": stale_id})
    assert fetched["milestone"]["superseded_by"] == replacement_id

    # Superseder untouched.
    replacement_fetched = await harness.call("get_milestone", {"milestone_id": replacement_id})
    assert replacement_fetched["milestone"]["status"] == "proposed"
    assert replacement_fetched["milestone"]["superseded_by"] == ""


@pytest.mark.asyncio
async def test_update_milestone_status_force_false_string_not_treated_as_true(harness):
    """String 'false' must NOT coerce to force=True.

    Regression guard for PR #43 Copilot R1 finding 1: previously
    ``force=bool(args.get("force", False))`` treated any non-empty
    string as True, so JSON/CLI callers sending ``force="false"``
    accidentally enabled forced rollbacks. The handler now uses
    ``_bool_arg`` which strictly treats common false-ish strings
    (``"false"``, ``"0"``, ``"no"``, ``"off"``, ``""``) as False.
    """
    work_unit = """{
      "name": "Force-string cluster",
      "targets": ["developer"],
      "frs": [{"fr_id": "fr_developer_forcestr", "description": "scope", "priority": "high"}]
    }"""
    proposed = await harness.call("propose_milestone_from_work_unit", {
        "work_unit": work_unit, "title": "Force-string milestone",
    })
    mid = proposed["milestone"]["id"]

    # Drive the milestone into a terminal state so rollback requires force.
    await harness.call("update_milestone_status", {
        "milestone_id": mid, "status": "in_progress",
    })
    await harness.call("update_milestone_status", {
        "milestone_id": mid, "status": "completed",
    })

    # Attempt rollback with the literal string "false" as force — must
    # be refused, just as `force=False` would be.
    result = await harness.call("update_milestone_status", {
        "milestone_id": mid,
        "status": "in_progress",
        "force": "false",
    })
    assert "error" in result
    assert "illegal transition" in result["error"]

    # Confirm status unchanged on disk.
    fetched = await harness.call("get_milestone", {"milestone_id": mid})
    assert fetched["milestone"]["status"] == "completed"

    # Sanity: the string "true" DOES coerce to True and permits rollback.
    rolled = await harness.call("update_milestone_status", {
        "milestone_id": mid,
        "status": "in_progress",
        "force": "true",
    })
    assert "error" not in rolled
    assert rolled["milestone"]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_delete_milestone_via_agent_refuses_with_audit_trail(harness):
    stale_wu = """{
      "name": "Mutated cluster",
      "targets": ["developer"],
      "frs": [{"fr_id": "fr_developer_mut1", "description": "scope", "priority": "high"}]
    }"""
    stale = await harness.call("propose_milestone_from_work_unit", {
        "work_unit": stale_wu,
    })
    mid = stale["milestone"]["id"]

    # Add a mutation note via the status transition skill.
    await harness.call("update_milestone_status", {
        "milestone_id": mid, "status": "in_progress", "notes": "kicking off",
    })
    delete_result = await harness.call("delete_milestone", {"milestone_id": mid})
    assert "error" in delete_result
    assert "supersede" in delete_result["error"].lower()


@pytest.mark.asyncio
async def test_prepare_development_handoff_from_top_work_unit(harness):
    fr1 = harness.agent.pipeline.frs.promote(
        target="developer",
        title="Milestone entity",
        description="Milestone entity",
        priority="high",
        concept="handoff",
    )
    fr2 = harness.agent.pipeline.frs.promote(
        target="developer",
        title="Draft spec artifact",
        description="Draft spec artifact",
        priority="medium",
        concept="handoff",
    )
    result = await harness.call("prepare_development_handoff", {"target": "developer"})

    assert result["status"] == "ready"
    assert result["source"] == "developer_local"
    assert result["remaining_work_units"] == 0
    assert result["milestone"]["id"].startswith("ms_developer_")
    assert result["milestone"]["fr_ids"] == [fr1.id, fr2.id]
    assert result["work_unit"]["source"] == "developer_local"
    assert result["scope_review"]["recommendation"] == "ready_for_spec"
    assert fr1.id in result["draft_spec"]
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


@pytest.mark.asyncio
async def test_create_session_checkpoint_handler(harness, git_repo):
    fr = harness.agent.pipeline.frs.promote(
        target="developer",
        title="Checkpoint workflow",
        description="Checkpoint workflow",
        priority="medium",
        concept="context economics",
    )
    (git_repo / "scratch.txt").write_text("work\n")

    result = await harness.call("create_session_checkpoint", {
        "fr_id": fr.id,
        "cwd": str(git_repo),
        "context_tokens": 900_000,
        "context_limit": 1_000_000,
        "idle_minutes": 20,
        "tests": {"passed": 12, "failed": 0, "errors": 0, "digest": "12 passed"},
        "next_actions": "finish implementation,request review",
    })

    checkpoint = result["checkpoint"]
    assert checkpoint["schema"] == "session-checkpoint/v1"
    assert checkpoint["fr"]["id"] == fr.id
    assert checkpoint["repo"]["branch"] == "main"
    assert checkpoint["repo"]["dirty"] is True
    assert "scratch.txt" in checkpoint["repo"]["changed_files"]
    assert checkpoint["tests"]["passed"] == 12
    assert checkpoint["agent_state"]["agent_id"]
    assert {f["kind"] for f in checkpoint["token_hygiene"]} == {
        "context_window_pressure",
        "idle_cache_risk",
    }
    assert checkpoint["next_actions"][0] == "finish implementation"


@pytest.mark.asyncio
async def test_resume_session_checkpoint_handler_detects_git_drift(harness, git_repo):
    initial = await harness.call("create_session_checkpoint", {"cwd": str(git_repo)})
    checkpoint = initial["checkpoint"]
    (git_repo / "later.txt").write_text("later\n")

    result = await harness.call("resume_session_checkpoint", {
        "cwd": str(git_repo),
        "checkpoint": checkpoint,
    })

    resume = result["resume"]
    assert resume["schema"] == "session-resume/v1"
    assert resume["stale"] is True
    assert "changed file set differs from checkpoint" in resume["stale_reasons"]
    assert "resume checkpoint:" in resume["briefing"]


@pytest.mark.asyncio
async def test_create_session_checkpoint_rejects_unknown_fr(harness, git_repo):
    result = await harness.call("create_session_checkpoint", {
        "fr_id": "fr_developer_ffffffff",
        "cwd": str(git_repo),
    })

    assert result == {"error": "unknown FR id: fr_developer_ffffffff"}


@pytest.mark.asyncio
async def test_audit_repo_hygiene_handler(harness, git_repo):
    (git_repo / "README.md").write_text("# Demo\n\nNo config docs.\n")
    (git_repo / "legacy.md").write_text("MS-01 stubbed reference\n")

    result = await harness.call("audit_repo_hygiene", {
        "repo_path": str(git_repo),
        "include_text_scan": True,
    })

    audit = result["audit"]
    assert audit["schema"] == "repo-hygiene/v1"
    assert audit["repo_inventory"]["has_readme"] is True
    assert audit["git_status"]["is_git_repo"] is True
    assert any(f["path"] == "legacy.md" for f in audit["deprecated_paths"])
    assert any(a["id"] == "write-hygiene-artifact" for a in audit["cleanup_plan"])


@pytest.mark.asyncio
async def test_apply_repo_hygiene_plan_handler_writes_artifact(harness, git_repo):
    result = await harness.call("apply_repo_hygiene_plan", {
        "repo_path": str(git_repo),
        "audit_path": "docs/hygiene.md",
    })

    audit = result["audit"]
    assert audit["applied_changes"][0]["path"] == "docs/hygiene.md"
    assert (git_repo / "docs" / "hygiene.md").exists()


# -- CLI --version flag (FR-A2 adoption) --


def test_main_version_flag_prints_and_exits(capsys):
    """`python -m developer.agent --version` prints resolved version + exits 0.

    Regression guard for the CLI wiring. Runs ``main()`` in-process with
    monkeypatched sys.argv so we don't need a subprocess; argparse's
    version action still raises SystemExit(0) with the formatted
    message on stdout.
    """
    import sys
    from developer import agent as agent_module

    saved_argv = sys.argv
    sys.argv = ["developer.agent", "--version"]
    try:
        with pytest.raises(SystemExit) as excinfo:
            agent_module.main()
        assert excinfo.value.code == 0
    finally:
        sys.argv = saved_argv

    out = capsys.readouterr().out
    assert out.startswith("developer.agent "), f"unexpected prog name in {out!r}"
    # Version itself is resolved from pyproject at call time; only assert
    # it's a non-empty dotted version string so bumps don't break the test.
    version_part = out.strip().split(" ", 1)[1]
    assert version_part, "version suffix empty"
    assert version_part != "<unknown>", "resolve_version failed unexpectedly"


# -- project lifecycle (fr_developer_5d0a8711 Phase 2) ------------------


def test_project_lifecycle_skills_registered(harness):
    harness.assert_skill_exists("project_init", description="Register")
    harness.assert_skill_exists("list_projects", description="List")
    harness.assert_skill_exists("get_project", description="Look up")


@pytest.mark.asyncio
async def test_project_init_creates_record(harness):
    result = await harness.call("project_init", {
        "slug": "demo",
        "repos": "/tmp/a,/tmp/b",
        "name": "Demo Project",
        "domain": "software-engineering",
    })
    assert result["slug"] == "demo"
    assert result["name"] == "Demo Project"
    assert result["domain"] == "software-engineering"
    assert len(result["repos"]) == 2
    assert result["status"] == "active"
    assert result["created_at"] > 0


@pytest.mark.asyncio
async def test_project_init_accepts_json_repos(harness):
    result = await harness.call("project_init", {
        "slug": "json-repos",
        "repos": '[{"path": "/x", "role": "library"}, {"path": "/y", "role": "agent"}]',
    })
    roles = [r["role"] for r in result["repos"]]
    assert roles == ["library", "agent"]


@pytest.mark.asyncio
async def test_project_init_rejects_missing_slug(harness):
    result = await harness.call("project_init", {"slug": "", "repos": ""})
    assert "error" in result
    assert "slug" in result["error"].lower()


@pytest.mark.asyncio
async def test_project_init_rejects_duplicate(harness):
    await harness.call("project_init", {"slug": "dup", "repos": "/a"})
    result = await harness.call("project_init", {"slug": "dup", "repos": "/b"})
    assert "error" in result
    assert "duplicate" in result["error"].lower()


@pytest.mark.asyncio
async def test_project_init_rejects_bad_slug(harness):
    # Provide valid repos so the slug-validation path is what fails.
    # (After the 'repos required' check landed, passing empty repos would
    # trip THAT guard first and mask the slug test.)
    result = await harness.call("project_init", {"slug": "BAD SLUG", "repos": "/a"})
    assert "error" in result
    assert "invalid" in result["error"].lower() or "slug" in result["error"].lower()


@pytest.mark.asyncio
async def test_project_init_rejects_bad_config_json(harness):
    result = await harness.call("project_init", {
        "slug": "cfg",
        "repos": "/a",
        "config": "{ malformed",
    })
    assert "error" in result
    assert "config" in result["error"].lower()


@pytest.mark.asyncio
async def test_project_init_rejects_malformed_json_repos(harness):
    # Malformed JSON starting with '[' must NOT silently fall back to
    # CSV parsing and produce garbage path fragments.
    result = await harness.call("project_init", {
        "slug": "malformed-repos",
        "repos": '[{"path": "/x"',
    })
    assert "error" in result
    assert "json" in result["error"].lower() or "invalid" in result["error"].lower()


@pytest.mark.asyncio
async def test_project_init_rejects_non_repoable_json_entries(harness):
    # Real validation edge: JSON list whose elements aren't str/dict/RepoRef.
    # _normalize_repos raises TypeError, which the handler routes through
    # {error: 'invalid argument: ...'}. Covers the "valid JSON, bad content"
    # path that wasn't exercised before.
    result = await harness.call("project_init", {
        "slug": "bad-entries",
        "repos": '[42, true, null]',
    })
    assert "error" in result
    assert "invalid" in result["error"].lower()


@pytest.mark.asyncio
async def test_project_init_rejects_json_object_repos(harness):
    # A JSON object (not a list) passed as repos MUST error cleanly —
    # not silently CSV-split into `['{"path":"/x"}']` as a path.
    result = await harness.call("project_init", {
        "slug": "obj-repos",
        "repos": '{"path":"/x"}',
    })
    assert "error" in result
    err = result["error"].lower()
    assert "invalid" in err or "json" in err or "list" in err


@pytest.mark.asyncio
async def test_project_init_rejects_bare_dict_repos(harness):
    # Bare dict (not a JSON string — actual dict) gets iterated as keys
    # by ProjectStore unless we intercept. Explicit rejection keeps
    # the error surface consistent with the JSON-object-as-string case.
    result = await harness.call("project_init", {
        "slug": "dict-repos",
        "repos": {"path": "/x", "role": "library"},  # real dict, not JSON string
    })
    assert "error" in result
    err = result["error"].lower()
    assert "invalid" in err or "list" in err or "dict" in err


@pytest.mark.asyncio
async def test_project_init_rejects_empty_repos(harness):
    # Skill schema says repos is required. Empty after normalization
    # (empty string, comma-only string, empty JSON list) must error.
    for index, empty in enumerate(("", "   ", ",,,", "[]")):
        result = await harness.call("project_init", {
            "slug": f"empty-{index}",
            "repos": empty,
        })
        assert "error" in result, f"empty repos {empty!r} was accepted"
        assert "repos" in result["error"].lower()


@pytest.mark.asyncio
async def test_project_init_accepts_valid_list_repos(harness):
    # Sanity pair for the rejection tests — the happy path still works.
    result = await harness.call("project_init", {
        "slug": "dict-entries-ok",
        "repos": '[{"path":"/a","role":"library"},{"path":"/b","role":"agent"}]',
    })
    assert "error" not in result
    roles = [r["role"] for r in result["repos"]]
    assert roles == ["library", "agent"]


@pytest.mark.asyncio
async def test_list_projects_empty(harness):
    result = await harness.call("list_projects", {})
    assert result == {"count": 0, "projects": []}


@pytest.mark.asyncio
async def test_list_projects_include_retired_string_false_not_truthy(harness):
    # Naive `bool("false") == True` would silently include retired records.
    # `_bool_arg` correctly treats "false"/"no"/"0"/"" as falsey. Regression
    # guard mirrors the milestone-status force-string test.
    #
    # Seed BOTH an active and a retired project so the test actually
    # distinguishes the two paths — a test that doesn't seed retired data
    # can't detect the `bool("false") → True` bug.
    from developer.project_store import Project, PROJECT_STATUS_RETIRED

    await harness.call("project_init", {"slug": "visible", "repos": "/a"})
    # Inject a retired project directly via the store (no skill yet exposes
    # the retire operation — that's Phase 3 work).
    pipeline = harness.agent.pipeline
    retired = Project(
        slug="retired-one",
        name="retired-one",
        status=PROJECT_STATUS_RETIRED,
        created_at=1.0,
        updated_at=1.0,
    )
    pipeline.projects._put(retired)

    # Sanity: default hides the retired one.
    default = await harness.call("list_projects", {})
    assert default["count"] == 1

    # `include_retired=True` (real bool) shows both.
    with_retired = await harness.call("list_projects", {"include_retired": True})
    assert with_retired["count"] == 2

    # String-falsey variants must NOT silently include the retired record.
    for falsey in ("false", "no", "0", "", "FALSE", "  false  "):
        result = await harness.call("list_projects", {"include_retired": falsey})
        assert result["count"] == 1, (
            f"include_retired={falsey!r} leaked the retired project "
            f"(got count={result['count']}, expected 1)"
        )


@pytest.mark.asyncio
async def test_list_projects_compact(harness):
    await harness.call("project_init", {"slug": "alpha", "repos": "/a"})
    await harness.call("project_init", {"slug": "bravo", "repos": "/b"})
    result = await harness.call("list_projects", {"detail": "compact"})
    assert result["count"] == 2
    assert result["slugs"] == ["alpha", "bravo"]


@pytest.mark.asyncio
async def test_list_projects_full(harness):
    await harness.call("project_init", {
        "slug": "full-one",
        "repos": '[{"path": "/r", "role": "library"}]',
        "domain": "se",
    })
    result = await harness.call("list_projects", {"detail": "full"})
    assert result["count"] == 1
    row = result["projects"][0]
    assert row["slug"] == "full-one"
    assert row["repos"][0]["role"] == "library"
    assert row["domain"] == "se"


@pytest.mark.asyncio
async def test_list_projects_detail_normalizes_case_and_whitespace(harness):
    # `  FULL  ` and `BRIEF` must normalize to their lowercase variants so
    # the response shape matches the declared contract instead of silently
    # falling back to the brief default for unexpected casing.
    await harness.call("project_init", {
        "slug": "normalize-case",
        "repos": '[{"path": "/r", "role": "library"}]',
    })
    result_full = await harness.call("list_projects", {"detail": "  FULL  "})
    assert result_full["count"] == 1
    assert "repos" in result_full["projects"][0]  # full shape

    result_compact = await harness.call("list_projects", {"detail": "Compact"})
    assert "slugs" in result_compact  # compact shape


@pytest.mark.asyncio
async def test_list_projects_unknown_detail_falls_back_to_brief(harness):
    await harness.call("project_init", {"slug": "unknown-detail", "repos": "/a"})
    result = await harness.call("list_projects", {"detail": "nonsense"})
    assert result["count"] == 1
    row = result["projects"][0]
    assert "repos" not in row  # not full
    assert "slugs" not in result  # not compact
    assert row["slug"] == "unknown-detail"


@pytest.mark.asyncio
async def test_project_init_accepts_config_with_error_key(harness):
    # Regression: the `_parse_json_dict` helper uses a one-key
    # `{"error": ...}` dict as its parse-failure sentinel, which conflates
    # with a user config that legitimately has an "error" key (e.g.
    # `{"error": "warn"}`). project_init inlines its own JSON parse to
    # avoid this collision.
    result = await harness.call("project_init", {
        "slug": "error-key-config",
        "repos": "/a",
        "config": '{"error": "warn"}',
    })
    assert "error" not in result
    assert result["config"] == {"error": "warn"}


@pytest.mark.asyncio
async def test_get_project_found(harness):
    await harness.call("project_init", {"slug": "hit", "repos": "/a"})
    result = await harness.call("get_project", {"slug": "hit"})
    assert result["project"]["slug"] == "hit"


@pytest.mark.asyncio
async def test_get_project_missing_returns_none(harness):
    result = await harness.call("get_project", {"slug": "ghost"})
    assert result == {"project": None}


@pytest.mark.asyncio
async def test_get_project_rejects_bad_slug(harness):
    result = await harness.call("get_project", {"slug": "BAD"})
    assert "error" in result
    assert "slug" in result["error"].lower()


# -- review_staged_diff (fr_developer_6ecd0c01) --


def test_review_staged_diff_skill_registered(harness):
    harness.assert_skill_exists("review_staged_diff", description="staged")


@pytest.mark.asyncio
async def test_review_staged_diff_requires_cwd(harness):
    result = await harness.call("review_staged_diff", {"cwd": ""})
    assert "error" in result
    assert "cwd is required" in result["error"]


@pytest.mark.asyncio
async def test_review_staged_diff_rejects_empty_staged(harness, git_repo):
    # Clean git_repo has nothing staged; handler must refuse rather
    # than calling the reviewer with an empty string.
    called = {"count": 0}

    async def mock_request(**kwargs):
        called["count"] += 1
        return {"result": {}}

    harness.agent.request = mock_request
    result = await harness.call("review_staged_diff", {"cwd": str(git_repo)})
    assert "error" in result
    assert "no staged changes" in result["error"]
    assert called["count"] == 0, "reviewer must not be invoked when there's no diff"


@pytest.mark.asyncio
async def test_review_staged_diff_forwards_raw_diff(harness, git_repo):
    # Stage a file mutation so git diff --cached returns real content.
    # The handler must pipe those raw bytes — not a summary — into
    # reviewer.review_diff.
    (git_repo / "a.txt").write_text("first\nsecond line\n")
    import subprocess
    subprocess.run(["git", "add", "a.txt"], cwd=str(git_repo), check=True)

    captured: dict = {}
    fake_review = {
        "result": {
            "findings": [],
            "disposition": "approved",
            "model": "fake-model",
        }
    }

    async def mock_request(**kwargs):
        captured.update(kwargs)
        return fake_review

    harness.agent.request = mock_request

    result = await harness.call("review_staged_diff", {
        "cwd": str(git_repo),
        "backend": "ollama",
        "model": "qwen2.5-coder:14b",
        "severity_floor": "note",
        "timeout_s": 45,
    })

    assert result == fake_review["result"]

    # Routing shape: agent_type + operation + timeout.
    assert captured["agent_type"] == "reviewer"
    assert captured["operation"] == "review_diff"
    assert captured["timeout"] == 45

    # Payload: raw diff bytes (not a summary), plus forwarded tunables.
    forwarded = captured["args"]
    assert "diff" in forwarded
    assert forwarded["diff"].startswith("diff --git"), (
        "review_staged_diff must forward raw `git diff --cached` output; "
        f"got leading {forwarded['diff'][:40]!r}"
    )
    assert "second line" in forwarded["diff"]
    assert forwarded["backend"] == "ollama"
    assert forwarded["model"] == "qwen2.5-coder:14b"
    assert forwarded["severity_floor"] == "note"
    # context default includes cwd + branch.
    assert str(git_repo) in forwarded["context"]
    assert "main" in forwarded["context"]


@pytest.mark.asyncio
async def test_review_staged_diff_caller_context_wins(harness, git_repo):
    # If the caller sets context explicitly, the handler must not overwrite
    # it with the cwd/branch default.
    (git_repo / "a.txt").write_text("first\nb\n")
    import subprocess
    subprocess.run(["git", "add", "a.txt"], cwd=str(git_repo), check=True)

    captured: dict = {}

    async def mock_request(**kwargs):
        captured.update(kwargs)
        return {"result": {"findings": []}}

    harness.agent.request = mock_request

    await harness.call("review_staged_diff", {
        "cwd": str(git_repo),
        "context": "pre-push review for PR #123 (caller-supplied)",
    })
    assert captured["args"]["context"] == "pre-push review for PR #123 (caller-supplied)"


@pytest.mark.asyncio
async def test_review_staged_diff_rejects_invalid_timeout(harness, git_repo):
    # Stage a file so we'd otherwise reach the reviewer-call site; the
    # handler must refuse before calling self.request() for any timeout
    # that isn't a positive finite number.
    (git_repo / "a.txt").write_text("first\nb\n")
    import subprocess
    subprocess.run(["git", "add", "a.txt"], cwd=str(git_repo), check=True)

    invocations = {"count": 0}

    async def mock_request(**kwargs):
        invocations["count"] += 1
        return {"result": {"findings": []}}

    harness.agent.request = mock_request

    for bad in ("slow", 0, -5, float("inf"), float("nan")):
        result = await harness.call("review_staged_diff", {
            "cwd": str(git_repo),
            "timeout_s": bad,
        })
        assert "error" in result, f"timeout_s={bad!r} must be rejected, got {result!r}"
        assert "timeout_s" in result["error"].lower()

    assert invocations["count"] == 0, (
        "review_staged_diff must not call reviewer on invalid timeout_s"
    )


@pytest.mark.asyncio
async def test_review_staged_diff_reports_reviewer_failure(harness, git_repo):
    (git_repo / "a.txt").write_text("first\nb\n")
    import subprocess
    subprocess.run(["git", "add", "a.txt"], cwd=str(git_repo), check=True)

    async def mock_request(**kwargs):
        raise RuntimeError("reviewer offline")

    harness.agent.request = mock_request

    result = await harness.call("review_staged_diff", {"cwd": str(git_repo)})
    assert "error" in result
    assert "reviewer request failed" in result["error"]
    assert "reviewer offline" in result["error"]


# ---------------------------------------------------------------------------
# fr_developer_69973285 — skill-handler project pass-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_fr_defaults_project_to_default_project(harness):
    from developer.project_store import DEFAULT_PROJECT
    result = await harness.call("promote_fr", {
        "target": "developer", "title": "default-proj-fr", "description": "d",
    })
    assert result["project"] == DEFAULT_PROJECT
    # Round-trips through the store.
    got = await harness.call("get_fr_local", {"fr_id": result["fr_id"]})
    assert got["project"] == DEFAULT_PROJECT


@pytest.mark.asyncio
async def test_promote_fr_passes_project_through_to_store(harness):
    result = await harness.call("promote_fr", {
        "target": "developer", "title": "alpha-proj-fr", "description": "d",
        "project": "alpha",
    })
    assert result["project"] == "alpha"
    got = await harness.call("get_fr_local", {"fr_id": result["fr_id"]})
    assert got["project"] == "alpha"


@pytest.mark.asyncio
async def test_list_frs_local_filters_by_project(harness):
    # One default-project FR and one alpha FR; ensure the filter
    # reaches the store.
    default_fr = await harness.call("promote_fr", {
        "target": "developer", "title": "d-fr", "description": "d",
    })
    alpha_fr = await harness.call("promote_fr", {
        "target": "developer", "title": "a-fr", "description": "a",
        "project": "alpha",
    })
    alpha_only = await harness.call("list_frs_local", {"project": "alpha"})
    ids = {f["id"] for f in alpha_only["frs"]}
    assert alpha_fr["fr_id"] in ids
    assert default_fr["fr_id"] not in ids
    # Empty project → all projects (None at the store).
    all_of_them = await harness.call("list_frs_local", {})
    all_ids = {f["id"] for f in all_of_them["frs"]}
    assert {default_fr["fr_id"], alpha_fr["fr_id"]} <= all_ids


@pytest.mark.asyncio
async def test_next_fr_local_filters_by_project(harness):
    default_fr = await harness.call("promote_fr", {
        "target": "developer", "title": "d-next", "description": "d",
    })
    alpha_fr = await harness.call("promote_fr", {
        "target": "developer", "title": "a-next", "description": "a",
        "project": "alpha",
    })
    # Scoped to alpha should pick alpha_fr (only ready candidate there).
    result = await harness.call("next_fr_local", {"project": "alpha"})
    assert result["fr"] is not None
    assert result["fr"]["id"] == alpha_fr["fr_id"]


@pytest.mark.asyncio
async def test_file_bug_passes_project_through(harness):
    result = await harness.call("file_bug", {
        "target": "developer", "title": "b", "description": "d",
        "observed_entity": "x", "project": "genealogy",
    })
    assert result["project"] == "genealogy"


@pytest.mark.asyncio
async def test_list_bugs_filters_by_project(harness):
    a = await harness.call("file_bug", {
        "target": "developer", "title": "alpha-b", "description": "d1",
        "observed_entity": "e1", "project": "alpha",
    })
    b = await harness.call("file_bug", {
        "target": "developer", "title": "beta-b", "description": "d2",
        "observed_entity": "e2", "project": "beta",
    })
    alpha_only = await harness.call("list_bugs", {"project": "alpha"})
    ids = {bug["id"] for bug in alpha_only["bugs"]}
    assert a["bug_id"] in ids
    assert b["bug_id"] not in ids


@pytest.mark.asyncio
async def test_log_dogfood_passes_project_through(harness):
    result = await harness.call("log_dogfood", {
        "observation": "project-pass-obs", "project": "alpha-app",
    })
    assert result["project"] == "alpha-app"


@pytest.mark.asyncio
async def test_list_dogfood_filters_by_project(harness):
    a = await harness.call("log_dogfood", {
        "observation": "alpha obs", "project": "alpha",
    })
    b = await harness.call("log_dogfood", {
        "observation": "beta obs", "project": "beta",
    })
    alpha_only = await harness.call("list_dogfood", {"project": "alpha"})
    ids = {d["id"] for d in alpha_only["dogfood"]}
    assert a["dog_id"] in ids
    assert b["dog_id"] not in ids


@pytest.mark.asyncio
async def test_list_milestones_filters_by_project(harness):
    # Seed two milestones with distinct fr_ids so they get separate ids,
    # one default and one explicit.
    def _wu(name, fr_id):
        return {
            "name": name,
            "targets": ["developer"],
            "rank": 1,
            "frs": [{"fr_id": fr_id, "target": "developer", "title": fr_id}],
        }
    import json
    default_ms = await harness.call("propose_milestone_from_work_unit", {
        "work_unit": json.dumps(_wu("A", "fr_developer_a0000001")),
    })
    alpha_ms = await harness.call("propose_milestone_from_work_unit", {
        "work_unit": json.dumps(_wu("B", "fr_developer_b0000001")),
        "project": "alpha",
    })
    alpha_only = await harness.call("list_milestones", {"project": "alpha"})
    ids = {m["id"] for m in alpha_only["milestones"]}
    assert alpha_ms["milestone"]["id"] in ids
    assert default_ms["milestone"]["id"] not in ids


@pytest.mark.asyncio
async def test_list_frs_local_string_false_include_all_not_truthy(harness):
    # Regression: naive bool("false") == True would silently include
    # terminal-state FRs when a caller sends a JSON/CLI string.
    # _bool_arg correctly treats "false" / "0" / "no" / "" as falsey.
    # Seeding a terminal FR and asserting visibility is how this test
    # actually detects the bug — without a terminal row, include_all
    # flipping True would go unnoticed.
    active = await harness.call("promote_fr", {
        "target": "developer", "title": "active", "description": "d",
    })
    archived = await harness.call("promote_fr", {
        "target": "developer", "title": "stale", "description": "d",
    })
    archived_result = await harness.call("update_fr_status", {
        "fr_id": archived["fr_id"], "status": "archived",
    })
    assert archived_result["status"] == "archived"

    # include_all=True → terminal FR is visible.
    with_archived = await harness.call("list_frs_local", {"include_all": True})
    with_ids = {f["id"] for f in with_archived["frs"]}
    assert archived["fr_id"] in with_ids
    assert active["fr_id"] in with_ids

    # Falsy string variants must HIDE the terminal FR. If _bool_arg
    # regressed to naive bool(), "false" would show the archived row.
    for falsey in ("false", "0", "no", "", "FALSE", "  false  "):
        result = await harness.call("list_frs_local", {"include_all": falsey})
        ids = {f["id"] for f in result["frs"]}
        assert archived["fr_id"] not in ids, (
            f"include_all={falsey!r} leaked a terminal FR"
        )
        assert active["fr_id"] in ids


@pytest.mark.asyncio
async def test_list_milestones_string_false_include_archived_not_truthy(harness):
    def _wu(name, fr_id):
        return {
            "name": name, "targets": ["developer"], "rank": 1,
            "frs": [{"fr_id": fr_id, "target": "developer", "title": fr_id}],
        }
    import json
    # Seed one active and one abandoned milestone so the test
    # actually measures filter behavior, not just call shape.
    active = await harness.call("propose_milestone_from_work_unit", {
        "work_unit": json.dumps(_wu("active-ms", "fr_developer_msa00001")),
    })
    to_abandon = await harness.call("propose_milestone_from_work_unit", {
        "work_unit": json.dumps(_wu("abandon-ms", "fr_developer_msb00002")),
    })
    abandoned = await harness.call("update_milestone_status", {
        "milestone_id": to_abandon["milestone"]["id"],
        "status": "abandoned",
    })
    assert abandoned.get("milestone", {}).get("status") == "abandoned" or \
        abandoned.get("status") == "abandoned"

    # include_archived=True → abandoned milestone visible.
    with_arc = await harness.call("list_milestones", {"include_archived": True})
    with_ids = {m["id"] for m in with_arc["milestones"]}
    assert to_abandon["milestone"]["id"] in with_ids
    assert active["milestone"]["id"] in with_ids

    # Falsy string variants must HIDE the abandoned milestone.
    for falsey in ("false", "0", "no", ""):
        result = await harness.call("list_milestones", {"include_archived": falsey})
        ids = {m["id"] for m in result["milestones"]}
        assert to_abandon["milestone"]["id"] not in ids, (
            f"include_archived={falsey!r} leaked an abandoned milestone"
        )
        assert active["milestone"]["id"] in ids

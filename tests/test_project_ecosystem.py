"""Tests for developer.project_ecosystem.

Heuristic-discovery path + live-agent overlay parsing. No bus calls —
the overlay is passed in as a payload so tests stay deterministic and
offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from developer.project_ecosystem import (
    ROLE_AGENT,
    ROLE_APP,
    ROLE_LIBRARY,
    ROLE_SERVICE,
    EcosystemView,
    LiveAgent,
    RepoDescriptor,
    apply_live_overlay,
    build_view,
    discover_project,
    discover_siblings,
    extract_ecosystem_deps,
    find_pyproject,
    import_name_of,
    infer_role,
    install_name_of,
    parse_live_agents,
    read_pyproject,
)


# ---------------------------------------------------------------------------
# Descriptor serialization
# ---------------------------------------------------------------------------


class TestRepoDescriptor:
    def test_compact_omits_import_and_deps(self):
        r = RepoDescriptor(path="/x", install_name="foo", import_name="foo", role=ROLE_LIBRARY, ecosystem_deps=["dep"])
        d = r.to_dict(detail="compact")
        assert d == {"path": "/x", "role": ROLE_LIBRARY}

    def test_brief_includes_names(self):
        r = RepoDescriptor(path="/x", install_name="foo", import_name="foo_mod", role=ROLE_AGENT)
        d = r.to_dict(detail="brief")
        assert d["install_name"] == "foo"
        assert d["import_name"] == "foo_mod"
        assert "ecosystem_deps" not in d

    def test_full_includes_deps(self):
        r = RepoDescriptor(path="/x", install_name="foo", import_name="foo", role=ROLE_AGENT, ecosystem_deps=["a", "b"])
        d = r.to_dict(detail="full")
        assert d["ecosystem_deps"] == ["a", "b"]


class TestEcosystemView:
    def test_compact_shape(self):
        v = EcosystemView(
            project="khonliang",
            domain="se",
            repos=[RepoDescriptor(path="/r1", install_name="a", import_name="a"), RepoDescriptor(path="/r2", install_name="b", import_name="b")],
            agents_live=[LiveAgent(agent_id="x", agent_type="developer")],
        )
        d = v.to_dict(detail="compact")
        assert d == {
            "project": "khonliang",
            "repos": ["/r1", "/r2"],
            "repo_count": 2,
            "live_agent_count": 1,
        }

    def test_brief_has_domain_repos_agents_summary(self):
        v = EcosystemView(
            project="p", domain="d",
            repos=[RepoDescriptor(path="/r", install_name="n", import_name="n", role=ROLE_LIBRARY)],
            agents_live=[LiveAgent(agent_id="x", agent_type="t", version="0.1", skill_count=3, healthy=True)],
            agents_declared=["y"],
            health_summary="ok",
        )
        d = v.to_dict(detail="brief")
        assert d["project"] == "p"
        assert d["domain"] == "d"
        assert d["agents"]["declared"] == ["y"]
        assert d["agents"]["live"][0]["skill_count"] == 3
        assert d["health_summary"] == "ok"
        assert "repo_count" not in d  # only in 'full'

    def test_full_adds_repo_count(self):
        v = EcosystemView(project="p", domain="d", repos=[RepoDescriptor(path="/r", install_name="n", import_name="n")])
        d = v.to_dict(detail="full")
        assert d["repo_count"] == 1


# ---------------------------------------------------------------------------
# Role inference
# ---------------------------------------------------------------------------


class TestInferRole:
    @pytest.mark.parametrize("install,expected", [
        ("khonliang-bus-lib", ROLE_LIBRARY),
        ("khonliang-reviewer-lib", ROLE_LIBRARY),
        ("some-lib", ROLE_LIBRARY),
        ("khonliang-bus", ROLE_SERVICE),
        ("khonliang-scheduler", ROLE_SERVICE),
        ("khonliang-developer", ROLE_AGENT),
        ("khonliang-researcher", ROLE_AGENT),
        ("khonliang-reviewer", ROLE_AGENT),
        ("khonliang-librarian", ROLE_AGENT),
        ("autostock", ROLE_APP),
        ("khonliang-genealogy", ROLE_APP),
        ("", ROLE_APP),
    ])
    def test_cases(self, install, expected):
        assert infer_role(install, "") == expected

    def test_uses_import_name_fallback_when_install_empty(self):
        assert infer_role("", "Khonliang-Bus-Lib") == ROLE_LIBRARY  # case-insensitive


# ---------------------------------------------------------------------------
# pyproject parsing
# ---------------------------------------------------------------------------


class TestFindPyproject:
    def test_finds_in_start_dir(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n')
        assert find_pyproject(tmp_path) == tmp_path / "pyproject.toml"

    def test_walks_up(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n')
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        assert find_pyproject(nested) == tmp_path / "pyproject.toml"

    def test_returns_none_when_missing(self, tmp_path):
        # Filesystem root will definitely not have a matching pyproject.
        # Use a subdir of tmp_path (no pyproject anywhere up the chain).
        sub = tmp_path / "nothing" / "here"
        sub.mkdir(parents=True)
        # Not guaranteed — there could be a pyproject at /, though unusual.
        # Accept either None OR some path far away (walk up eventually finds
        # system pyprojects). Guard: assert either None or not within tmp_path.
        result = find_pyproject(sub)
        if result is not None:
            assert tmp_path not in result.parents


class TestReadPyproject:
    def test_reads_valid(self, tmp_path):
        path = tmp_path / "pyproject.toml"
        path.write_text('[project]\nname = "foo"\n')
        data = read_pyproject(path)
        assert data["project"]["name"] == "foo"

    def test_empty_on_missing(self, tmp_path):
        assert read_pyproject(tmp_path / "nope.toml") == {}

    def test_empty_on_malformed(self, tmp_path):
        path = tmp_path / "pyproject.toml"
        path.write_text("this is definitely =] not [= valid toml")
        assert read_pyproject(path) == {}


class TestInstallAndImportNames:
    def test_install_name(self):
        assert install_name_of({"project": {"name": "khonliang-bus"}}) == "khonliang-bus"

    def test_install_name_missing(self):
        assert install_name_of({}) == ""

    def test_import_name_from_setuptools_include(self):
        data = {
            "project": {"name": "khonliang-developer"},
            "tool": {"setuptools": {"packages": {"find": {"include": ["developer*"]}}}},
        }
        assert import_name_of(data) == "developer"

    def test_import_name_falls_back_to_install_name_with_underscores(self):
        data = {"project": {"name": "khonliang-bus-lib"}}
        assert import_name_of(data) == "khonliang_bus_lib"

    def test_import_name_empty_when_install_missing(self):
        assert import_name_of({}) == ""


class TestExtractEcosystemDeps:
    def test_filters_by_prefix(self):
        data = {
            "project": {
                "dependencies": [
                    "khonliang-bus-lib",
                    "khonliang-researcher-lib @ git+https://github.com/tolldog/khonliang-researcher-lib.git@main",
                    "httpx>=0.28",
                    "pydantic",
                ]
            }
        }
        deps = extract_ecosystem_deps(data, "khonliang-")
        assert deps == ["khonliang-bus-lib", "khonliang-researcher-lib"]

    def test_empty_when_no_match(self):
        assert extract_ecosystem_deps({"project": {"dependencies": ["httpx"]}}, "khonliang-") == []

    def test_deduplicates(self):
        data = {"project": {"dependencies": ["khonliang-bus-lib", "khonliang-bus-lib"]}}
        assert extract_ecosystem_deps(data, "khonliang-") == ["khonliang-bus-lib"]

    def test_strips_version_specifiers(self):
        data = {
            "project": {
                "dependencies": [
                    "khonliang-bus-lib>=0.1",
                    "khonliang-researcher-lib==0.3.0",
                    "khonliang-reviewer-lib<=1.0",
                    "khonliang-librarian~=0.2",
                ]
            }
        }
        assert extract_ecosystem_deps(data, "khonliang-") == [
            "khonliang-bus-lib",
            "khonliang-librarian",
            "khonliang-researcher-lib",
            "khonliang-reviewer-lib",
        ]

    def test_strips_extras_and_markers(self):
        data = {
            "project": {
                "dependencies": [
                    "khonliang-bus-lib[test]",
                    "khonliang-researcher-lib>=0.3; python_version>='3.11'",
                    "khonliang-reviewer-lib[extra1,extra2]>=0.1",
                ]
            }
        }
        assert extract_ecosystem_deps(data, "khonliang-") == [
            "khonliang-bus-lib",
            "khonliang-researcher-lib",
            "khonliang-reviewer-lib",
        ]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _mk_repo(parent: Path, name: str, install: str, include: str = "") -> Path:
    """Create a synthetic repo under ``parent`` with a pyproject."""

    repo = parent / name
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "{install}"\n'
        + (f'[tool.setuptools.packages.find]\ninclude = ["{include}*"]\n' if include else "")
    )
    return repo


class TestDiscoverSiblings:
    def test_finds_prefix_matches(self, tmp_path):
        _mk_repo(tmp_path, "foo-a", "foo-a", include="foo_a")
        _mk_repo(tmp_path, "foo-b", "foo-b", include="foo_b")
        _mk_repo(tmp_path, "bar-c", "bar-c", include="bar_c")
        hits = discover_siblings(tmp_path, prefix="foo-")
        names = sorted(h.name for h in hits)
        assert names == ["foo-a", "foo-b"]

    def test_skips_dirs_without_pyproject(self, tmp_path):
        _mk_repo(tmp_path, "foo-a", "foo-a")
        (tmp_path / "foo-b").mkdir()  # no pyproject
        hits = discover_siblings(tmp_path, prefix="foo-")
        assert [h.name for h in hits] == ["foo-a"]

    def test_empty_on_missing_anchor(self, tmp_path):
        assert discover_siblings(tmp_path / "does-not-exist", prefix="x-") == []

    def test_degrades_on_permission_error(self, tmp_path, monkeypatch):
        # Simulate a dir that's stat-able but not listable — read-only
        # introspection should swallow the error and return [].
        def boom(self):
            raise PermissionError("no list for you")

        monkeypatch.setattr(Path, "iterdir", boom)
        # Anchor doesn't matter — iterdir is the failure point.
        assert discover_siblings(tmp_path, prefix="x-") == []

    def test_respects_max_repos(self, tmp_path):
        for i in range(5):
            _mk_repo(tmp_path, f"foo-{i}", f"foo-{i}")
        hits = discover_siblings(tmp_path, prefix="foo-", max_repos=3)
        assert len(hits) == 3


class TestDiscoverProject:
    def test_discovers_anchor_and_siblings(self, tmp_path):
        anchor = _mk_repo(tmp_path, "ecosystem-developer", "ecosystem-developer", include="developer")
        _mk_repo(tmp_path, "ecosystem-bus", "ecosystem-bus", include="bus")
        _mk_repo(tmp_path, "ecosystem-bus-lib", "ecosystem-bus-lib", include="ecosystem_bus")
        _mk_repo(tmp_path, "unrelated-app", "unrelated-app")

        view = discover_project(anchor)
        assert view.project == "ecosystem-developer"
        names = sorted(r.install_name for r in view.repos)
        assert names == ["ecosystem-bus", "ecosystem-bus-lib", "ecosystem-developer"]

    def test_derives_prefix_from_install_name(self, tmp_path):
        anchor = _mk_repo(tmp_path, "foo-bar", "foo-bar")
        _mk_repo(tmp_path, "foo-baz", "foo-baz")
        view = discover_project(anchor)
        assert {r.install_name for r in view.repos} == {"foo-bar", "foo-baz"}

    def test_handles_no_pyproject(self, tmp_path, monkeypatch):
        # Monkeypatch `find_pyproject` to walk only within a controlled
        # subtree so ancestor pyprojects on the real filesystem don't
        # leak into the test.
        from developer import project_ecosystem as pe

        def bounded_find(start: Path):
            # Only walk within tmp_path; anything outside returns None,
            # simulating "no pyproject anywhere up the chain."
            cur = start.resolve()
            while True:
                if tmp_path not in cur.parents and cur != tmp_path:
                    return None
                candidate = cur / "pyproject.toml"
                if candidate.is_file():
                    return candidate
                if cur.parent == cur:
                    return None
                cur = cur.parent

        monkeypatch.setattr(pe, "find_pyproject", bounded_find)

        sub = tmp_path / "nothing-here"
        sub.mkdir()
        view = discover_project(sub)
        assert view.project == "unknown"
        assert "no pyproject" in view.health_summary.lower()

    def test_infers_roles_from_name_suffix(self, tmp_path):
        _mk_repo(tmp_path, "es-bus", "es-bus", include="bus")
        _mk_repo(tmp_path, "es-bus-lib", "es-bus-lib", include="es_bus")
        _mk_repo(tmp_path, "es-developer", "es-developer", include="developer")
        anchor = _mk_repo(tmp_path, "es-app", "es-app")
        view = discover_project(anchor)
        role_by_name = {r.install_name: r.role for r in view.repos}
        assert role_by_name["es-bus"] == ROLE_SERVICE
        assert role_by_name["es-bus-lib"] == ROLE_LIBRARY
        assert role_by_name["es-developer"] == ROLE_AGENT
        assert role_by_name["es-app"] == ROLE_APP


# ---------------------------------------------------------------------------
# Live-agent overlay
# ---------------------------------------------------------------------------


class TestParseLiveAgents:
    def test_accepts_list_shape(self):
        payload = [
            {"id": "a", "agent_type": "developer", "version": "0.2.0", "status": "healthy", "skill_count": 3},
            {"id": "b", "agent_type": "researcher", "version": "0.1.0", "status": "healthy", "skill_count": 7},
        ]
        agents = parse_live_agents(payload)
        assert [a.agent_id for a in agents] == ["a", "b"]
        assert agents[0].agent_type == "developer"
        assert agents[0].skill_count == 3

    def test_accepts_dict_shape_with_agents_key(self):
        payload = {"agents": [{"id": "a", "agent_type": "x", "status": "healthy"}]}
        agents = parse_live_agents(payload)
        assert len(agents) == 1

    def test_returns_empty_for_bogus_shape(self):
        assert parse_live_agents("not a list") == []
        assert parse_live_agents(None) == []
        assert parse_live_agents({"other": "shape"}) == []

    def test_skips_non_dict_items(self):
        payload = [{"id": "good", "agent_type": "t", "status": "healthy"}, "bad", None, 42]
        agents = parse_live_agents(payload)
        assert [a.agent_id for a in agents] == ["good"]

    def test_explicit_unhealthy_flips_healthy(self):
        for bad in ("unhealthy", "failed", "down", "UNHEALTHY", "  Down  "):
            agents = parse_live_agents([{"id": "x", "agent_type": "t", "status": bad}])
            assert agents[0].healthy is False, f"{bad!r} should flip healthy off"

    def test_unknown_status_stays_healthy(self):
        # Healthy-by-default: only the explicit unhealthy-ish set turns the
        # flag off. "degraded" / "starting" / missing are treated as healthy
        # — matches the bus schema's implicit default and keeps the parser
        # forward-compatible with future shapes that omit the field.
        for s in ("degraded", "starting", "", None, "weird"):
            item = {"id": "x", "agent_type": "t"}
            if s is not None:
                item["status"] = s
            agents = parse_live_agents([item])
            assert agents[0].healthy is True, f"{s!r} should NOT flip healthy off"

    def test_non_numeric_skill_count_defaults_to_zero(self):
        # Non-int skill_count in one row shouldn't crash the skill.
        for bad in ("many", None, "", [], {}):
            agents = parse_live_agents([{"id": "x", "agent_type": "t", "skill_count": bad, "status": "healthy"}])
            assert agents[0].skill_count == 0

    def test_skips_rows_missing_both_identifiers(self):
        # Partial rows with neither id nor agent_type should NOT surface
        # as empty-string agents — they're noise.
        payload = [
            {"id": "good", "agent_type": "t", "status": "healthy"},
            {"status": "healthy"},           # no id, no agent_type
            {"id": "", "agent_type": ""},     # both empty
            {"id": "also-good", "agent_type": "u"},
        ]
        agents = parse_live_agents(payload)
        assert [a.agent_id for a in agents] == ["good", "also-good"]


class TestApplyLiveOverlay:
    def test_mutates_view(self):
        view = EcosystemView(project="p", domain="d")
        apply_live_overlay(view, [{"id": "x", "agent_type": "t", "status": "healthy"}])
        assert len(view.agents_live) == 1

    def test_replaces_existing(self):
        view = EcosystemView(project="p", domain="d", agents_live=[LiveAgent(agent_id="old", agent_type="t")])
        apply_live_overlay(view, [{"id": "new", "agent_type": "t", "status": "healthy"}])
        assert [a.agent_id for a in view.agents_live] == ["new"]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


class TestBuildView:
    def test_without_live_payload(self, tmp_path):
        anchor = _mk_repo(tmp_path, "ex-one", "ex-one")
        _mk_repo(tmp_path, "ex-two", "ex-two")
        view = build_view(anchor)
        assert view.project == "ex-one"
        assert view.agents_live == []

    def test_with_live_payload(self, tmp_path):
        anchor = _mk_repo(tmp_path, "ex-one", "ex-one")
        view = build_view(anchor, services_payload=[{"id": "x", "agent_type": "y", "status": "healthy"}])
        assert len(view.agents_live) == 1

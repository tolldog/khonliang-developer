"""Tests for developer's DevRepoStore (fr_developer_5f3dc62e)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from khonliang.knowledge.store import KnowledgeStore

from developer.dev_repo_store import (
    DevRepo,
    DevRepoError,
    DevRepoStore,
    resolve_fr_target,
)


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "dev_repos.db"
    return DevRepoStore(KnowledgeStore(db_path=str(db)))


# ---------------------------------------------------------------------------
# DevRepo dataclass
# ---------------------------------------------------------------------------


class TestDevRepo:
    def test_id_uses_project(self):
        r = DevRepo(project="myproj", repo_path="/x")
        assert r.id == "dev_repo_myproj"

    def test_defaults(self):
        r = DevRepo(project="x", repo_path="/x")
        assert r.default_branch == "main"
        assert r.remote_url == ""
        assert r.owning_agents == []
        assert r.in_flight_pr_numbers == []
        assert r.last_hygiene_audit_at == 0.0

    def test_roundtrip(self):
        original = DevRepo(
            project="developer",
            repo_path="/repo",
            default_branch="trunk",
            remote_url="git@x:y.git",
            test_command="pytest -q",
            compile_command="python -m py_compile",
            reviewer_convention="khonliang-reviewer",
            owning_agents=["developer-primary"],
            last_hygiene_audit_at=5.0,
            last_hygiene_disposition="clean",
            in_flight_pr_numbers=[12, 13],
            created_at=1.0,
            updated_at=2.0,
        )
        assert DevRepo.from_dict(original.to_dict()) == original


# ---------------------------------------------------------------------------
# DevRepoStore.register
# ---------------------------------------------------------------------------


class TestRegister:
    def test_basic(self, store):
        r = store.register("developer", "/abs/path")
        assert r.project == "developer"
        assert r.repo_path == "/abs/path"
        assert r.default_branch == "main"
        assert r.created_at > 0
        assert r.updated_at == r.created_at

    def test_requires_repo_path(self, store):
        with pytest.raises(DevRepoError):
            store.register("developer", "")

    @pytest.mark.parametrize("project", ["", "UPPER", "has space", "slash/bad", "-leading"])
    def test_rejects_invalid_project(self, store, project):
        with pytest.raises(DevRepoError):
            store.register(project, "/x")

    def test_upsert_is_idempotent_on_project(self, store):
        first = store.register("developer", "/a")
        second = store.register("developer", "/b", test_command="pytest -q")
        assert first.id == second.id
        assert second.repo_path == "/b"
        assert second.test_command == "pytest -q"
        assert store.list()[0].repo_path == "/b"

    def test_upsert_preserves_created_at(self, store):
        first = store.register("developer", "/a")
        second = store.register("developer", "/b")
        assert second.created_at == first.created_at
        assert second.updated_at >= first.updated_at

    def test_upsert_preserves_cached_hygiene_and_pr_fields(self, store):
        store.register("developer", "/a")
        store.record_hygiene_audit("developer", disposition="clean", audited_at=42.0)
        store.set_in_flight_prs("developer", [1, 2])

        reregistered = store.register("developer", "/b", test_command="pytest")
        assert reregistered.last_hygiene_audit_at == 42.0
        assert reregistered.last_hygiene_disposition == "clean"
        assert reregistered.in_flight_pr_numbers == [1, 2]

    def test_owning_agents_accepts_bare_string(self, store):
        r = store.register("developer", "/a", owning_agents="developer-primary")
        assert r.owning_agents == ["developer-primary"]

    def test_owning_agents_accepts_list(self, store):
        r = store.register("developer", "/a", owning_agents=["a", "b"])
        assert r.owning_agents == ["a", "b"]


# ---------------------------------------------------------------------------
# DevRepoStore.get / list
# ---------------------------------------------------------------------------


class TestGet:
    def test_missing_returns_none(self, store):
        assert store.get("nope") is None

    def test_returns_registered(self, store):
        store.register("developer", "/a")
        got = store.get("developer")
        assert got is not None
        assert got.repo_path == "/a"

    def test_invalid_slug_raises(self, store):
        with pytest.raises(DevRepoError):
            store.get("BAD SLUG")

    def test_tag_gated_against_id_collision(self, store):
        # A record at the same id shape without the dev_repo tag must not
        # surface as a DevRepo (mirrors ProjectStore.get's collision guard).
        from khonliang.knowledge.store import EntryStatus, KnowledgeEntry, Tier

        store.knowledge.add(
            KnowledgeEntry(
                id="dev_repo_ghost",
                title="ghost",
                content="{}",
                tier=Tier.DERIVED,
                status=EntryStatus.DISTILLED,
                tags=["some_other_store"],
            )
        )
        assert store.get("ghost") is None


class TestList:
    def test_empty(self, store):
        assert store.list() == []

    def test_lists_all_by_default(self, store):
        store.register("alpha", "/a")
        store.register("beta", "/b")
        projects = [r.project for r in store.list()]
        assert projects == ["alpha", "beta"]

    def test_scope_filters(self, store):
        store.register("alpha", "/a")
        store.register("beta", "/b")
        store.register("gamma", "/c")
        projects = [r.project for r in store.list(scope=["alpha", "gamma"])]
        assert projects == ["alpha", "gamma"]

    def test_scope_silently_omits_unregistered(self, store):
        store.register("alpha", "/a")
        projects = [r.project for r in store.list(scope=["alpha", "unregistered"])]
        assert projects == ["alpha"]


# ---------------------------------------------------------------------------
# record_hygiene_audit / set_in_flight_prs
# ---------------------------------------------------------------------------


class TestHygieneAndPrCache:
    def test_record_hygiene_audit(self, store):
        store.register("developer", "/a")
        updated = store.record_hygiene_audit("developer", disposition="3 findings", audited_at=99.0)
        assert updated.last_hygiene_audit_at == 99.0
        assert updated.last_hygiene_disposition == "3 findings"

    def test_record_hygiene_audit_unknown_project_raises(self, store):
        with pytest.raises(DevRepoError):
            store.record_hygiene_audit("nope")

    def test_set_in_flight_prs(self, store):
        store.register("developer", "/a")
        updated = store.set_in_flight_prs("developer", [5, 6, 7])
        assert updated.in_flight_pr_numbers == [5, 6, 7]

    def test_set_in_flight_prs_unknown_project_raises(self, store):
        with pytest.raises(DevRepoError):
            store.set_in_flight_prs("nope", [1])


# ---------------------------------------------------------------------------
# resolve_fr_target
# ---------------------------------------------------------------------------


@dataclass
class _FakeFR:
    target: str


class _FakeFRStore:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, fr_id):
        return self._mapping.get(fr_id)


class TestResolveFrTarget:
    def test_requires_fr_id(self, store):
        with pytest.raises(DevRepoError):
            resolve_fr_target(_FakeFRStore({}), store, "")

    def test_unknown_fr(self, store):
        result = resolve_fr_target(_FakeFRStore({}), store, "fr_developer_deadbeef")
        assert result == {
            "fr_id": "fr_developer_deadbeef",
            "target": None,
            "resolved": False,
            "reason": "unknown fr id",
            "repo": None,
        }

    def test_fr_with_no_target(self, store):
        frs = _FakeFRStore({"fr_x_1": _FakeFR(target="")})
        result = resolve_fr_target(frs, store, "fr_x_1")
        assert result["resolved"] is False
        assert result["reason"] == "fr has no target"

    def test_fr_target_not_registered(self, store):
        frs = _FakeFRStore({"fr_x_1": _FakeFR(target="unregistered")})
        result = resolve_fr_target(frs, store, "fr_x_1")
        assert result["resolved"] is False
        assert result["reason"] == "target not registered in dev_repos"
        assert result["target"] == "unregistered"

    def test_fr_target_resolves(self, store):
        store.register("developer", "/abs/developer")
        frs = _FakeFRStore({"fr_x_1": _FakeFR(target="developer")})
        result = resolve_fr_target(frs, store, "fr_x_1")
        assert result["resolved"] is True
        assert result["repo"]["repo_path"] == "/abs/developer"

    def test_fr_target_with_invalid_slug_shape_treated_as_unregistered(self, store):
        # A target like "My Cool App" isn't a valid dev_repo slug at all —
        # still a clean "not registered" outcome, not a raised error.
        frs = _FakeFRStore({"fr_x_1": _FakeFR(target="My Cool App")})
        result = resolve_fr_target(frs, store, "fr_x_1")
        assert result["resolved"] is False
        assert result["reason"] == "target not registered in dev_repos"

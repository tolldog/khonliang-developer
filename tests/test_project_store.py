"""Tests for developer's ProjectStore.

Phase 1 scope (``fr_developer_5d0a8711``): the store primitive — create,
get, list, slug validation, duplicate detection. Cross-store migration
and skill wiring land in follow-up PRs on the same branch.
"""

from __future__ import annotations

import pytest

from khonliang.knowledge.store import (
    EntryStatus,
    KnowledgeEntry,
    KnowledgeStore,
    Tier,
)

from developer.project_store import (
    ALLOWED_ROLES,
    ENTRY_TAG,
    PROJECT_ROLE_AGENT,
    PROJECT_ROLE_APP,
    PROJECT_ROLE_LIBRARY,
    PROJECT_ROLE_SERVICE,
    PROJECT_STATUS_ACTIVE,
    PROJECT_STATUS_RETIRED,
    Project,
    ProjectDuplicateError,
    ProjectStore,
    RepoRef,
)


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "projects.db"
    return ProjectStore(KnowledgeStore(db_path=str(db)))


# ---------------------------------------------------------------------------
# Dataclass round-trip
# ---------------------------------------------------------------------------


class TestRepoRef:
    def test_to_dict_minimum(self):
        assert RepoRef(path="/x").to_dict() == {
            "path": "/x",
            "role": PROJECT_ROLE_APP,
            "install_name": "",
        }

    def test_from_dict_fills_defaults(self):
        ref = RepoRef.from_dict({"path": "/y"})
        assert ref.path == "/y"
        assert ref.role == PROJECT_ROLE_APP
        assert ref.install_name == ""

    def test_from_dict_roundtrip(self):
        original = RepoRef(path="/z", role=PROJECT_ROLE_LIBRARY, install_name="khonliang-bus-lib")
        assert RepoRef.from_dict(original.to_dict()) == original


class TestProject:
    def test_id_uses_slug(self):
        p = Project(slug="myproj")
        assert p.id == "project_myproj"

    def test_defaults(self):
        p = Project(slug="x")
        assert p.status == PROJECT_STATUS_ACTIVE
        assert p.domain == "generic"
        assert p.repos == []
        assert p.config == {}

    def test_roundtrip(self):
        original = Project(
            slug="k",
            name="Khonliang",
            domain="software-engineering",
            repos=[RepoRef(path="/a", role=PROJECT_ROLE_AGENT)],
            config={"active_branch": "main"},
            status=PROJECT_STATUS_ACTIVE,
            created_at=1.0,
            updated_at=2.0,
        )
        assert Project.from_dict(original.to_dict()) == original


# ---------------------------------------------------------------------------
# ProjectStore.create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_basic(self, store):
        p = store.create("khonliang", repos=[{"path": "/a", "role": "library"}])
        assert p.slug == "khonliang"
        assert p.status == PROJECT_STATUS_ACTIVE
        assert p.domain == "generic"
        assert p.repos[0].path == "/a"
        assert p.repos[0].role == PROJECT_ROLE_LIBRARY
        assert p.created_at > 0
        assert p.updated_at == p.created_at

    def test_accepts_string_repos(self, store):
        p = store.create("x", repos=["/a", "/b"])
        assert [r.path for r in p.repos] == ["/a", "/b"]
        # string repos default to app role
        assert all(r.role == PROJECT_ROLE_APP for r in p.repos)

    def test_accepts_repo_ref_instances(self, store):
        ref = RepoRef(path="/svc", role=PROJECT_ROLE_SERVICE)
        p = store.create("y", repos=[ref])
        assert p.repos[0].role == PROJECT_ROLE_SERVICE

    def test_name_defaults_to_slug(self, store):
        p = store.create("alpha", repos=[])
        assert p.name == "alpha"

    def test_explicit_name(self, store):
        p = store.create("alpha", repos=[], name="Alpha Project")
        assert p.name == "Alpha Project"

    def test_domain_override(self, store):
        p = store.create("g", repos=[], domain="genealogy")
        assert p.domain == "genealogy"

    def test_config_passthrough(self, store):
        p = store.create("c", repos=[], config={"max_parallel": 3})
        assert p.config == {"max_parallel": 3}

    def test_rejects_duplicate_slug(self, store):
        store.create("k", repos=[])
        with pytest.raises(ProjectDuplicateError):
            store.create("k", repos=[])

    @pytest.mark.parametrize("slug", ["", "UPPER", "has space", "slash/bad", "-leading-dash", "a" * 65])
    def test_rejects_bad_slug(self, store, slug):
        with pytest.raises(ValueError):
            store.create(slug, repos=[])

    @pytest.mark.parametrize("slug", ["k", "khonliang", "khon-liang", "khon_liang", "p1", "2nd-slot"])
    def test_accepts_valid_slug(self, store, slug):
        store.create(slug, repos=[])
        assert store.exists(slug)

    def test_rejects_bad_role(self, store):
        with pytest.raises(ValueError):
            store.create("x", repos=[{"path": "/a", "role": "misc"}])

    def test_rejects_empty_repo_path(self, store):
        with pytest.raises(ValueError):
            store.create("x", repos=[{"path": "", "role": "app"}])


# ---------------------------------------------------------------------------
# ProjectStore.get / exists
# ---------------------------------------------------------------------------


class TestGet:
    def test_roundtrip(self, store):
        created = store.create("k", repos=[{"path": "/a"}], domain="se", name="K")
        got = store.get("k")
        assert got is not None
        assert got.slug == "k"
        assert got.name == "K"
        assert got.domain == "se"
        assert len(got.repos) == 1
        assert got.repos[0].path == "/a"
        assert got.created_at == created.created_at

    def test_returns_none_when_missing(self, store):
        # Mirrors FRStore.get / BugStore.get_bug — absence is not exceptional.
        assert store.get("ghost") is None

    def test_rejects_bad_slug(self, store):
        # Invalid slug is still a raise — it signals a programming error
        # (never a match by construction), not a runtime absence.
        with pytest.raises(ValueError):
            store.get("UPPER")


class TestExists:
    def test_true_after_create(self, store):
        store.create("k", repos=[])
        assert store.exists("k")

    def test_false_for_missing(self, store):
        assert not store.exists("missing")

    def test_false_for_bad_slug(self, store):
        # Invalid slugs never match — returns False without raising.
        assert not store.exists("BAD SLUG")


# ---------------------------------------------------------------------------
# ProjectStore.list
# ---------------------------------------------------------------------------


class TestList:
    def test_empty(self, store):
        assert store.list() == []

    def test_single(self, store):
        store.create("a", repos=[])
        listed = store.list()
        assert len(listed) == 1
        assert listed[0].slug == "a"

    def test_sorted_alphabetically_by_slug(self, store):
        store.create("charlie", repos=[])
        store.create("alpha", repos=[])
        store.create("bravo", repos=[])
        slugs = [p.slug for p in store.list()]
        assert slugs == ["alpha", "bravo", "charlie"]

    def test_filters_retired_by_default(self, tmp_path):
        # retire by writing directly since there's no lifecycle skill yet.
        ks = KnowledgeStore(db_path=str(tmp_path / "x.db"))
        store = ProjectStore(ks)
        store.create("active-one", repos=[])

        # Seed a retired record by manually putting it through the store
        retired = Project(
            slug="retired-one",
            name="retired-one",
            status=PROJECT_STATUS_RETIRED,
            created_at=1.0,
            updated_at=1.0,
        )
        store._put(retired)

        visible = store.list()
        assert [p.slug for p in visible] == ["active-one"]

        with_retired = store.list(include_retired=True)
        assert sorted(p.slug for p in with_retired) == ["active-one", "retired-one"]

    def test_ignores_non_project_entries(self, tmp_path):
        # Another store's record in the same DB must not leak into list().
        ks = KnowledgeStore(db_path=str(tmp_path / "multi.db"))
        store = ProjectStore(ks)
        store.create("proj", repos=[])

        ks.add(
            KnowledgeEntry(
                id="fr_foo_deadbeef",
                title="unrelated FR record",
                content="{}",
                tier=Tier.DERIVED,
                status=EntryStatus.DISTILLED,
                tags=["fr"],
            )
        )

        listed = store.list()
        assert [p.slug for p in listed] == ["proj"]

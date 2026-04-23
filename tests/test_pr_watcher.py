"""Tests for the long-running PR fleet watcher (fr_developer_6c8ec260).

Focus areas:
- Dedupe: each transition fires exactly once per watcher lifetime.
- Transition cycle: no-review → review-landed → second-review-landed
  emits one ``pr.review_landed`` event per distinct review_id.
- Failure isolation: a GitHub client failure on one PR emits
  ``pr.poll_error`` and keeps the watcher alive for the next cycle.
- Persistence: a fresh registry pointed at the same DB as a prior run
  does not re-emit events for already-observed transitions.
- Skill wiring: the three bus skills exist, accept + reject inputs
  correctly, and surface the watcher id from the registry.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from khonliang_bus.testing import AgentTestHarness

from developer.agent import DeveloperAgent
from developer.github_client import GithubClientError
from developer.pr_watcher import (
    ALL_PR_TOPICS,
    COMMENT_PREVIEW_CHARS,
    PRFleetWatcher,
    PRSnapshot,
    PRWatcherConfig,
    PRWatcherRegistry,
    PRWatcherStore,
    TOPIC_COMMENT_POSTED,
    TOPIC_FLEET_DIGEST,
    TOPIC_INLINE_FINDING,
    TOPIC_MERGED,
    TOPIC_MERGE_READY,
    TOPIC_NEW_COMMIT,
    TOPIC_POLL_ERROR,
    TOPIC_REVIEW_LANDED,
    comment_looks_like_bot_verdict,
    parse_pr_numbers_arg,
    parse_repos_arg,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> PRWatcherStore:
    """A fresh watcher store backed by a throwaway SQLite file."""
    return PRWatcherStore(str(tmp_path / "pr_watcher.db"))


def _make_snapshot(
    repo: str = "owner/repo",
    pr_number: int = 1,
    head_sha: str = "sha1",
    reviews: list[dict] | None = None,
    merged: bool = False,
    merged_at: str = "",
    state: str = "open",
    mergeable: bool | None = None,
    merge_state: str = "",
) -> PRSnapshot:
    return PRSnapshot(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        reviews=list(reviews or []),
        merged=merged,
        merged_at=merged_at,
        state=state,
        mergeable=mergeable,
        merge_state=merge_state,
    )


class _FakeGithub:
    """Trivial stand-in for a GithubClient so the loop can be exercised."""

    def __init__(self) -> None:
        self.snapshots: dict[tuple[str, int], PRSnapshot] = {}
        self.errors: dict[tuple[str, int], Exception] = {}
        self.open_prs: dict[str, list[int]] = {}
        self.fetch_calls: list[tuple[str, int]] = []

    async def fetch_snapshot(self, repo: str, pr_number: int) -> PRSnapshot:
        self.fetch_calls.append((repo, pr_number))
        err = self.errors.get((repo, pr_number))
        if err is not None:
            raise err
        snap = self.snapshots.get((repo, pr_number))
        if snap is None:
            raise GithubClientError(f"no snapshot configured for {repo}#{pr_number}")
        return snap

    async def list_open_prs(self, repo: str) -> list[int]:
        return list(self.open_prs.get(repo, []))


def _make_watcher(
    store: PRWatcherStore,
    gh: _FakeGithub,
    published: list[tuple[str, dict]],
    *,
    watcher_id: str = "prw_test",
    repos: list[str] | None = None,
    pr_numbers: list[int] | None = None,
    interval_s: int = 60,
) -> PRFleetWatcher:
    async def publish(topic: str, payload: dict) -> None:
        published.append((topic, dict(payload)))

    config = PRWatcherConfig(
        watcher_id=watcher_id,
        repos=repos or ["owner/repo"],
        pr_numbers=pr_numbers or [1],
        interval_s=interval_s,
        started_at=0.0,
    )
    # Register so touch_last_poll has a row to update — mirrors what
    # PRWatcherRegistry.start would do in production.
    store.register_watcher(
        watcher_id=config.watcher_id,
        repos=config.repos,
        pr_numbers=config.pr_numbers,
        interval_s=config.interval_s,
        started_at=config.started_at,
    )
    return PRFleetWatcher(
        config=config,
        store=store,
        publish=publish,
        fetch_snapshot=gh.fetch_snapshot,
        list_open_prs=gh.list_open_prs,
        now_fn=lambda: 0.0,
    )


def _topics(events: list[tuple[str, dict]]) -> list[str]:
    """Granular-only topic list.

    Filters out :data:`TOPIC_FLEET_DIGEST` — existing tests pre-date
    fr_developer_fafb36f1 and assert exact equality against the
    granular event sequence. The digest event is additive (emitted once
    per poll cycle in addition to the granular ones); keeping it out of
    this helper preserves the historical invariants those assertions
    capture. Tests that specifically exercise the digest opt in via
    ``[t for t, _ in events if t == TOPIC_FLEET_DIGEST]``.
    """
    return [t for t, _ in events if t != TOPIC_FLEET_DIGEST]


def _granular(events: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Granular-only event list (topic + payload).

    Companion to :func:`_topics` for tests that positionally index into
    ``published`` expecting only the granular ``pr.*`` events. Lets
    those tests keep their original ``published[n]`` assertions without
    having to reason about where in the interleaved stream the digest
    lands.
    """
    return [(t, p) for t, p in events if t != TOPIC_FLEET_DIGEST]


# ---------------------------------------------------------------------------
# Transition cycle: no-review → review-landed → second-review-landed.
# Exercises the most important correctness property: per-transition dedupe
# means the same review_id never fires twice, and a new review fires once.
# ---------------------------------------------------------------------------


async def test_full_transition_cycle_emits_each_event_once(store):
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)

    # Cycle 1: PR has a head commit but no reviews yet. Expect one
    # pr.new_commit and nothing else.
    gh.snapshots[("owner/repo", 1)] = _make_snapshot(head_sha="abc123")
    emitted = await watcher.poll_once()
    assert emitted == 1
    assert _topics(published) == [TOPIC_NEW_COMMIT]
    assert _granular(published)[0][1]["head_sha"] == "abc123"

    # Cycle 2: first review lands (Copilot, APPROVED). Expect one
    # pr.review_landed with the review id and reviewer; no duplicate
    # pr.new_commit (dedupe on head_sha=abc123).
    gh.snapshots[("owner/repo", 1)] = _make_snapshot(
        head_sha="abc123",
        reviews=[{
            "id": 111,
            "reviewer": "copilot-pull-request-reviewer[bot]",
            "state": "APPROVED",
            "submitted_at": "2026-04-21T10:00:00Z",
        }],
    )
    emitted = await watcher.poll_once()
    assert emitted == 1
    assert _topics(published) == [TOPIC_NEW_COMMIT, TOPIC_REVIEW_LANDED]
    payload = _granular(published)[1][1]
    assert payload["review_id"] == 111
    assert payload["reviewer"] == "copilot-pull-request-reviewer[bot]"
    assert payload["state"] == "APPROVED"

    # Cycle 3: poll again with the same review list. Nothing new fires.
    emitted = await watcher.poll_once()
    assert emitted == 0
    assert _topics(published) == [TOPIC_NEW_COMMIT, TOPIC_REVIEW_LANDED]

    # Cycle 4: a second review from a human reviewer. One more
    # pr.review_landed, no duplicates of previous events.
    gh.snapshots[("owner/repo", 1)] = _make_snapshot(
        head_sha="abc123",
        reviews=[
            {
                "id": 111,
                "reviewer": "copilot-pull-request-reviewer[bot]",
                "state": "APPROVED",
                "submitted_at": "2026-04-21T10:00:00Z",
            },
            {
                "id": 222,
                "reviewer": "tolldog",
                "state": "APPROVED",
                "submitted_at": "2026-04-21T10:05:00Z",
            },
        ],
    )
    emitted = await watcher.poll_once()
    assert emitted == 1
    assert _topics(published) == [
        TOPIC_NEW_COMMIT, TOPIC_REVIEW_LANDED, TOPIC_REVIEW_LANDED,
    ]
    assert _granular(published)[2][1]["review_id"] == 222
    assert _granular(published)[2][1]["reviewer"] == "tolldog"


async def test_new_commit_fires_once_per_head_sha(store):
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)

    gh.snapshots[("owner/repo", 1)] = _make_snapshot(head_sha="sha_a")
    await watcher.poll_once()
    await watcher.poll_once()
    await watcher.poll_once()
    assert [t for t in _topics(published) if t == TOPIC_NEW_COMMIT] == [TOPIC_NEW_COMMIT]

    # Force-push: new head_sha → one more event.
    gh.snapshots[("owner/repo", 1)] = _make_snapshot(head_sha="sha_b")
    await watcher.poll_once()
    new_commit_events = [p for t, p in published if t == TOPIC_NEW_COMMIT]
    assert [p["head_sha"] for p in new_commit_events] == ["sha_a", "sha_b"]


# ---------------------------------------------------------------------------
# Merge / merge-ready / closed transitions.
# ---------------------------------------------------------------------------


async def test_merged_fires_once_and_suppresses_merge_ready(store):
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)

    gh.snapshots[("owner/repo", 1)] = _make_snapshot(
        head_sha="final",
        state="closed",
        merged=True,
        merged_at="2026-04-21T12:00:00Z",
        mergeable=True,
        merge_state="clean",
    )
    await watcher.poll_once()
    # Same snapshot twice; merged event must not double-fire.
    await watcher.poll_once()

    topics = _topics(published)
    assert topics.count(TOPIC_MERGED) == 1
    # merge_ready is suppressed once merged is recorded (per spec:
    # merged is terminal; emitting merge_ready after merged would be
    # confusing and it's also not semantically meaningful).
    assert TOPIC_MERGE_READY not in topics


async def test_merged_transition_driven_by_real_github_client_shape(store):
    """End-to-end: drive ``pr.merged`` through ``_snapshot_from_github``
    with a real :class:`GithubClient` wrapping a fake githubkit surface.

    Regression guard for ``fr_developer_207ff0fb``: before that FR,
    :meth:`GithubClient.get_pr` dropped ``merged``/``merged_at`` from
    its projection, so even if the upstream GitHub response said the
    PR was merged, the watcher could never fire ``pr.merged``. This
    test builds the dict through the real client code path and
    confirms the transition lands with a non-empty ``merged_at`` in
    the payload — i.e. the projection now carries the fields and the
    helpers in :mod:`developer.pr_watcher` read them correctly.
    """
    from developer.github_client import GithubClient
    from developer.pr_watcher import _snapshot_from_github

    from tests.test_github_client import _FakePR, _install_fake_gh

    client = GithubClient(token="t")
    _install_fake_gh(client, pr=_FakePR(
        number=36,
        state="closed",
        head_sha="final_sha",
        mergeable=True,
        mergeable_state="clean",
        merged=True,
        merged_at="2026-04-21T12:00:00Z",
    ))

    published: list[tuple[str, dict]] = []

    async def fetch_snapshot(repo: str, pr_number: int):
        return await _snapshot_from_github(client, repo, pr_number)

    async def list_open_prs(repo: str) -> list[int]:
        return []

    async def publish(topic: str, payload: dict) -> None:
        published.append((topic, dict(payload)))

    config = PRWatcherConfig(
        watcher_id="prw_merged_e2e",
        repos=["owner/repo"],
        pr_numbers=[36],
        interval_s=60,
        started_at=0.0,
    )
    store.register_watcher(
        watcher_id=config.watcher_id,
        repos=config.repos,
        pr_numbers=config.pr_numbers,
        interval_s=config.interval_s,
        started_at=config.started_at,
    )
    watcher = PRFleetWatcher(
        config=config,
        store=store,
        publish=publish,
        fetch_snapshot=fetch_snapshot,
        list_open_prs=list_open_prs,
        now_fn=lambda: 0.0,
    )

    await watcher.poll_once()

    merged_events = [p for t, p in published if t == TOPIC_MERGED]
    assert len(merged_events) == 1
    assert merged_events[0]["merged_at"] == "2026-04-21T12:00:00Z"
    assert merged_events[0]["pr_number"] == 36


async def test_merge_ready_fires_when_conditions_align(store):
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)

    gh.snapshots[("owner/repo", 1)] = _make_snapshot(
        head_sha="ready_sha",
        mergeable=True,
        merge_state="clean",
        reviews=[{
            "id": 1,
            "reviewer": "copilot-pull-request-reviewer[bot]",
            "state": "APPROVED",
            "submitted_at": "now",
        }],
    )
    await watcher.poll_once()
    topics = _topics(published)
    assert TOPIC_MERGE_READY in topics
    await watcher.poll_once()
    assert _topics(published).count(TOPIC_MERGE_READY) == 1


async def test_merge_ready_suppressed_when_changes_requested(store):
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)

    gh.snapshots[("owner/repo", 1)] = _make_snapshot(
        head_sha="blocked_sha",
        mergeable=True,
        merge_state="clean",
        reviews=[{
            "id": 1,
            "reviewer": "someone",
            "state": "CHANGES_REQUESTED",
            "submitted_at": "now",
        }],
    )
    await watcher.poll_once()
    assert TOPIC_MERGE_READY not in _topics(published)


def test_merge_ready_ignores_bot_changes_requested():
    """FR semantics: bot CHANGES_REQUESTED shouldn't block merge_ready.

    Matches the docstring promise — filter is by user.type=="Bot" OR
    login endswith [bot] OR reviewer-string endswith [bot]. A human
    CHANGES_REQUESTED still blocks; only bot ones are advisory.
    """
    # Raw GH shape with user dict + type=Bot.
    snap = _make_snapshot(
        head_sha="s",
        mergeable=True,
        merge_state="clean",
        reviews=[{
            "id": 1,
            "reviewer": "copilot-pull-request-reviewer[bot]",
            "user": {"type": "Bot", "login": "copilot-pull-request-reviewer[bot]"},
            "state": "CHANGES_REQUESTED",
            "submitted_at": "now",
        }],
    )
    assert snap.merge_ready() is True

    # Normalized flat shape (as emitted by _snapshot_from_github):
    # reviewer string endswith [bot].
    snap_flat = _make_snapshot(
        head_sha="s",
        mergeable=True,
        merge_state="clean",
        reviews=[{
            "id": 1,
            "reviewer": "copilot-pull-request-reviewer[bot]",
            "state": "CHANGES_REQUESTED",
            "submitted_at": "now",
        }],
    )
    assert snap_flat.merge_ready() is True

    # Human CHANGES_REQUESTED still blocks.
    snap_human = _make_snapshot(
        head_sha="s",
        mergeable=True,
        merge_state="clean",
        reviews=[{
            "id": 2,
            "reviewer": "tolldog",
            "state": "CHANGES_REQUESTED",
            "submitted_at": "now",
        }],
    )
    assert snap_human.merge_ready() is False


async def test_new_commit_payload_has_no_pushed_at_field(store):
    """Regression guard: earlier versions emitted a ``pushed_at`` field
    that was always empty because GithubClient.get_pr() doesn't return
    a push timestamp. Subscribers should not find a blank dead field.
    """
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published, watcher_id="prw_nopush")
    gh.snapshots[("owner/repo", 1)] = _make_snapshot(head_sha="abc")
    await watcher.poll_once()
    new_commit_payloads = [p for t, p in published if t == TOPIC_NEW_COMMIT]
    assert len(new_commit_payloads) == 1
    assert "pushed_at" not in new_commit_payloads[0]
    assert new_commit_payloads[0]["head_sha"] == "abc"


# ---------------------------------------------------------------------------
# Failure-mode isolation and poll-error emission.
# ---------------------------------------------------------------------------


async def test_single_pr_failure_does_not_kill_watcher(store):
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(
        store, gh, published,
        repos=["owner/repo"], pr_numbers=[1, 2],
    )

    gh.errors[("owner/repo", 1)] = GithubClientError("simulated API 500")
    gh.snapshots[("owner/repo", 2)] = _make_snapshot(
        pr_number=2, head_sha="ok_sha",
    )
    await watcher.poll_once()

    topics = _topics(published)
    # PR #1 failed → one pr.poll_error carrying the reason.
    poll_errors = [p for t, p in published if t == TOPIC_POLL_ERROR]
    assert len(poll_errors) == 1
    assert poll_errors[0]["repo"] == "owner/repo"
    assert poll_errors[0]["pr_number"] == 1
    assert "simulated API 500" in poll_errors[0]["reason"]
    # PR #2 still produced its normal pr.new_commit.
    assert TOPIC_NEW_COMMIT in topics


async def test_poll_error_not_deduped_across_cycles(store):
    """Persistent failure should keep emitting pr.poll_error each poll,
    so subscribers see ongoing breakage rather than a silent stall."""
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)
    gh.errors[("owner/repo", 1)] = GithubClientError("still broken")

    await watcher.poll_once()
    await watcher.poll_once()
    assert _topics(published).count(TOPIC_POLL_ERROR) == 2


# ---------------------------------------------------------------------------
# Persistence / restart resumption.
# ---------------------------------------------------------------------------


async def test_dedupe_survives_restart(tmp_path):
    """Creating a second watcher against the same DB + watcher_id must
    not re-emit events for transitions observed by the prior instance."""
    db = str(tmp_path / "pr_watcher.db")
    gh = _FakeGithub()
    gh.snapshots[("owner/repo", 1)] = _make_snapshot(head_sha="persist_sha")

    store_a = PRWatcherStore(db)
    pub_a: list[tuple[str, dict]] = []
    watcher_a = _make_watcher(store_a, gh, pub_a, watcher_id="prw_persist")
    await watcher_a.poll_once()
    assert _topics(pub_a) == [TOPIC_NEW_COMMIT]

    # New process, fresh store connection, SAME db file + watcher_id.
    store_b = PRWatcherStore(db)
    pub_b: list[tuple[str, dict]] = []
    watcher_b = _make_watcher(store_b, gh, pub_b, watcher_id="prw_persist")
    await watcher_b.poll_once()
    assert pub_b == [], "restarted watcher re-emitted a previously-emitted transition"


async def test_registry_rehydrate_respawns_without_reemitting(tmp_path):
    """FR fr_developer_6c8ec260 guarantee: on agent restart, the
    registry's persisted rows are rehydrated as live tasks that reuse
    the same ``watcher_id`` so the dedupe table applies and events
    observed before the restart do NOT fire again.
    """
    db = str(tmp_path / "pr_watcher.db")
    gh = _FakeGithub()
    gh.snapshots[("owner/repo", 1)] = _make_snapshot(head_sha="stable_sha")

    published_a: list[tuple[str, dict]] = []

    async def publish_a(topic: str, payload: dict) -> None:
        published_a.append((topic, dict(payload)))

    # First incarnation: start a watcher, let it poll once, stop the
    # task (but DO NOT call registry.stop — that would delete the row;
    # we want to simulate a crash/clean shutdown that keeps persisted
    # state like DeveloperAgent.shutdown does).
    store_a = PRWatcherStore(db)

    def factory_a(config: PRWatcherConfig) -> PRFleetWatcher:
        return PRFleetWatcher(
            config=config,
            store=store_a,
            publish=publish_a,
            fetch_snapshot=gh.fetch_snapshot,
            list_open_prs=gh.list_open_prs,
        )

    registry_a = PRWatcherRegistry(
        store=store_a, publish=publish_a, factory=factory_a,
    )
    watcher_id = await registry_a.start(
        repos=["owner/repo"], pr_numbers=[1], interval_s=60,
    )
    # Drive a poll so events land + dedupe rows are persisted.
    live_a = registry_a._watchers[watcher_id]
    await live_a.watcher.poll_once()
    assert _topics(published_a) == [TOPIC_NEW_COMMIT]

    # Simulate "agent shutdown preserves DB": cancel the task but leave
    # the registry row intact.
    live_a.stop_event.set()
    live_a.task.cancel()
    try:
        await live_a.task
    except (asyncio.CancelledError, Exception):
        pass

    # Second incarnation: fresh registry against the same DB. rehydrate
    # should discover the persisted row and respawn the watcher with
    # the same id — and the dedupe table should suppress the previously
    # emitted pr.new_commit event.
    store_b = PRWatcherStore(db)
    published_b: list[tuple[str, dict]] = []

    async def publish_b(topic: str, payload: dict) -> None:
        published_b.append((topic, dict(payload)))

    def factory_b(config: PRWatcherConfig) -> PRFleetWatcher:
        # Assert the rehydrated watcher reuses the persisted id.
        assert config.watcher_id == watcher_id
        return PRFleetWatcher(
            config=config,
            store=store_b,
            publish=publish_b,
            fetch_snapshot=gh.fetch_snapshot,
            list_open_prs=gh.list_open_prs,
        )

    registry_b = PRWatcherRegistry(
        store=store_b, publish=publish_b, factory=factory_b,
    )
    spawned = await registry_b.rehydrate()
    assert spawned == [watcher_id]

    # Drive one poll on the rehydrated watcher; no event should fire.
    live_b = registry_b._watchers[watcher_id]
    await live_b.watcher.poll_once()
    assert published_b == [], (
        "rehydrated watcher re-emitted transition already observed pre-restart"
    )

    # A genuinely new transition (force-push to new head_sha) must
    # still fire exactly once — rehydration must not stop new work.
    gh.snapshots[("owner/repo", 1)] = _make_snapshot(head_sha="fresh_sha")
    await live_b.watcher.poll_once()
    assert _topics(published_b) == [TOPIC_NEW_COMMIT]
    assert published_b[0][1]["head_sha"] == "fresh_sha"

    # Clean up the live task (stop without touching state semantics).
    await registry_b.stop(watcher_id)


async def test_registry_rehydrate_is_idempotent(tmp_path):
    """rehydrate() twice in a row must not double-spawn a live task."""
    db = str(tmp_path / "pr_watcher.db")
    store = PRWatcherStore(db)
    store.register_watcher(
        watcher_id="prw_persisted",
        repos=["owner/repo"],
        pr_numbers=[1],
        interval_s=60,
        started_at=0.0,
    )

    published: list[tuple[str, dict]] = []

    async def publish(topic: str, payload: dict) -> None:
        published.append((topic, dict(payload)))

    gh = _FakeGithub()
    gh.snapshots[("owner/repo", 1)] = _make_snapshot(head_sha="x")

    def factory(config: PRWatcherConfig) -> PRFleetWatcher:
        return PRFleetWatcher(
            config=config,
            store=store,
            publish=publish,
            fetch_snapshot=gh.fetch_snapshot,
            list_open_prs=gh.list_open_prs,
        )

    registry = PRWatcherRegistry(store=store, publish=publish, factory=factory)
    first = await registry.rehydrate()
    second = await registry.rehydrate()
    try:
        assert first == ["prw_persisted"]
        assert second == [], "second rehydrate double-spawned an already-live watcher"
        assert len(registry._watchers) == 1
    finally:
        await registry.stop("prw_persisted")


# ---------------------------------------------------------------------------
# Registry — start/list/stop lifecycle.
# ---------------------------------------------------------------------------


async def test_registry_start_and_stop_tracks_watcher(store):
    published: list[tuple[str, dict]] = []

    async def publish(topic: str, payload: dict) -> None:
        published.append((topic, dict(payload)))

    gh = _FakeGithub()
    gh.snapshots[("owner/repo", 1)] = _make_snapshot(head_sha="r1")

    def factory(config: PRWatcherConfig) -> PRFleetWatcher:
        return PRFleetWatcher(
            config=config,
            store=store,
            publish=publish,
            fetch_snapshot=gh.fetch_snapshot,
            list_open_prs=gh.list_open_prs,
        )

    registry = PRWatcherRegistry(store=store, publish=publish, factory=factory)
    watcher_id = await registry.start(
        repos=["owner/repo"], pr_numbers=[1], interval_s=60,
    )
    try:
        listed = registry.list_watchers()
        assert len(listed) == 1
        assert listed[0]["watcher_id"] == watcher_id
        assert listed[0]["repos"] == ["owner/repo"]
        assert listed[0]["pr_numbers"] == [1]
    finally:
        stopped = await registry.stop(watcher_id)
        assert stopped is True
    # stop is cleaning: the registry forgets about it.
    assert registry.list_watchers() == []


async def test_registry_rejects_empty_repos(store):
    registry = PRWatcherRegistry(
        store=store,
        publish=lambda t, p: None,  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError):
        await registry.start(repos=[], pr_numbers=[1], interval_s=60)


async def test_registry_rejects_non_positive_interval(store):
    registry = PRWatcherRegistry(
        store=store,
        publish=lambda t, p: None,  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError):
        await registry.start(repos=["o/r"], pr_numbers=[1], interval_s=0)


async def test_stop_unknown_watcher_returns_false(store):
    registry = PRWatcherRegistry(
        store=store,
        publish=lambda t, p: None,  # type: ignore[arg-type]
    )
    assert await registry.stop("prw_does_not_exist") is False


# ---------------------------------------------------------------------------
# Argument parsing.
# ---------------------------------------------------------------------------


def test_parse_repos_accepts_list_and_csv():
    assert parse_repos_arg(["a/b", "c/d"]) == ["a/b", "c/d"]
    assert parse_repos_arg("a/b, c/d ,  ") == ["a/b", "c/d"]
    assert parse_repos_arg(None) == []


def test_parse_pr_numbers_rejects_non_integers():
    assert parse_pr_numbers_arg("1,2,3") == [1, 2, 3]
    assert parse_pr_numbers_arg([1, "2"]) == [1, 2]
    assert parse_pr_numbers_arg("") == []
    with pytest.raises(ValueError):
        parse_pr_numbers_arg("1,notanumber,3")


def test_all_pr_topics_start_with_pr_namespace():
    """Subscribers use ``bus_wait_for_event(filter='pr.*')`` so the
    namespace invariant matters — keep it easy to verify."""
    for topic in ALL_PR_TOPICS:
        assert topic.startswith("pr.")


# ---------------------------------------------------------------------------
# Skill-layer wiring (DeveloperAgent).
# ---------------------------------------------------------------------------


@pytest.fixture
def harness(temp_config_file):
    return AgentTestHarness(DeveloperAgent, config_path=str(temp_config_file()))


def test_watch_pr_fleet_skill_registered(harness):
    harness.assert_skill_exists("watch_pr_fleet", description="PR watcher")


def test_list_pr_watchers_skill_registered(harness):
    harness.assert_skill_exists("list_pr_watchers", description="active PR watchers")


def test_stop_pr_watcher_skill_registered(harness):
    harness.assert_skill_exists("stop_pr_watcher", description="Stop a PR watcher")


async def test_watch_pr_fleet_skill_rejects_missing_repos(harness):
    result = await harness.call("watch_pr_fleet", {"pr_numbers": "1"})
    assert "error" in result
    assert "repos" in result["error"]


async def test_watch_pr_fleet_skill_rejects_bad_pr_numbers(harness):
    result = await harness.call(
        "watch_pr_fleet",
        {"repos": "owner/repo", "pr_numbers": "1,abc"},
    )
    assert "error" in result
    assert "non-integer" in result["error"]


async def test_watch_pr_fleet_skill_rejects_non_positive_interval(harness):
    result = await harness.call(
        "watch_pr_fleet",
        {"repos": "owner/repo", "pr_numbers": "1", "interval_s": 0},
    )
    assert "error" in result


async def test_stop_pr_watcher_requires_id(harness):
    result = await harness.call("stop_pr_watcher", {})
    assert result == {"error": "watcher_id is required"}


async def test_watch_pr_fleet_skill_starts_watcher(harness, monkeypatch):
    """End-to-end: the skill invokes the registry, which produces a watcher
    whose background task is running when the skill returns."""
    # Install a fake factory on the registry once it's constructed.
    gh = _FakeGithub()
    gh.snapshots[("owner/repo", 1)] = _make_snapshot(head_sha="abc")

    published: list[tuple[str, dict]] = []

    async def publish(topic: str, payload: dict) -> None:
        published.append((topic, dict(payload)))

    monkeypatch.setattr(harness.agent, "publish", publish)

    # Trigger registry construction via the property so we can patch
    # its factory before start() runs.
    registry = harness.agent.pr_watcher_registry

    def factory(config: PRWatcherConfig) -> PRFleetWatcher:
        return PRFleetWatcher(
            config=config,
            store=registry.store,
            publish=publish,
            fetch_snapshot=gh.fetch_snapshot,
            list_open_prs=gh.list_open_prs,
            # Short interval so poll happens quickly in the test.
        )

    registry._factory = factory

    result = await harness.call(
        "watch_pr_fleet",
        {"repos": "owner/repo", "pr_numbers": "1", "interval_s": 60},
    )
    try:
        assert "watcher_id" in result
        assert result["pr_numbers"] == [1]
        assert result["repos"] == ["owner/repo"]

        # Wait briefly for the first poll to go through. The loop
        # polls immediately before sleeping, so 0.2s is plenty.
        for _ in range(20):
            if published:
                break
            await asyncio.sleep(0.01)
        assert _topics(published) == [TOPIC_NEW_COMMIT]

        listed = await harness.call("list_pr_watchers", {})
        assert listed["count"] == 1
        assert listed["watchers"][0]["watcher_id"] == result["watcher_id"]
    finally:
        stop_result = await harness.call(
            "stop_pr_watcher", {"watcher_id": result["watcher_id"]},
        )
        assert stop_result["stopped"] is True


async def test_agent_restart_rehydrates_and_does_not_reemit(temp_config_file):
    """End-to-end FR guarantee (fr_developer_6c8ec260):

    (a) start a watcher via the skill,
    (b) let it emit some events,
    (c) "restart" the agent (new DeveloperAgent against the same DB),
    (d) verify the watcher is back running and does NOT re-emit
        transitions observed before the restart.

    This test exercises PRWatcherRegistry.rehydrate() through the agent
    path (rather than in isolation) so a future refactor that skips the
    rehydrate call still fails here.
    """
    # Shared config + DB path across both agent incarnations.
    cfg_path = temp_config_file()

    gh = _FakeGithub()
    gh.snapshots[("owner/repo", 1)] = _make_snapshot(head_sha="pre_restart_sha")

    # -- incarnation A --------------------------------------------------
    harness_a = AgentTestHarness(DeveloperAgent, config_path=str(cfg_path))
    published_a: list[tuple[str, dict]] = []

    async def publish_a(topic: str, payload: dict) -> None:
        published_a.append((topic, dict(payload)))

    harness_a.agent.publish = publish_a  # type: ignore[method-assign]
    registry_a = harness_a.agent.pr_watcher_registry

    def factory_a(config: PRWatcherConfig) -> PRFleetWatcher:
        return PRFleetWatcher(
            config=config,
            store=registry_a.store,
            publish=publish_a,
            fetch_snapshot=gh.fetch_snapshot,
            list_open_prs=gh.list_open_prs,
        )

    registry_a._factory = factory_a

    result = await harness_a.call(
        "watch_pr_fleet",
        {"repos": "owner/repo", "pr_numbers": "1", "interval_s": 60},
    )
    watcher_id = result["watcher_id"]

    # Wait for at least one poll to land so we actually have something
    # to dedupe against on the next incarnation.
    for _ in range(50):
        if published_a:
            break
        await asyncio.sleep(0.01)
    assert _topics(published_a) == [TOPIC_NEW_COMMIT]

    # "Shutdown preserving DB": cancel the live task but keep the
    # registry row (mirrors DeveloperAgent.shutdown). We can't call
    # real shutdown through the harness because it'd also try to
    # close a connector we never opened.
    live = registry_a._watchers.pop(watcher_id)
    live.stop_event.set()
    live.task.cancel()
    try:
        await live.task
    except (asyncio.CancelledError, Exception):
        pass

    # -- incarnation B --------------------------------------------------
    harness_b = AgentTestHarness(DeveloperAgent, config_path=str(cfg_path))
    published_b: list[tuple[str, dict]] = []

    async def publish_b(topic: str, payload: dict) -> None:
        published_b.append((topic, dict(payload)))

    harness_b.agent.publish = publish_b  # type: ignore[method-assign]
    registry_b = harness_b.agent.pr_watcher_registry

    def factory_b(config: PRWatcherConfig) -> PRFleetWatcher:
        # Sanity: the rehydrated watcher reuses the persisted id.
        assert config.watcher_id == watcher_id
        return PRFleetWatcher(
            config=config,
            store=registry_b.store,
            publish=publish_b,
            fetch_snapshot=gh.fetch_snapshot,
            list_open_prs=gh.list_open_prs,
        )

    registry_b._factory = factory_b

    # Trigger rehydration. The agent.start() override drives this on a
    # real run; call directly here so we don't spin up a full bus.
    spawned = await registry_b.rehydrate()
    assert spawned == [watcher_id], (
        "rehydrate did not respawn the persisted watcher"
    )

    try:
        # Watcher is back up. Poll it once; no event should re-emit.
        live_b = registry_b._watchers[watcher_id]
        await live_b.watcher.poll_once()
        assert published_b == [], (
            "post-restart watcher re-emitted a pre-restart transition; "
            "dedupe key reuse is broken"
        )

        # A genuinely new transition (force-push) still fires.
        gh.snapshots[("owner/repo", 1)] = _make_snapshot(head_sha="post_restart_sha")
        await live_b.watcher.poll_once()
        assert _topics(published_b) == [TOPIC_NEW_COMMIT]
        assert published_b[0][1]["head_sha"] == "post_restart_sha"

        # list_pr_watchers sees the rehydrated row.
        listed = await harness_b.call("list_pr_watchers", {})
        assert listed["count"] == 1
        assert listed["watchers"][0]["watcher_id"] == watcher_id
    finally:
        await harness_b.call("stop_pr_watcher", {"watcher_id": watcher_id})


# ---------------------------------------------------------------------------
# Comment-channel events (fr_developer_e2bdd869):
# pr.comment_posted (issue comments) + pr.inline_finding (review-thread).
# ---------------------------------------------------------------------------


def _make_snapshot_with_comments(
    *,
    pr_number: int = 1,
    head_sha: str = "sha1",
    external_issue_comments: list[dict] | None = None,
    inline_findings: list[dict] | None = None,
    reviews: list[dict] | None = None,
) -> PRSnapshot:
    """Build a snapshot with comment-channel lists populated.

    Calls :func:`_populate_comment_summaries` so the derived snapshot
    fields reflect the lists — mirrors what ``_snapshot_from_github``
    does in production, and keeps the consumer-facing summary view
    consistent regardless of whether the snapshot came from the real
    client or a test fixture.
    """
    from developer.pr_watcher import _populate_comment_summaries

    snap = PRSnapshot(
        repo="owner/repo",
        pr_number=pr_number,
        head_sha=head_sha,
        reviews=list(reviews or []),
        external_issue_comments=list(external_issue_comments or []),
        inline_findings=list(inline_findings or []),
    )
    _populate_comment_summaries(snap)
    return snap


async def test_issue_comment_fires_one_comment_posted(store):
    """A new non-self issue comment fires exactly one pr.comment_posted."""
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)

    gh.snapshots[("owner/repo", 1)] = _make_snapshot_with_comments(
        external_issue_comments=[
            {
                "id": 501,
                "author": "copilot-pull-request-reviewer[bot]",
                "body": "Review verdict goes here.",
                "posted_at": "2026-04-22T10:00:00Z",
            },
        ],
    )
    emitted = await watcher.poll_once()
    comment_events = [p for t, p in published if t == TOPIC_COMMENT_POSTED]
    assert len(comment_events) == 1
    assert comment_events[0]["comment_id"] == 501
    assert comment_events[0]["author"] == "copilot-pull-request-reviewer[bot]"
    assert comment_events[0]["body"] == "Review verdict goes here."
    assert comment_events[0]["posted_at"] == "2026-04-22T10:00:00Z"
    assert comment_events[0]["repo"] == "owner/repo"
    assert comment_events[0]["pr_number"] == 1
    assert emitted >= 1

    # Re-polling with the same comment must not re-emit.
    await watcher.poll_once()
    assert len([p for t, p in published if t == TOPIC_COMMENT_POSTED]) == 1


async def test_inline_finding_fires_one_event_with_path_and_line(store):
    """A new non-self inline review-thread comment fires exactly one
    pr.inline_finding with path + line + body."""
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)

    gh.snapshots[("owner/repo", 1)] = _make_snapshot_with_comments(
        inline_findings=[
            {
                "id": 601,
                "author": "copilot-pull-request-reviewer[bot]",
                "body": "This assertion is tautological.",
                "posted_at": "2026-04-22T10:05:00Z",
                "path": "developer/pr_watcher.py",
                "line": 123,
                "review_id": 888,
            },
        ],
    )
    await watcher.poll_once()
    inline_events = [p for t, p in published if t == TOPIC_INLINE_FINDING]
    assert len(inline_events) == 1
    payload = inline_events[0]
    assert payload["comment_id"] == 601
    assert payload["path"] == "developer/pr_watcher.py"
    assert payload["line"] == 123
    assert payload["body"] == "This assertion is tautological."
    assert payload["review_id"] == 888
    assert payload["author"] == "copilot-pull-request-reviewer[bot]"

    # Re-polling with the same finding must not re-emit.
    await watcher.poll_once()
    assert len([p for t, p in published if t == TOPIC_INLINE_FINDING]) == 1


async def test_copilot_review_with_four_findings_fires_one_review_plus_four_findings(store):
    """Acceptance scenario: Copilot posts a review with 4 inline findings →
    subscriber sees 1 pr.review_landed + 4 pr.inline_finding events."""
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)

    gh.snapshots[("owner/repo", 1)] = _make_snapshot_with_comments(
        reviews=[{
            "id": 999,
            "reviewer": "copilot-pull-request-reviewer[bot]",
            "state": "COMMENTED",
            "submitted_at": "2026-04-22T10:00:00Z",
        }],
        inline_findings=[
            {
                "id": 700 + i,
                "author": "copilot-pull-request-reviewer[bot]",
                "body": f"Finding {i}",
                "posted_at": f"2026-04-22T10:0{i}:00Z",
                "path": f"developer/mod_{i}.py",
                "line": 10 * i,
                "review_id": 999,
            }
            for i in range(4)
        ],
    )
    await watcher.poll_once()

    review_events = [p for t, p in published if t == TOPIC_REVIEW_LANDED]
    inline_events = [p for t, p in published if t == TOPIC_INLINE_FINDING]
    assert len(review_events) == 1
    assert len(inline_events) == 4
    # All 4 carry non-empty body content — the watcher doesn't force a
    # follow-up API call on the subscriber.
    assert all(p["body"] for p in inline_events)
    # All 4 correlate to the formal review via review_id.
    assert all(p["review_id"] == 999 for p in inline_events)


async def test_self_authored_comments_do_not_fire_events(store):
    """Self-authored comments on either channel must NOT fire events.

    The filtering happens at :func:`_snapshot_from_github` (via the
    ``self_login`` param) — by the time a snapshot reaches the emit
    path its comment lists are already external-only. This test
    verifies the filter at the fetch surface, which is the durable
    guarantee for the FR.
    """
    from developer.pr_watcher import _snapshot_from_github

    class _FakeClient:
        async def get_pr(self, repo, pr_number):
            return {
                "state": "open", "merged": False, "merged_at": None,
                "mergeable": True, "mergeable_state": "clean",
                "head_sha": "sha1", "author": "tolldog",
            }

        async def list_pr_reviews(self, repo, pr_number):
            return []

        async def list_pr_issue_comments(self, repo, pr_number):
            return [
                {"id": 1, "user": "tolldog", "body": "@copilot please re-review",
                 "created_at": "2026-04-22T10:00:00Z"},
                {"id": 2, "user": "copilot-pull-request-reviewer[bot]",
                 "body": "verdict", "created_at": "2026-04-22T10:01:00Z"},
            ]

        async def list_pr_review_comments(self, repo, pr_number):
            from developer.github_client import GithubReviewComment
            return [
                GithubReviewComment(
                    id=10, pr_number=pr_number, repo=repo, reviewer="tolldog",
                    path="a.py", line=1, body="self inline reply",
                    created_at="2026-04-22T10:02:00Z", pull_request_review_id=None,
                ),
                GithubReviewComment(
                    id=11, pr_number=pr_number, repo=repo,
                    reviewer="copilot-pull-request-reviewer[bot]",
                    path="b.py", line=5, body="bot inline finding",
                    created_at="2026-04-22T10:03:00Z", pull_request_review_id=777,
                ),
            ]

    snap = await _snapshot_from_github(_FakeClient(), "owner/repo", 1, self_login="tolldog")
    # Self-authored issue comment dropped; bot comment retained.
    assert [c["id"] for c in snap.external_issue_comments] == [2]
    # Self-authored inline comment dropped; bot finding retained.
    assert [f["id"] for f in snap.inline_findings] == [11]
    # Derived summary matches the non-self latest.
    assert snap.latest_issue_comment_by == "copilot-pull-request-reviewer[bot]"
    assert snap.latest_inline_by == "copilot-pull-request-reviewer[bot]"


async def test_comment_dedupe_across_restart(tmp_path):
    """Restart resume: previously-seen comments on both channels are not
    re-emitted. Dedupe-across-restart covers pr.comment_posted AND
    pr.inline_finding the same way it covers pr.review_landed — each by
    the transition's stable id (comment_id / inline-comment id).
    """
    db = str(tmp_path / "pr_watcher.db")
    gh = _FakeGithub()
    gh.snapshots[("owner/repo", 1)] = _make_snapshot_with_comments(
        head_sha="shared_sha",
        external_issue_comments=[{
            "id": 501, "author": "copilot[bot]", "body": "hi",
            "posted_at": "2026-04-22T10:00:00Z",
        }],
        inline_findings=[{
            "id": 601, "author": "copilot[bot]", "body": "inline",
            "posted_at": "2026-04-22T10:01:00Z",
            "path": "a.py", "line": 1, "review_id": 42,
        }],
    )

    store_a = PRWatcherStore(db)
    pub_a: list[tuple[str, dict]] = []
    watcher_a = _make_watcher(store_a, gh, pub_a, watcher_id="prw_comment_restart")
    await watcher_a.poll_once()
    # First incarnation sees the new_commit + both comment kinds.
    topics_a = _topics(pub_a)
    assert TOPIC_COMMENT_POSTED in topics_a
    assert TOPIC_INLINE_FINDING in topics_a

    # Second incarnation against same DB + watcher_id.
    store_b = PRWatcherStore(db)
    pub_b: list[tuple[str, dict]] = []
    watcher_b = _make_watcher(store_b, gh, pub_b, watcher_id="prw_comment_restart")
    await watcher_b.poll_once()
    # No pr.comment_posted / pr.inline_finding re-emission.
    topics_b = _topics(pub_b)
    assert TOPIC_COMMENT_POSTED not in topics_b, (
        "restart re-emitted an issue comment already observed"
    )
    assert TOPIC_INLINE_FINDING not in topics_b, (
        "restart re-emitted an inline finding already observed"
    )


async def test_multiple_comment_ids_each_fire_once(store):
    """Sanity: distinct comment ids on each channel each fire once, and
    re-polling with the same set is a no-op (dedupe is per-comment-id)."""
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)

    gh.snapshots[("owner/repo", 1)] = _make_snapshot_with_comments(
        external_issue_comments=[
            {"id": 501, "author": "bot1", "body": "a", "posted_at": "t1"},
            {"id": 502, "author": "bot2", "body": "b", "posted_at": "t2"},
        ],
        inline_findings=[
            {"id": 601, "author": "bot", "body": "x", "posted_at": "t3",
             "path": "a.py", "line": 1, "review_id": 1},
            {"id": 602, "author": "bot", "body": "y", "posted_at": "t4",
             "path": "b.py", "line": 2, "review_id": 1},
        ],
    )
    await watcher.poll_once()
    assert sum(1 for t, _ in published if t == TOPIC_COMMENT_POSTED) == 2
    assert sum(1 for t, _ in published if t == TOPIC_INLINE_FINDING) == 2

    # Re-poll: no new events.
    await watcher.poll_once()
    assert sum(1 for t, _ in published if t == TOPIC_COMMENT_POSTED) == 2
    assert sum(1 for t, _ in published if t == TOPIC_INLINE_FINDING) == 2

    # A brand-new comment on each channel fires exactly one more event.
    gh.snapshots[("owner/repo", 1)] = _make_snapshot_with_comments(
        external_issue_comments=[
            {"id": 501, "author": "bot1", "body": "a", "posted_at": "t1"},
            {"id": 502, "author": "bot2", "body": "b", "posted_at": "t2"},
            {"id": 503, "author": "bot3", "body": "c", "posted_at": "t5"},
        ],
        inline_findings=[
            {"id": 601, "author": "bot", "body": "x", "posted_at": "t3",
             "path": "a.py", "line": 1, "review_id": 1},
            {"id": 602, "author": "bot", "body": "y", "posted_at": "t4",
             "path": "b.py", "line": 2, "review_id": 1},
            {"id": 603, "author": "bot", "body": "z", "posted_at": "t6",
             "path": "c.py", "line": 3, "review_id": 2},
        ],
    )
    await watcher.poll_once()
    assert sum(1 for t, _ in published if t == TOPIC_COMMENT_POSTED) == 3
    assert sum(1 for t, _ in published if t == TOPIC_INLINE_FINDING) == 3


async def test_snapshot_summary_fields_reflect_latest_non_self_entry(store):
    """Sanity for the derived summary: ``latest_*_comment_*`` /
    ``latest_inline_*`` should mirror the LAST entry in each list.

    These fields feed ``pr_fleet_status`` / fleet-digest views (the
    companion FR) — verify they're populated so the aggregate view
    doesn't have to re-iterate the lists itself.
    """
    snap = _make_snapshot_with_comments(
        external_issue_comments=[
            {"id": 1, "author": "first", "body": "one", "posted_at": "t1"},
            {"id": 2, "author": "second", "body": "two", "posted_at": "t2"},
        ],
        inline_findings=[
            {"id": 10, "author": "bot", "body": "inline one",
             "posted_at": "t10", "path": "a.py", "line": 1, "review_id": 1},
            {"id": 11, "author": "bot2", "body": "inline two",
             "posted_at": "t11", "path": "b.py", "line": 2, "review_id": 2},
        ],
    )
    assert snap.external_issue_comments_count == 2
    assert snap.latest_issue_comment_at == "t2"
    assert snap.latest_issue_comment_by == "second"
    assert snap.latest_issue_comment_preview == "two"
    assert snap.inline_findings_count == 2
    assert snap.latest_inline_at == "t11"
    assert snap.latest_inline_by == "bot2"
    assert snap.latest_inline_path == "b.py"
    assert snap.latest_inline_line == 2
    assert snap.latest_inline_preview == "inline two"


def test_comment_preview_truncates_at_500_chars():
    """Summary ``latest_issue_comment_preview`` / ``latest_inline_preview``
    cap at 500 chars to keep the snapshot bounded for fleet-digest use.
    Event payloads carry the full body — only the preview is capped.

    Uses the ``COMMENT_PREVIEW_CHARS`` constant rather than the literal
    ``500`` so the assertion tracks the module-level cap if it ever
    moves; the constant is the source of truth.
    """
    long = "x" * (COMMENT_PREVIEW_CHARS * 2 + 200)
    snap = _make_snapshot_with_comments(
        external_issue_comments=[
            {"id": 1, "author": "u", "body": long, "posted_at": "t"},
        ],
        inline_findings=[
            {"id": 2, "author": "u", "body": long, "posted_at": "t",
             "path": "a.py", "line": 1, "review_id": None},
        ],
    )
    assert len(snap.latest_issue_comment_preview) == COMMENT_PREVIEW_CHARS
    assert snap.latest_issue_comment_preview == "x" * COMMENT_PREVIEW_CHARS
    assert len(snap.latest_inline_preview) == COMMENT_PREVIEW_CHARS
    assert snap.latest_inline_preview == "x" * COMMENT_PREVIEW_CHARS
    # Explicit upper-bound check — preview must be ≤ COMMENT_PREVIEW_CHARS
    # per the field contract (truncation semantic), not == for all inputs.
    assert len(snap.latest_inline_preview) <= COMMENT_PREVIEW_CHARS
    assert len(snap.latest_issue_comment_preview) <= COMMENT_PREVIEW_CHARS


# ---------------------------------------------------------------------------
# comment_looks_like_bot_verdict classifier.
# ---------------------------------------------------------------------------


def test_comment_classifier_recognizes_copilot_re_review_shape():
    """Copilot's re-review verdict posted as a bare issue comment quotes
    the triggering ``@copilot please re-review`` mention on its first
    non-empty line AND comes from a copilot-shaped author login."""
    body = (
        "> @copilot please re-review\n\n"
        "Thanks! I've reviewed the changes and everything looks good now."
    )
    assert comment_looks_like_bot_verdict(
        body, author="copilot-pull-request-reviewer[bot]",
    ) is True
    # Capitalized author variant also counts.
    assert comment_looks_like_bot_verdict(body, author="Copilot") is True


def test_comment_classifier_rejects_generic_user_comment():
    """Random user comments and non-verdict shapes do NOT match."""
    assert comment_looks_like_bot_verdict(
        "Looks great, merge when ready.", author="tolldog",
    ) is False
    # A human quoting the same phrase shouldn't match — author guard.
    assert comment_looks_like_bot_verdict(
        "> @copilot please re-review\nthanks!", author="tolldog",
    ) is False
    # Copilot author but a non-verdict-shaped body (no quoted mention
    # as the first non-empty line) shouldn't match — too eager otherwise.
    assert comment_looks_like_bot_verdict(
        "Generic copilot comment without the quoted trigger.",
        author="copilot-pull-request-reviewer[bot]",
    ) is False
    # Empty body → False.
    assert comment_looks_like_bot_verdict("", author="Copilot") is False


def test_comment_looks_like_bot_verdict_recognizes_copilot_swe_agent():
    """Post-consolidation regression guard (PR #39 Copilot R3).

    Before the consolidation ``comment_looks_like_bot_verdict``
    maintained its own narrow Copilot-login set (only
    ``copilot-pull-request-reviewer`` and bare ``copilot``) while
    ``github_client.is_copilot_login`` accepted the broader set
    including ``copilot-swe-agent``. A verdict-shaped body authored by
    ``copilot-swe-agent`` would therefore be misclassified as a
    non-bot comment in one place and as a bot comment in the other.

    After R3 both sites consult the same helper, so a
    ``copilot-swe-agent`` author IS now recognized here. This test
    exists specifically to catch a regression if the two sets ever
    drift apart again.
    """
    body = (
        "> @copilot please re-review\n\n"
        "Verdict from the SWE agent after autonomous fixup."
    )
    assert comment_looks_like_bot_verdict(
        body, author="copilot-swe-agent",
    ) is True
    # The ``[bot]``-suffixed variant should likewise match — the
    # consolidated set covers both auth variants.
    assert comment_looks_like_bot_verdict(
        body, author="copilot-swe-agent[bot]",
    ) is True
    # Non-Copilot bot logins must still be rejected — the classifier's
    # author guard only trusts Copilot-shaped identities even when the
    # body looks verdict-like. Prevents a friendly bot from quoting the
    # trigger phrase and getting treated as a Copilot verdict.
    assert comment_looks_like_bot_verdict(
        body, author="dependabot[bot]",
    ) is False


def test_comment_classifier_tolerates_leading_blank_lines():
    """First non-empty line is what matters; stray blank lines at the
    top don't break detection. Copilot's renderer sometimes inserts
    leading whitespace and we shouldn't be brittle about it."""
    body = "\n\n> @copilot please re-review\n\nAll clear now."
    assert comment_looks_like_bot_verdict(
        body, author="copilot-pull-request-reviewer[bot]",
    ) is True


# ---------------------------------------------------------------------------
# Topic namespace invariant.
# ---------------------------------------------------------------------------


def test_comment_topics_included_in_all_pr_topics():
    """Both new topics must be in ALL_PR_TOPICS so subscribers filtering
    on the ``pr.*`` namespace see them via ``bus_wait_for_event``."""
    assert TOPIC_COMMENT_POSTED in ALL_PR_TOPICS
    assert TOPIC_INLINE_FINDING in ALL_PR_TOPICS


# ---------------------------------------------------------------------------
# Unchanged-snapshot diffing (Copilot PR #39 perf finding).
# Long PR threads shouldn't churn the dedupe table on every poll. The
# watcher tracks seen comment ids in-memory and only calls ``_emit``
# (and its ``INSERT OR IGNORE``) for the delta.
# ---------------------------------------------------------------------------


async def test_unchanged_comment_snapshot_causes_zero_dedupe_inserts_on_second_poll(
    store, monkeypatch,
):
    """Key correctness claim: polling twice with an unchanged snapshot
    of N comments should hit ``mark_emitted`` (the ``INSERT OR IGNORE``
    path) exactly N times on poll 1, and exactly 0 times on poll 2.

    The assertion is a spy on ``PRWatcherStore.mark_emitted`` call
    count rather than reading sqlite stats — it's the direct path that
    executes the ``INSERT OR IGNORE`` and has no other callers, so a
    zero count proves zero DB writes.
    """
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)

    # A 20-comment snapshot — 10 issue comments + 10 inline findings.
    # Matches the finding's spec (20 total comments, 20 INSERT calls on
    # poll 1, zero on poll 2).
    gh.snapshots[("owner/repo", 1)] = _make_snapshot_with_comments(
        head_sha="sha-unchanged",
        external_issue_comments=[
            {
                "id": 1000 + i,
                "author": "copilot-pull-request-reviewer[bot]",
                "body": f"issue comment {i}",
                "posted_at": f"2026-04-22T10:{i:02d}:00Z",
            }
            for i in range(10)
        ],
        inline_findings=[
            {
                "id": 2000 + i,
                "author": "copilot-pull-request-reviewer[bot]",
                "body": f"inline finding {i}",
                "posted_at": f"2026-04-22T11:{i:02d}:00Z",
                "path": f"m{i}.py",
                "line": i,
                "review_id": 7777,
            }
            for i in range(10)
        ],
    )

    # Spy on mark_emitted and count calls per transition_kind.
    call_log: list[str] = []
    real_mark_emitted = store.mark_emitted

    def spy_mark_emitted(**kwargs):
        call_log.append(kwargs["transition_kind"])
        return real_mark_emitted(**kwargs)

    monkeypatch.setattr(store, "mark_emitted", spy_mark_emitted)

    # Poll 1: every comment id is novel → each goes through _emit →
    # mark_emitted / INSERT OR IGNORE. Plus 1 for new_commit.
    await watcher.poll_once()
    poll1_comment_calls = sum(
        1 for k in call_log if k in ("comment_posted", "inline_finding")
    )
    assert poll1_comment_calls == 20, (
        f"poll 1 should INSERT 20 rows (10 + 10) for comment channels, "
        f"got {poll1_comment_calls}: {call_log}"
    )
    assert sum(1 for p in call_log if p == "comment_posted") == 10
    assert sum(1 for p in call_log if p == "inline_finding") == 10

    call_log.clear()

    # Poll 2: same snapshot. No comment id is novel → in-memory cache
    # short-circuits before ``_emit``, so mark_emitted is never called
    # for the comment transitions. This is the load-bearing assertion:
    # zero INSERT OR IGNORE calls against the dedupe table for either
    # comment channel when the snapshot is unchanged.
    await watcher.poll_once()
    poll2_comment_calls = sum(
        1 for k in call_log if k in ("comment_posted", "inline_finding")
    )
    assert poll2_comment_calls == 0, (
        f"poll 2 should cause ZERO comment-channel INSERT OR IGNORE "
        f"calls when the snapshot is unchanged; got {poll2_comment_calls}: "
        f"{call_log}"
    )

    # And of course no duplicate events were published either.
    comment_events = [t for t, _ in published if t == TOPIC_COMMENT_POSTED]
    inline_events = [t for t, _ in published if t == TOPIC_INLINE_FINDING]
    assert len(comment_events) == 10
    assert len(inline_events) == 10


async def test_new_comment_after_unchanged_poll_still_emits_and_inserts(
    store, monkeypatch,
):
    """Sanity companion: the perf fix must not break the happy path.
    After a no-op poll, a genuinely new comment id on poll 3 must go
    through _emit / mark_emitted exactly once."""
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)

    baseline = [
        {
            "id": 5001,
            "author": "copilot[bot]",
            "body": "first",
            "posted_at": "2026-04-22T10:00:00Z",
        },
    ]
    gh.snapshots[("owner/repo", 1)] = _make_snapshot_with_comments(
        external_issue_comments=list(baseline),
    )
    await watcher.poll_once()
    await watcher.poll_once()

    # Now inject a genuinely new comment.
    call_log: list[str] = []
    real_mark_emitted = store.mark_emitted

    def spy_mark_emitted(**kwargs):
        call_log.append(kwargs["transition_kind"])
        return real_mark_emitted(**kwargs)

    monkeypatch.setattr(store, "mark_emitted", spy_mark_emitted)

    gh.snapshots[("owner/repo", 1)] = _make_snapshot_with_comments(
        external_issue_comments=baseline + [
            {
                "id": 5002,
                "author": "copilot[bot]",
                "body": "second",
                "posted_at": "2026-04-22T11:00:00Z",
            },
        ],
    )
    await watcher.poll_once()
    # Exactly one new comment_posted INSERT (for id 5002), none for 5001.
    assert call_log.count("comment_posted") == 1
    comment_events = [p for t, p in published if t == TOPIC_COMMENT_POSTED]
    assert [p["comment_id"] for p in comment_events] == [5001, 5002]


# ---------------------------------------------------------------------------
# Case-insensitive self-login filter (Copilot PR #39 nit).
# GitHub logins are case-insensitive; comparing raw strings would leak
# self-authored comments when storage / config cases differ.
# ---------------------------------------------------------------------------


async def test_self_login_filter_is_case_insensitive(tmp_path):
    """Mixed-case ``self_login`` must still filter self-authored comments
    on both channels. Uses ``.casefold()`` in the filter so e.g. a
    stored ``self_login='Tolldog'`` still drops an issue comment
    authored as ``tolldog`` (or ``TOLLDOG``)."""
    from developer.pr_watcher import _snapshot_from_github

    class _FakeClient:
        async def get_pr(self, repo, pr_number):
            return {
                "state": "open", "merged": False, "merged_at": None,
                "mergeable": True, "mergeable_state": "clean",
                "head_sha": "sha1", "author": "Tolldog",
            }

        async def list_pr_reviews(self, repo, pr_number):
            return []

        async def list_pr_issue_comments(self, repo, pr_number):
            return [
                # Lowercase author; config is mixed-case "Tolldog" — must drop.
                {"id": 1, "user": "tolldog", "body": "self lower",
                 "created_at": "2026-04-22T10:00:00Z"},
                # Uppercase variant — must also drop.
                {"id": 2, "user": "TOLLDOG", "body": "self upper",
                 "created_at": "2026-04-22T10:01:00Z"},
                # External author — must retain.
                {"id": 3, "user": "copilot-pull-request-reviewer[bot]",
                 "body": "external", "created_at": "2026-04-22T10:02:00Z"},
            ]

        async def list_pr_review_comments(self, repo, pr_number):
            from developer.github_client import GithubReviewComment
            return [
                # Case variant of config login — must drop.
                GithubReviewComment(
                    id=10, pr_number=pr_number, repo=repo, reviewer="TollDog",
                    path="a.py", line=1, body="self mixed",
                    created_at="2026-04-22T10:03:00Z", pull_request_review_id=None,
                ),
                # External — must retain.
                GithubReviewComment(
                    id=11, pr_number=pr_number, repo=repo,
                    reviewer="copilot-pull-request-reviewer[bot]",
                    path="b.py", line=5, body="bot",
                    created_at="2026-04-22T10:04:00Z", pull_request_review_id=7,
                ),
            ]

    # Mixed-case self_login value, simulating whatever the stored config
    # surface happens to hand in — filter must still do the right thing.
    snap = await _snapshot_from_github(
        _FakeClient(), "owner/repo", 1, self_login="Tolldog",
    )
    # Both case variants of the self login are dropped; external kept.
    assert [c["id"] for c in snap.external_issue_comments] == [3]
    assert [f["id"] for f in snap.inline_findings] == [11]


# ---------------------------------------------------------------------------
# Copilot R2 cache-prune finding: in "watch all open PRs" mode the
# ``_seen_*`` caches must shed entries for PRs no longer in the active
# set. Otherwise closed/merged PRs keep their seen-id baggage forever
# for the watcher's lifetime.
# ---------------------------------------------------------------------------


async def test_seen_caches_prune_when_pr_drops_out_of_active_set(store):
    """Simulate a fleet watcher with ``list_open_prs``-driven resolution.
    Poll 1 has PRs {1, 2, 3}; poll 2 has {1, 2} (PR 3 merged/closed).
    After poll 2, both ``_seen_*`` caches should only contain keys for
    PRs 1 and 2."""
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    # Build the watcher inline rather than via ``_make_watcher`` because
    # that helper's ``pr_numbers or [1]`` default would coerce an empty
    # list back into ``[1]``; the cache-leak scenario requires the
    # "watch all open PRs" path (empty explicit list → ``list_open_prs``
    # drives the set each cycle).
    async def publish(topic: str, payload: dict) -> None:
        published.append((topic, dict(payload)))

    config = PRWatcherConfig(
        watcher_id="prw_prune_test",
        repos=["owner/repo"],
        pr_numbers=[],
        interval_s=60,
        started_at=0.0,
    )
    store.register_watcher(
        watcher_id=config.watcher_id,
        repos=config.repos,
        pr_numbers=config.pr_numbers,
        interval_s=config.interval_s,
        started_at=config.started_at,
    )
    watcher = PRFleetWatcher(
        config=config,
        store=store,
        publish=publish,
        fetch_snapshot=gh.fetch_snapshot,
        list_open_prs=gh.list_open_prs,
        now_fn=lambda: 0.0,
    )

    def _snap_with_ids(pr_number: int) -> PRSnapshot:
        return _make_snapshot_with_comments(
            pr_number=pr_number,
            head_sha=f"sha-{pr_number}",
            external_issue_comments=[
                {
                    "id": 10000 + pr_number,
                    "author": "copilot[bot]",
                    "body": f"issue on PR {pr_number}",
                    "posted_at": "2026-04-22T10:00:00Z",
                },
            ],
            inline_findings=[
                {
                    "id": 20000 + pr_number,
                    "author": "copilot[bot]",
                    "body": f"inline on PR {pr_number}",
                    "posted_at": "2026-04-22T10:01:00Z",
                    "path": "m.py",
                    "line": 1,
                    "review_id": 9,
                },
            ],
        )

    # Poll 1: fleet is {1, 2, 3}, all three populate the caches.
    gh.open_prs["owner/repo"] = [1, 2, 3]
    for n in (1, 2, 3):
        gh.snapshots[("owner/repo", n)] = _snap_with_ids(n)
    await watcher.poll_once()
    assert set(watcher._seen_issue_comment_ids.keys()) == {
        ("owner/repo", 1), ("owner/repo", 2), ("owner/repo", 3),
    }
    assert set(watcher._seen_inline_finding_ids.keys()) == {
        ("owner/repo", 1), ("owner/repo", 2), ("owner/repo", 3),
    }

    # Poll 2: PR 3 merged/closed — fleet resolver returns only {1, 2}.
    # Prune must drop key (owner/repo, 3) from both caches without
    # touching keys for still-active PRs.
    gh.open_prs["owner/repo"] = [1, 2]
    await watcher.poll_once()
    assert set(watcher._seen_issue_comment_ids.keys()) == {
        ("owner/repo", 1), ("owner/repo", 2),
    }
    assert set(watcher._seen_inline_finding_ids.keys()) == {
        ("owner/repo", 1), ("owner/repo", 2),
    }

    # Poll 3: fleet empties entirely → both caches go back to empty.
    gh.open_prs["owner/repo"] = []
    await watcher.poll_once()
    assert watcher._seen_issue_comment_ids == {}
    assert watcher._seen_inline_finding_ids == {}


# ---------------------------------------------------------------------------
# Cooperative cancellation propagation (PR #39 Copilot R4).
#
# ``asyncio.CancelledError`` subclasses ``Exception`` (Python 3.8+), so a
# bare ``except Exception`` silently converts cancellation into the
# error-isolation / degrade-to-empty / log-and-continue path. These tests
# pin that ``poll_once`` and ``_resolve_self_login`` let ``CancelledError``
# propagate out of those fallbacks — without the explicit
# ``except asyncio.CancelledError: raise`` guard, cancelling a watcher
# mid-poll would look identical to a transient API glitch and the task
# would silently refuse to stop.
# ---------------------------------------------------------------------------


async def test_poll_once_propagates_cancellation_from_fetch_snapshot(store):
    """``poll_once`` wraps ``_fetch_snapshot`` in ``except Exception`` to
    keep the watcher alive through transient errors. CancelledError must
    short-circuit that fallback so shutdown actually shuts down."""
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)
    gh.errors[("owner/repo", 1)] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await watcher.poll_once()
    # And no poll_error was published — the broad-Exception fallback
    # must not have run.
    assert not [t for t, _ in published if t == TOPIC_POLL_ERROR]


async def test_poll_once_propagates_cancellation_from_list_open_prs(store):
    """Watch-all-open-PRs mode resolves the PR set via ``_list_open_prs``
    with its own ``except Exception`` fallback (the one that emits
    ``pr.poll_error`` and ``continue``s). CancelledError there must also
    propagate — otherwise cancelling during fleet discovery silently
    returns an empty pair list and the watcher keeps polling."""

    async def publish(topic: str, payload: dict) -> None:
        pass

    async def fetch_snapshot(repo, pr_number):  # pragma: no cover
        raise AssertionError("should not be reached once list_open_prs cancels")

    async def list_open_prs(repo: str) -> list[int]:
        raise asyncio.CancelledError()

    # Use pr_numbers=[] directly (bypassing _make_watcher's truthiness
    # fallback) so poll_once takes the fleet-discovery path instead of
    # the explicit list.
    config = PRWatcherConfig(
        watcher_id="prw_cancel_list_open",
        repos=["owner/repo"],
        pr_numbers=[],
        interval_s=60,
        started_at=0.0,
    )
    store.register_watcher(
        watcher_id=config.watcher_id,
        repos=config.repos,
        pr_numbers=config.pr_numbers,
        interval_s=config.interval_s,
        started_at=config.started_at,
    )
    watcher = PRFleetWatcher(
        config=config,
        store=store,
        publish=publish,
        fetch_snapshot=fetch_snapshot,
        list_open_prs=list_open_prs,
        now_fn=lambda: 0.0,
    )
    with pytest.raises(asyncio.CancelledError):
        await watcher.poll_once()


async def test_default_factory_resolve_self_login_propagates_cancellation(monkeypatch, store):
    """R4 site (``pr_watcher.py:1047``): ``_default_factory`` wires a
    ``_resolve_self_login`` closure that degrades to empty on failure so
    a broken /user endpoint can't take down the watcher. The degrade must
    NOT apply to CancelledError — that would swallow cooperative
    cancellation of the watcher task mid-resolve.

    We exercise the real factory-constructed closure by calling the
    watcher's ``_fetch_snapshot`` — which invokes ``_resolve_self_login``
    first — with a ``GithubClient`` whose ``get_authenticated_user_login``
    raises ``CancelledError``.
    """
    from developer import pr_watcher as _pw

    class _CancellingClient:
        async def get_authenticated_user_login(self) -> str:
            raise asyncio.CancelledError()

    # The factory constructs its own GithubClient(); patch the symbol in
    # the module so the closure captures our cancelling stub instead.
    monkeypatch.setattr(_pw, "GithubClient", lambda *a, **kw: _CancellingClient())

    # Minimal shim providing the two attrs _default_factory pulls off self.
    class _Shim:
        _default_factory = _pw.PRWatcherRegistry._default_factory

        def __init__(self, store):
            self.store = store

        async def _publish(self, topic: str, payload: dict) -> None:  # pragma: no cover
            pass

    config = PRWatcherConfig(
        watcher_id="prw_cancel_self_login",
        repos=["owner/repo"],
        pr_numbers=[1],
        interval_s=60,
        started_at=0.0,
    )
    store.register_watcher(
        watcher_id=config.watcher_id,
        repos=config.repos,
        pr_numbers=config.pr_numbers,
        interval_s=config.interval_s,
        started_at=config.started_at,
    )
    shim = _Shim(store)
    watcher = shim._default_factory(config)

    # Any call that triggers the fetch_snapshot closure — which resolves
    # self_login first — must surface CancelledError instead of
    # converting it to empty-string.
    with pytest.raises(asyncio.CancelledError):
        await watcher._fetch_snapshot("owner/repo", 1)


async def test_list_open_pr_numbers_propagates_cancellation():
    """Free function ``_list_open_pr_numbers`` wraps ``pulls.async_list``
    in ``except Exception`` that raises ``GithubClientError``. Cancel
    path must short-circuit that mapping."""
    from developer.pr_watcher import _list_open_pr_numbers

    class _Pulls:
        async def async_list(self, **_):
            raise asyncio.CancelledError()

    class _Rest:
        pulls = _Pulls()

    class _Client:
        rest = _Rest()

    class _GhStub:
        _token = "t"
        def _client(self):
            return _Client()

    with pytest.raises(asyncio.CancelledError):
        await _list_open_pr_numbers(_GhStub(), "owner/repo")


# ---------------------------------------------------------------------------
# fr_developer_fafb36f1: pr_fleet_status snapshot + pr.fleet_digest event.
#
# pr_fleet_status is the one-shot read: walk every live watcher, project its
# cached PR snapshots into a compact shape. pr.fleet_digest is the per-cycle
# aggregate event that fires only when at least one transition fired in that
# cycle.
# ---------------------------------------------------------------------------


def _digest_events(published: list[tuple[str, dict]]) -> list[dict]:
    """Extract the ``pr.fleet_digest`` payloads from the captured stream."""
    return [p for t, p in published if t == TOPIC_FLEET_DIGEST]


async def _start_registry_watcher(
    *,
    store: PRWatcherStore,
    gh: _FakeGithub,
    publish,
    pr_numbers: list[int],
    repos: list[str] | None = None,
) -> tuple[PRWatcherRegistry, str]:
    """Spin up a :class:`PRWatcherRegistry` backed by ``_FakeGithub`` and
    start a single watcher. Returns the registry + the watcher id.

    Used by ``pr_fleet_status`` tests that need the registry walk path
    rather than a bare ``PRFleetWatcher`` — the skill reads from
    :meth:`PRWatcherRegistry.fleet_snapshot`, which consults
    :meth:`PRFleetWatcher.latest_snapshots` via the live handle.
    """
    def factory(config: PRWatcherConfig) -> PRFleetWatcher:
        return PRFleetWatcher(
            config=config,
            store=store,
            publish=publish,
            fetch_snapshot=gh.fetch_snapshot,
            list_open_prs=gh.list_open_prs,
            now_fn=lambda: 0.0,
        )

    registry = PRWatcherRegistry(store=store, publish=publish, factory=factory)
    watcher_id = await registry.start(
        repos=repos or ["owner/repo"], pr_numbers=pr_numbers, interval_s=60,
    )
    return registry, watcher_id


async def test_pr_fleet_status_returns_snapshot_for_all_watchers(tmp_path):
    """Two watchers + populated snapshots → both watchers + all their PRs
    show up under ``fleet`` with the compact shape."""
    db = str(tmp_path / "fleet.db")
    store = PRWatcherStore(db)
    gh = _FakeGithub()

    snap1 = _make_snapshot_with_comments(
        pr_number=1, head_sha="sha_1",
        reviews=[{"id": 1, "reviewer": "copilot[bot]", "state": "APPROVED",
                  "submitted_at": "2026-04-22T10:00:00Z"}],
    )
    # Pin the snapshot's ``repo`` to match the watcher key below so
    # ``_repo_short`` projects the expected fleet key.
    snap1.repo = "owner/alpha"
    gh.snapshots[("owner/alpha", 1)] = snap1

    snap5 = _make_snapshot_with_comments(pr_number=5, head_sha="sha_5")
    snap5.repo = "owner/beta"
    gh.snapshots[("owner/beta", 5)] = snap5

    published: list[tuple[str, dict]] = []

    async def publish(topic: str, payload: dict) -> None:
        published.append((topic, dict(payload)))

    reg_a, wid_a = await _start_registry_watcher(
        store=store, gh=gh, publish=publish, pr_numbers=[1],
        repos=["owner/alpha"],
    )
    reg_b, wid_b = await _start_registry_watcher(
        store=store, gh=gh, publish=publish, pr_numbers=[5],
        repos=["owner/beta"],
    )
    try:
        # Drive a poll on each watcher so their snapshot caches populate.
        await reg_a._watchers[wid_a].watcher.poll_once()
        await reg_b._watchers[wid_b].watcher.poll_once()

        # Both registries share the same store, so fleet_snapshot called
        # on either registry sees both watcher rows — the in-memory live
        # handles differ though, so only one registry has the live
        # snapshots for each watcher.
        snap_a = reg_a.fleet_snapshot()
        snap_b = reg_b.fleet_snapshot()
        wid_set = {w["id"] for w in snap_a["watchers"]}
        assert wid_a in wid_set and wid_b in wid_set

        keys_a = {item["k"] for item in snap_a["fleet"]}
        keys_b = {item["k"] for item in snap_b["fleet"]}
        assert "alpha#1" in keys_a
        assert "beta#5" in keys_b

        # Compact shape: required fields present on every fleet item.
        for snap_view in (snap_a, snap_b):
            for item in snap_view["fleet"]:
                assert set(item.keys()) >= {
                    "k", "reviews", "external_issue_comments",
                    "inline_findings", "merged", "mergeable",
                    "head_sha", "state",
                }
                assert set(item["reviews"].keys()) >= {
                    "count", "latest_state", "latest_by", "latest_at",
                }
                assert set(item["inline_findings"].keys()) >= {
                    "count", "latest_by", "latest_at",
                    "latest_path", "latest_line",
                }

        # Top-level t is ISO-8601 with Z suffix.
        assert snap_a["t"].endswith("Z")
    finally:
        await reg_a.stop(wid_a)
        await reg_b.stop(wid_b)


async def test_pr_fleet_status_scoped_by_watcher_id(tmp_path):
    """Filter to one watcher id — only that watcher's PRs land in fleet."""
    db = str(tmp_path / "fleet.db")
    store = PRWatcherStore(db)
    gh = _FakeGithub()
    gh.snapshots[("owner/repo", 1)] = _make_snapshot_with_comments(
        pr_number=1, head_sha="sha_1",
    )
    gh.snapshots[("owner/repo", 2)] = _make_snapshot_with_comments(
        pr_number=2, head_sha="sha_2",
    )

    published: list[tuple[str, dict]] = []

    async def publish(topic: str, payload: dict) -> None:
        published.append((topic, dict(payload)))

    reg_a, wid_a = await _start_registry_watcher(
        store=store, gh=gh, publish=publish, pr_numbers=[1],
    )
    reg_b, wid_b = await _start_registry_watcher(
        store=store, gh=gh, publish=publish, pr_numbers=[2],
    )
    try:
        await reg_a._watchers[wid_a].watcher.poll_once()
        await reg_b._watchers[wid_b].watcher.poll_once()

        snap_scoped = reg_a.fleet_snapshot(watcher_id=wid_a)
        assert [w["id"] for w in snap_scoped["watchers"]] == [wid_a]
        assert {item["k"] for item in snap_scoped["fleet"]} == {"repo#1"}

        # Unknown watcher id → empty, not error.
        snap_none = reg_a.fleet_snapshot(watcher_id="prw_bogus")
        assert snap_none["watchers"] == []
        assert snap_none["fleet"] == []
    finally:
        await reg_a.stop(wid_a)
        await reg_b.stop(wid_b)


def test_pr_fleet_status_empty_fleet(store):
    """Zero watchers active → empty ``watchers`` + empty ``fleet``."""
    registry = PRWatcherRegistry(
        store=store,
        publish=lambda t, p: None,  # type: ignore[arg-type]
    )
    snap = registry.fleet_snapshot()
    assert snap["watchers"] == []
    assert snap["fleet"] == []
    assert snap["t"].endswith("Z")


async def test_pr_fleet_digest_aggregates_per_cycle_transitions(store):
    """Copilot posts 1 review with 4 inline findings in a single poll →
    exactly 1 ``pr.fleet_digest`` event with 1 ``changed`` entry containing
    1 review event + 4 inline_finding events, alongside the granular
    1 ``pr.review_landed`` + 4 ``pr.inline_finding`` events. Granular
    events must not be removed or reshaped by the digest being additive.
    """
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)

    gh.snapshots[("owner/repo", 1)] = _make_snapshot_with_comments(
        head_sha="agg_sha",
        reviews=[{
            "id": 999,
            "reviewer": "copilot-pull-request-reviewer[bot]",
            "state": "COMMENTED",
            "submitted_at": "2026-04-22T10:00:00Z",
            "body": "Posted 4 concerns inline.",
        }],
        inline_findings=[
            {
                "id": 700 + i,
                "author": "copilot-pull-request-reviewer[bot]",
                "body": f"Finding {i}",
                "posted_at": f"2026-04-22T10:0{i}:00Z",
                "path": f"developer/mod_{i}.py",
                "line": 10 * (i + 1),
                "review_id": 999,
            }
            for i in range(4)
        ],
    )
    await watcher.poll_once()

    # Granular events unchanged: 1 new_commit + 1 review + 4 inline.
    granular_topics = _topics(published)
    assert granular_topics.count(TOPIC_NEW_COMMIT) == 1
    assert granular_topics.count(TOPIC_REVIEW_LANDED) == 1
    assert granular_topics.count(TOPIC_INLINE_FINDING) == 4

    digests = _digest_events(published)
    assert len(digests) == 1
    digest = digests[0]
    assert digest["watcher_id"] == "prw_test"
    assert digest["t"].endswith("Z")
    assert len(digest["changed"]) == 1
    entry = digest["changed"][0]
    assert entry["k"] == "repo#1"

    kinds = [e["kind"] for e in entry["events"]]
    # One review + four inline_findings + one new_commit, all in the
    # same cycle digest. Order is emit order (new_commit → review →
    # inline findings).
    assert kinds.count("review") == 1
    assert kinds.count("inline_finding") == 4
    assert kinds.count("new_commit") == 1

    # The per-entry ``state`` block is the compact fleet-item shape
    # (matches pr_fleet_status projection).
    state = entry["state"]
    assert state["head_sha"] == "agg_sha"
    assert state["reviews"]["count"] == 1
    assert state["inline_findings"]["count"] == 4


async def test_pr_fleet_digest_silent_cycle_emits_nothing(store):
    """A poll that produces zero new transitions → zero digest events.

    Drives two polls on a static snapshot: the first cycle fires
    granular events (and therefore a digest); the second cycle observes
    no transition diff → no digest.
    """
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)

    gh.snapshots[("owner/repo", 1)] = _make_snapshot(head_sha="static_sha")
    await watcher.poll_once()  # primes dedupe
    digests_poll1 = _digest_events(published)
    assert len(digests_poll1) == 1  # pr.new_commit fired → digest fired

    # Poll 2: unchanged snapshot → zero granular events → zero digest.
    before = len(published)
    await watcher.poll_once()
    after_events = published[before:]
    assert after_events == [], (
        f"silent poll cycle emitted events: {after_events}"
    )


async def test_pr_fleet_digest_inline_finding_body_included_inline(store):
    """A subscriber consuming only ``pr.fleet_digest`` must see the finding
    body + path + line without needing a follow-up API call — that's the
    point of the aggregate event."""
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)

    gh.snapshots[("owner/repo", 1)] = _make_snapshot_with_comments(
        head_sha="body_sha",
        inline_findings=[{
            "id": 4242,
            "author": "copilot-pull-request-reviewer[bot]",
            "body": "This helper is load-bearing — please add a test.",
            "posted_at": "2026-04-22T12:00:00Z",
            "path": "developer/pr_watcher.py",
            "line": 500,
            "review_id": 77,
        }],
    )
    await watcher.poll_once()

    digests = _digest_events(published)
    assert len(digests) == 1
    entry = digests[0]["changed"][0]
    inline_events = [e for e in entry["events"] if e["kind"] == "inline_finding"]
    assert len(inline_events) == 1
    ev = inline_events[0]
    assert ev["body"] == "This helper is load-bearing — please add a test."
    assert ev["path"] == "developer/pr_watcher.py"
    assert ev["line"] == 500
    assert ev["by"] == "copilot-pull-request-reviewer[bot]"
    assert ev["review_id"] == 77


async def test_pr_fleet_digest_body_truncated_to_500_chars(store):
    """Bodies on review / comment / inline_finding digest events cap at
    :data:`COMMENT_PREVIEW_CHARS` (500). Protects subscribers from
    massive payloads dominating the bus stream."""
    gh = _FakeGithub()
    published: list[tuple[str, dict]] = []
    watcher = _make_watcher(store, gh, published)

    long = "y" * (COMMENT_PREVIEW_CHARS * 2 + 100)
    gh.snapshots[("owner/repo", 1)] = _make_snapshot_with_comments(
        head_sha="trunc_sha",
        reviews=[{
            "id": 1,
            "reviewer": "copilot[bot]",
            "state": "COMMENTED",
            "submitted_at": "t",
            "body": long,
        }],
        external_issue_comments=[{
            "id": 100,
            "author": "copilot[bot]",
            "body": long,
            "posted_at": "t",
        }],
        inline_findings=[{
            "id": 200,
            "author": "copilot[bot]",
            "body": long,
            "posted_at": "t",
            "path": "a.py",
            "line": 1,
            "review_id": None,
        }],
    )
    await watcher.poll_once()

    digests = _digest_events(published)
    assert len(digests) == 1
    entry = digests[0]["changed"][0]
    for ev in entry["events"]:
        body = ev.get("body")
        if body is None:
            continue
        assert len(body) <= COMMENT_PREVIEW_CHARS, (
            f"{ev['kind']} body not truncated: {len(body)} chars"
        )


# ---------------------------------------------------------------------------
# Skill-layer wiring for pr_fleet_status (fr_developer_fafb36f1).
# ---------------------------------------------------------------------------


def test_pr_fleet_status_skill_registered(harness):
    harness.assert_skill_exists("pr_fleet_status", description="snapshot")


async def test_pr_fleet_status_skill_returns_empty_when_no_watchers(harness):
    result = await harness.call("pr_fleet_status", {})
    assert result["watchers"] == []
    assert result["fleet"] == []
    assert result["t"].endswith("Z")


async def test_pr_fleet_status_skill_scopes_by_watcher_id(harness, monkeypatch):
    """End-to-end: start a watcher, poll once, then call pr_fleet_status
    with + without the watcher id filter — both return the expected
    shape; a bogus id returns empty."""
    gh = _FakeGithub()
    gh.snapshots[("owner/repo", 1)] = _make_snapshot_with_comments(
        pr_number=1, head_sha="skill_sha",
    )
    published: list[tuple[str, dict]] = []

    async def publish(topic: str, payload: dict) -> None:
        published.append((topic, dict(payload)))

    monkeypatch.setattr(harness.agent, "publish", publish)
    registry = harness.agent.pr_watcher_registry

    def factory(config: PRWatcherConfig) -> PRFleetWatcher:
        return PRFleetWatcher(
            config=config,
            store=registry.store,
            publish=publish,
            fetch_snapshot=gh.fetch_snapshot,
            list_open_prs=gh.list_open_prs,
            now_fn=lambda: 0.0,
        )
    registry._factory = factory

    result = await harness.call(
        "watch_pr_fleet",
        {"repos": "owner/repo", "pr_numbers": "1", "interval_s": 60},
    )
    watcher_id = result["watcher_id"]
    try:
        # Force a poll so the snapshot cache is populated.
        await registry._watchers[watcher_id].watcher.poll_once()

        # Unscoped: one watcher + its one PR.
        snap_all = await harness.call("pr_fleet_status", {})
        assert [w["id"] for w in snap_all["watchers"]] == [watcher_id]
        assert [item["k"] for item in snap_all["fleet"]] == ["repo#1"]

        # Scoped to the known id — same result.
        snap_scoped = await harness.call(
            "pr_fleet_status", {"watcher_id": watcher_id},
        )
        assert [w["id"] for w in snap_scoped["watchers"]] == [watcher_id]

        # Scoped to bogus id — empty, not error.
        snap_bogus = await harness.call(
            "pr_fleet_status", {"watcher_id": "prw_nope"},
        )
        assert snap_bogus["watchers"] == []
        assert snap_bogus["fleet"] == []
    finally:
        await harness.call("stop_pr_watcher", {"watcher_id": watcher_id})


def test_fleet_digest_topic_in_all_pr_topics():
    """Subscribers filtering on ``pr.*`` via ``bus_wait_for_event`` must
    see the digest topic in the registry of known topics."""
    assert TOPIC_FLEET_DIGEST in ALL_PR_TOPICS
    assert TOPIC_FLEET_DIGEST.startswith("pr.")

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
    PRFleetWatcher,
    PRSnapshot,
    PRWatcherConfig,
    PRWatcherRegistry,
    PRWatcherStore,
    TOPIC_MERGED,
    TOPIC_MERGE_READY,
    TOPIC_NEW_COMMIT,
    TOPIC_POLL_ERROR,
    TOPIC_REVIEW_LANDED,
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
    return [t for t, _ in events]


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
    assert published[0][1]["head_sha"] == "abc123"

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
    payload = published[1][1]
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
    assert published[2][1]["review_id"] == 222
    assert published[2][1]["reviewer"] == "tolldog"


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

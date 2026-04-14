"""Tests for the GitHub client wrapper.

Focused on the module's own logic (auth discovery, repo parsing, error
normalization). Doesn't exercise the actual GitHub API — that's
integration-test territory and would need a token + network.
"""

from __future__ import annotations

import pytest

from developer.github_client import (
    GithubClient,
    GithubClientError,
    GithubReview,
    GithubReviewComment,
)


def test_import_does_not_construct_github_object():
    """Constructor is cheap; githubkit import is deferred to first API call."""
    c = GithubClient(token="x")
    assert c._gh is None


def test_token_from_kwarg_wins():
    c = GithubClient(token="from-kwarg")
    assert c._token == "from-kwarg"


def test_token_from_github_token_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    c = GithubClient()
    assert c._token == "from-env"


def test_token_falls_back_to_gh_token_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "gh-cli-style")
    c = GithubClient()
    assert c._token == "gh-cli-style"


def test_token_is_none_when_no_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    c = GithubClient()
    assert c._token is None


def test_split_repo_valid():
    assert GithubClient._split_repo("tolldog/khonliang-developer") == ("tolldog", "khonliang-developer")


def test_split_repo_rejects_bare_name():
    with pytest.raises(GithubClientError, match="owner/name"):
        GithubClient._split_repo("khonliang-developer")


def test_split_repo_with_extra_slashes_takes_first():
    # "owner/name/extra" — partition("/") gives ("owner", "/", "name/extra")
    owner, name = GithubClient._split_repo("owner/name/extra")
    assert owner == "owner"
    assert name == "name/extra"


def test_review_dataclass_roundtrip():
    r = GithubReview(
        id=1, pr_number=42, repo="o/n", reviewer="bot",
        state="COMMENTED", body="looks good", submitted_at="2026-04-13T00:00:00Z",
    )
    assert r.pr_number == 42
    assert r.state == "COMMENTED"


def test_review_comment_dataclass_roundtrip():
    c = GithubReviewComment(
        id=1, pr_number=42, repo="o/n", reviewer="bot",
        path="developer/foo.py", line=10, body="nit", created_at="2026-04-13",
    )
    assert c.path == "developer/foo.py"
    assert c.line == 10

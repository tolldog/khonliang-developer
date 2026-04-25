"""Pure-logic tests for developer.fr_drafting.

Covers the deterministic helpers (tokenization, classification,
priority inference, evidence scan, description composition) plus
``compose_draft`` end-to-end with stubbed brief/scan callables.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from developer import fr_drafting
from developer.fr_drafting import (
    CodeEvidence,
    DraftFR,
    compose_description,
    compose_draft,
    infer_classification,
    infer_priority,
    scan_for_evidence,
)


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------


def test_infer_classification_defaults_to_app():
    assert infer_classification("developer", "add a thing to a thing") == "app"


def test_infer_classification_picks_infra_from_target():
    assert infer_classification("scheduler", "add a knob") == "infra"


def test_infer_classification_picks_library_from_request():
    assert (
        infer_classification("developer", "extract a primitive into the library")
        == "library"
    )


def test_infer_priority_default_is_medium():
    assert infer_priority("plain request with no markers") == "medium"


def test_infer_priority_high_on_blocking_keyword():
    assert infer_priority("this is blocking the whole pipeline") == "high"


def test_infer_priority_low_on_polish_keyword():
    assert infer_priority("docs only cleanup of the readme") == "low"


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------


def test_tokenize_request_drops_stopwords_and_short_tokens():
    tokens = fr_drafting._tokenize_request(
        "we want to add a severity_floor arg to review_text"
    )
    assert "severity_floor" in tokens
    assert "review_text" in tokens
    # short tokens / stopwords filtered
    assert "we" not in tokens
    assert "to" not in tokens
    assert "a" not in tokens


def test_tokenize_request_dedupes_and_preserves_order():
    tokens = fr_drafting._tokenize_request(
        "review_text reviewer review_text changes for review_text behavior"
    )
    # "for" is short / stopword; review_text is deduped on second + third hits.
    assert tokens == ["review_text", "reviewer", "changes", "behavior"]


# ---------------------------------------------------------------------------
# Code evidence scan
# ---------------------------------------------------------------------------


def test_scan_for_evidence_finds_match(tmp_path: Path):
    (tmp_path / "agent.py").write_text(
        "def handle_review_text(self, args):\n"
        "    pass\n",
        encoding="utf-8",
    )
    evidence = scan_for_evidence(tmp_path, ["review_text"])
    assert len(evidence) == 1
    assert evidence[0].path == "agent.py"
    assert "handle_review_text" in evidence[0].snippet


def test_scan_for_evidence_respects_max_total(tmp_path: Path):
    for i in range(8):
        (tmp_path / f"f{i}.py").write_text(
            f"# token_{i} appears here\n", encoding="utf-8"
        )
    tokens = [f"token_{i}" for i in range(8)]
    evidence = scan_for_evidence(tmp_path, tokens, max_total=3)
    assert len(evidence) == 3


def test_scan_for_evidence_returns_empty_when_root_missing():
    assert scan_for_evidence(Path("/nope/does/not/exist"), ["x"]) == []


def test_scan_for_evidence_hints_first(tmp_path: Path):
    (tmp_path / "a.py").write_text("# token here\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("# token here\n", encoding="utf-8")
    # Hint the nested file; that's where the first match should land.
    evidence = scan_for_evidence(
        tmp_path, ["token"], repo_hints=["sub/b.py"], max_total=1
    )
    assert evidence[0].path == "sub/b.py"


# ---------------------------------------------------------------------------
# Description composition
# ---------------------------------------------------------------------------


def test_compose_description_omits_empty_sections():
    out = compose_description(
        request="why",
        motivation="",
        scope_bullets=[],
        acceptance_bullets=["one"],
        out_of_scope_bullets=[],
    )
    assert "**Request.**" in out
    assert "**Motivation.**" not in out
    assert "**Scope.**" not in out
    assert "**Acceptance.**" in out
    assert "**Out of scope.**" not in out


def test_compose_description_renders_bullets():
    out = compose_description(
        request="r",
        motivation="m",
        scope_bullets=["a", "b"],
        acceptance_bullets=["c"],
        out_of_scope_bullets=["d"],
    )
    assert "- a\n- b" in out
    assert "- c" in out
    assert "- d" in out


# ---------------------------------------------------------------------------
# compose_draft end-to-end
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.mark.asyncio
async def test_compose_draft_with_brief_and_scan():
    async def brief_fn(req, tgt):
        return ("From the corpus: this is a known concept.", ["paper-1", "paper-2"])

    def scan_fn(req, tgt, hints):
        return [CodeEvidence(path="developer/agent.py", snippet="    @handler(\"x\")")]

    result = await compose_draft(
        request="Add severity_floor to review_text so we can drop low-severity findings.",
        target="reviewer",
        brief_fn=brief_fn,
        scan_fn=scan_fn,
    )
    assert isinstance(result, DraftFR)
    assert result.draft["target"] == "reviewer"
    assert result.draft["title"].startswith("Add severity_floor")
    assert "From the corpus" in result.draft["description"]
    assert "developer/agent.py" in result.draft["description"]
    assert result.corpus_sources == ["paper-1", "paper-2"]
    assert result.code_evidence[0].path == "developer/agent.py"
    assert result.diagnostics == []
    assert result.draft["backing_papers"] == "paper-1,paper-2"
    assert result.draft["priority"] == "medium"
    assert result.draft["classification"] == "app"


@pytest.mark.asyncio
async def test_compose_draft_records_diagnostic_on_brief_failure():
    async def brief_fn(req, tgt):
        raise RuntimeError("bus down")

    def scan_fn(req, tgt, hints):
        return []

    result = await compose_draft(
        request="anything",
        target="developer",
        brief_fn=brief_fn,
        scan_fn=scan_fn,
    )
    assert any("brief_on failed" in d for d in result.diagnostics)
    # Draft is still composed from the request itself.
    assert result.draft["description"].strip() != ""


@pytest.mark.asyncio
async def test_compose_draft_records_diagnostic_on_scan_failure():
    async def brief_fn(req, tgt):
        return ("ok", [])

    def scan_fn(req, tgt, hints):
        raise OSError("disk full")

    result = await compose_draft(
        request="anything",
        target="developer",
        brief_fn=brief_fn,
        scan_fn=scan_fn,
    )
    assert any("code scan failed" in d for d in result.diagnostics)


@pytest.mark.asyncio
async def test_compose_draft_empty_request_returns_empty_draft():
    result = await compose_draft(request="   ", target="developer")
    assert result.draft["title"] == ""
    assert result.draft["description"] == ""
    assert any("empty" in d for d in result.diagnostics)


@pytest.mark.asyncio
async def test_compose_draft_caller_overrides_priority_and_classification():
    async def brief_fn(req, tgt):
        return ("", [])

    def scan_fn(req, tgt, hints):
        return []

    result = await compose_draft(
        request="ordinary request",
        target="developer",
        priority="high",
        classification="library",
        brief_fn=brief_fn,
        scan_fn=scan_fn,
    )
    assert result.draft["priority"] == "high"
    assert result.draft["classification"] == "library"


@pytest.mark.asyncio
async def test_compose_draft_no_callables_records_diagnostics():
    result = await compose_draft(request="anything", target="developer")
    diags = " ".join(result.diagnostics)
    assert "brief_fn" in diags
    assert "scan_fn" in diags
    # Still produces a usable draft from the request alone.
    assert result.draft["title"] != ""
    assert "**Request.**" in result.draft["description"]

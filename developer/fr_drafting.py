"""Compose a draft FR from a free-form request + corpus brief + code evidence.

This is the core of the ``draft_fr_from_request`` skill (FR
``fr_developer_232574cd``). The handler in :mod:`developer.agent`
plugs in the bus call to ``researcher.brief_on`` and a code-evidence
scan, and feeds the result into :func:`compose_draft` here.

Design notes
------------

* No I/O at this layer — all external calls (researcher bus,
  filesystem) are dependency-injected as callables. That keeps the
  composition logic pure and unit-testable.
* Graceful degrade: if the brief callable raises, the diagnostic is
  recorded and the draft is composed without corpus motivation. Same
  for the scan callable. Caller still gets a usable draft skeleton.
* Output shape mirrors :func:`developer.agent.handle_promote_fr` args
  so the caller can pass ``draft`` straight through ``promote_fr``
  once they've reviewed and edited.

This is best-effort scaffolding — see the FR description for what
"good" looks like. Expected to evolve via dogfooding.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional


# Public-facing data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodeEvidence:
    """A single ``(path, snippet)`` pair surfaced from a code scan."""

    path: str
    snippet: str


@dataclass
class DraftFR:
    """Composition result. ``draft`` is the kwarg dict for
    ``handle_promote_fr``; the rest is provenance the caller can
    surface to the user.
    """

    draft: dict[str, Any]
    corpus_sources: list[str] = field(default_factory=list)
    code_evidence: list[CodeEvidence] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    draft_id: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "draft": dict(self.draft),
            "corpus_sources": list(self.corpus_sources),
            "code_evidence": [
                {"path": e.path, "snippet": e.snippet} for e in self.code_evidence
            ],
            "diagnostics": list(self.diagnostics),
            "draft_id": self.draft_id,
        }


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------


_INFRA_HINTS = ("infra", "scheduler", "bus", "transport", "deploy", "ci")
_LIBRARY_HINTS = ("lib", "library", "primitive", "sdk")


def infer_classification(target: str, request: str) -> str:
    """Return one of ``app`` / ``library`` / ``infra``.

    Heuristic: target slug + request keywords. Defaults to ``app`` —
    it's the most common case for agent-targeted FRs.
    """
    haystack = f"{target} {request}".lower()
    if any(h in haystack for h in _INFRA_HINTS):
        return "infra"
    if any(h in haystack for h in _LIBRARY_HINTS):
        return "library"
    return "app"


_HIGH_PRIORITY_HINTS = ("blocking", "broken", "regression", "data loss", "security")
_LOW_PRIORITY_HINTS = ("nice to have", "polish", "cleanup", "docs only")


def infer_priority(request: str) -> str:
    """Return one of ``high`` / ``medium`` / ``low``. Defaults ``medium``."""
    body = request.lower()
    if any(h in body for h in _HIGH_PRIORITY_HINTS):
        return "high"
    if any(h in body for h in _LOW_PRIORITY_HINTS):
        return "low"
    return "medium"


# ---------------------------------------------------------------------------
# Code evidence
# ---------------------------------------------------------------------------


_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "to", "from", "for",
    "of", "in", "on", "at", "with", "by", "is", "are", "was", "were",
    "be", "been", "being", "this", "that", "these", "those", "it",
    "we", "i", "you", "they", "should", "could", "would", "may",
    "can", "do", "does", "did", "have", "has", "had", "as", "into",
    "out", "via", "per", "not", "so", "want", "wants", "wanted",
    "need", "needs", "needed", "make", "makes",
})


def _tokenize_request(text: str, *, min_len: int = 4) -> list[str]:
    """Pull keyword tokens from a free-form request, dropping stopwords.

    Tokens are lowercased word-like substrings (``[A-Za-z_][A-Za-z0-9_]*``)
    of length >= ``min_len`` and not in ``_STOPWORDS``. Order is
    preserved (first-occurrence) and duplicates collapsed so callers
    can stop at the first N matches.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for raw in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text):
        tok = raw.lower()
        if len(tok) < min_len or tok in _STOPWORDS or tok in seen_set:
            continue
        seen.append(tok)
        seen_set.add(tok)
    return seen


_DEFAULT_SCAN_GLOBS = ("**/*.py", "**/*.md")
_SNIPPET_RADIUS = 2  # lines of context above/below the match
_MAX_SNIPPETS_PER_TOKEN = 1
_MAX_TOTAL_SNIPPETS = 6


def scan_for_evidence(
    repo_root: Path,
    tokens: Iterable[str],
    *,
    repo_hints: Iterable[str] = (),
    globs: Iterable[str] = _DEFAULT_SCAN_GLOBS,
    max_total: int = _MAX_TOTAL_SNIPPETS,
) -> list[CodeEvidence]:
    """Greedy keyword grep over ``repo_root``.

    For each token, return the first matching ``(file, line)`` as a
    short snippet. Stops after ``max_total`` snippets. ``repo_hints``
    biases the search to specific subpaths (e.g. ``["developer/agent.py"]``)
    by checking those first. Best-effort: any I/O error skips the
    file silently.

    Symlink-safe: every candidate path is resolved and then required to
    stay under ``repo_root`` (post-resolution). A symlink that points
    outside the repo is silently dropped — we don't want to leak an
    absolute path from the host filesystem into ``CodeEvidence.path``,
    or read content the caller didn't intend.
    """
    if not repo_root.exists() or not repo_root.is_dir():
        return []
    tokens_list = list(tokens)
    if not tokens_list:
        return []

    try:
        root_resolved = repo_root.resolve()
    except OSError:
        return []

    def _within_root(p: Path) -> Optional[Path]:
        try:
            resolved = p.resolve()
        except OSError:
            return None
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            return None
        return resolved

    evidence: list[CodeEvidence] = []
    matched_per_token: dict[str, int] = {t: 0 for t in tokens_list}
    seen_paths: set[Path] = set()

    def _consider(path: Path) -> bool:
        """Try to collect snippets from ``path``. Returns True if max
        was reached (signal to stop the outer walk)."""
        if len(evidence) >= max_total:
            return True
        if path in seen_paths:
            return False
        seen_paths.add(path)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if len(evidence) >= max_total:
                return True
            lower = line.lower()
            for token in tokens_list:
                if matched_per_token[token] >= _MAX_SNIPPETS_PER_TOKEN:
                    continue
                if token in lower:
                    start = max(0, i - _SNIPPET_RADIUS)
                    end = min(len(lines), i + _SNIPPET_RADIUS + 1)
                    snippet = "\n".join(
                        f"{n + 1:>4}: {lines[n]}" for n in range(start, end)
                    )
                    rel = path.relative_to(root_resolved)
                    evidence.append(CodeEvidence(path=str(rel), snippet=snippet))
                    matched_per_token[token] += 1
                    break  # one match per line is enough
        return len(evidence) >= max_total

    # Hinted paths first.
    for hint in repo_hints:
        resolved = _within_root(repo_root / hint)
        if resolved is None or not resolved.is_file():
            continue
        if _consider(resolved):
            return evidence

    # Then a lazy glob walk — stop as soon as we've hit ``max_total``
    # rather than materializing every match into a sorted list up front.
    for pattern in globs:
        for p in repo_root.glob(pattern):
            resolved = _within_root(p)
            if resolved is None or not resolved.is_file():
                continue
            if _consider(resolved):
                return evidence
    return evidence


# ---------------------------------------------------------------------------
# Description composition
# ---------------------------------------------------------------------------


def compose_description(
    *,
    request: str,
    motivation: str,
    scope_bullets: list[str],
    acceptance_bullets: list[str],
    out_of_scope_bullets: list[str],
) -> str:
    """House-style FR description as plain markdown.

    Sections: Request (verbatim caller paragraph), Motivation,
    Scope, Acceptance, Out of scope. Empty sections are omitted so
    the output stays terse.
    """
    parts: list[str] = []
    if request.strip():
        parts.append(f"**Request.**\n\n{request.strip()}")
    if motivation.strip():
        parts.append(f"**Motivation.**\n\n{motivation.strip()}")
    if scope_bullets:
        parts.append("**Scope.**\n\n" + "\n".join(f"- {b}" for b in scope_bullets))
    if acceptance_bullets:
        parts.append(
            "**Acceptance.**\n\n" + "\n".join(f"- {b}" for b in acceptance_bullets)
        )
    if out_of_scope_bullets:
        parts.append(
            "**Out of scope.**\n\n"
            + "\n".join(f"- {b}" for b in out_of_scope_bullets)
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Top-level composer
# ---------------------------------------------------------------------------


# A brief callable returns ``(brief_text, sources)`` or raises.
BriefFn = Callable[[str, str], Awaitable[tuple[str, list[str]]]]
# A scan callable returns a list of CodeEvidence for the given request.
ScanFn = Callable[[str, str, list[str]], list[CodeEvidence]]


def _draft_id_for(request: str, target: str) -> str:
    """Stable-ish id for the draft: short hash + epoch seconds."""
    body = f"{target}:{request}".encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()[:8]
    return f"draft_{digest}_{int(time.time())}"


async def compose_draft(
    *,
    request: str,
    target: str = "",
    repo_hints: Optional[list[str]] = None,
    priority: str = "",
    classification: str = "",
    brief_fn: Optional[BriefFn] = None,
    scan_fn: Optional[ScanFn] = None,
) -> DraftFR:
    """Compose a :class:`DraftFR` from a free-form request.

    ``brief_fn`` and ``scan_fn`` are dependency-injected I/O. If
    omitted, the corresponding section is left empty and a diagnostic
    is recorded.
    """
    request = request or ""
    target = (target or "").strip()
    repo_hints = list(repo_hints or [])
    diagnostics: list[str] = []

    if not request.strip():
        return DraftFR(
            draft={
                "title": "",
                "description": "",
                "target": target,
                "priority": priority or "medium",
                "classification": classification or "app",
            },
            diagnostics=["request is empty; nothing to draft"],
            draft_id=_draft_id_for(request, target),
        )

    # 1. Corpus brief — best-effort.
    motivation = ""
    corpus_sources: list[str] = []
    if brief_fn is not None:
        try:
            motivation, corpus_sources = await brief_fn(request, target)
        except Exception as exc:  # noqa: BLE001 — best-effort
            diagnostics.append(f"brief_on failed: {exc}")
    else:
        diagnostics.append("no brief_fn supplied; motivation will be caller's request")

    # 2. Code evidence — best-effort.
    code_evidence: list[CodeEvidence] = []
    if scan_fn is not None:
        try:
            code_evidence = list(scan_fn(request, target, repo_hints))
        except Exception as exc:  # noqa: BLE001 — best-effort
            diagnostics.append(f"code scan failed: {exc}")
    else:
        diagnostics.append("no scan_fn supplied; acceptance will lack code evidence")

    # 3. Heuristics.
    classification_final = classification.strip() or infer_classification(target, request)
    priority_final = priority.strip() or infer_priority(request)

    # 4. Compose description. Don't fall back to the request itself for
    # Motivation — Request is rendered above as its own section, and
    # duplicating it adds noise without information. An empty
    # Motivation just omits the section.
    motivation_block = motivation.strip()
    scope_bullets = [
        f"Touches {ev.path}" for ev in code_evidence
    ] or ["Scope to be defined — no code evidence surfaced for this request."]
    acceptance_bullets = (
        [
            f"Behavior change visible in `{ev.path}` is exercised by a test."
            for ev in code_evidence
        ]
        or ["At least one test exercises the new/changed behavior."]
    )
    out_of_scope_bullets = ["Auto-promotion. Caller reviews this draft before promote_fr fires."]

    description = compose_description(
        request=request,
        motivation=motivation_block,
        scope_bullets=scope_bullets,
        acceptance_bullets=acceptance_bullets,
        out_of_scope_bullets=out_of_scope_bullets,
    )

    title = _title_from_request(request)

    draft = {
        "title": title,
        "description": description,
        "target": target,
        "priority": priority_final,
        "classification": classification_final,
        "backing_papers": ",".join(corpus_sources),
    }

    return DraftFR(
        draft=draft,
        corpus_sources=corpus_sources,
        code_evidence=code_evidence,
        diagnostics=diagnostics,
        draft_id=_draft_id_for(request, target),
    )


_TITLE_MAX_CHARS = 100


def _title_from_request(request: str) -> str:
    """First sentence of the request, capped at 100 chars."""
    body = request.strip()
    if not body:
        return ""
    first = re.split(r"[.\n]", body, maxsplit=1)[0].strip()
    if len(first) <= _TITLE_MAX_CHARS:
        return first
    return first[: _TITLE_MAX_CHARS - 1].rstrip() + "…"

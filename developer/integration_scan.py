"""Integration-point scanner — symmetric partner to fr_candidates_from_concepts.

Where ``fr_candidates_from_concepts`` turns researcher concept bundles into
*new-FR* proposals, this module turns a *merged feature* (FR, PR, or skill)
into a ranked list of ecosystem *adoption* proposals: existing FRs that
should migrate to the new primitive, agents with near-duplicate skills,
events that have no subscribers, TODO-style mentions that point at
refactor opportunities.

MVP exercises four of the five documented scan sources; source-code grep
across dev repos is deferred until a `dev_repos` registry lands
(``fr_developer_82fe7309``).

The scanner is factored around small pure functions so the agent handler
can plug in:

- a live :class:`developer.fr_store.FRStore` for FR scanning,
- a bus-backed async callable for remote skill discovery,
- an async callable returning subscriber counts for a topic,
- an optional LLM rationale callable capped by top_n.

Tests substitute the callables with in-process fakes.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional

# Signal categories. Kept here (not in the agent module) so downstream
# consumers that import the scanner directly can reuse the enum without
# pulling in the agent's heavier dependencies.
SIGNAL_DIRECT_REPLACE = "direct_replace"
SIGNAL_MIGRATE = "migrate"
SIGNAL_WIRE_SUBSCRIBER = "wire_subscriber"
SIGNAL_REFACTOR_TO_PRIMITIVE = "refactor_to_primitive"

ALL_SIGNALS = (
    SIGNAL_DIRECT_REPLACE,
    SIGNAL_MIGRATE,
    SIGNAL_WIRE_SUBSCRIBER,
    SIGNAL_REFACTOR_TO_PRIMITIVE,
)

# Regex keywords used by scan source #5 — FR-body mentions that hint at
# adoption opportunities. Case-insensitive; bounded to whole-word matches
# so "adopt" doesn't match "adoptable" / "todo" doesn't match "todolist"
# accidentally. Multi-word phrases are matched as whole phrases with
# word boundaries on both ends.
_ADOPT_KEYWORDS = (
    "todo",
    "follow-up",
    "follow up",
    "adopt",
    "future work",
    "should migrate",
)

# Pre-compiled word-boundary regexes per keyword. Each keyword is wrapped
# in ``\b`` anchors so substring collisions (e.g. "adopt" → "adoptable")
# don't register as matches. For multi-word / hyphenated keywords the
# anchors still bracket the full phrase correctly because ``\b`` is a
# zero-width boundary between a word and non-word character.
_ADOPT_KEYWORD_RES = tuple(
    (kw, re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE))
    for kw in _ADOPT_KEYWORDS
)

# Regex for parsing "owner/repo#123" or a full GH URL. Deliberately narrow —
# MVP only handles merged PRs so caller-supplied IDs must be one of these two
# exact shapes (matches the behaviour the handler documents).
_PR_SHORTHAND_RE = re.compile(r"^([\w.-]+)/([\w.-]+)#(\d+)$")
_PR_URL_RE = re.compile(
    r"^https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)(?:[/#?].*)?$"
)

# Small English stoplist — reused from _match_tokens in agent.py but kept
# local so this module doesn't import from the agent module (which would
# be a layering violation given the agent imports this module).
_STOPWORDS = frozenset({
    "the", "and", "for", "from", "into", "with", "this", "that",
    "apply", "developer", "workflow", "via", "when", "then",
    "have", "has", "its", "their", "these", "those", "new",
    "use", "using", "used", "add", "adds", "added", "are", "was",
    "were", "our", "your", "there", "here", "not", "but", "all",
    "any", "can", "may", "will", "would", "should", "could",
})

# LLM budget cap for rationale generation — matches the MVP spec.
MAX_LLM_RATIONALES = 20


@dataclass
class FeatureSurface:
    """Normalized representation of a feature source (FR, PR, or skill).

    Populated by :func:`extract_feature_surface` and consumed by every
    scan source. Keeping this as a dataclass (not a dict) means typos in
    field access surface as attribute errors instead of silent ``None``s.
    """

    kind: str
    id: str
    title: str = ""
    description: str = ""
    new_skills: list[str] = field(default_factory=list)
    new_events: list[str] = field(default_factory=list)
    new_types: list[str] = field(default_factory=list)
    topic_concepts: list[str] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)

    def to_public_dict(self) -> dict[str, Any]:
        """Serializable projection for the scan artifact + response echo."""
        return {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "new_skills": list(self.new_skills),
            "new_events": list(self.new_events),
            "new_types": list(self.new_types),
            "topic_concepts": list(self.topic_concepts),
            "tokens": sorted(self.tokens),
        }


@dataclass
class IntegrationCandidate:
    """A single ranked adoption site.

    ``target_id`` naming by kind:
      - ``fr``: ``fr_<target>_<hex>``
      - ``skill_callsite``: ``<agent_id>.<skill_name>``
      - ``agent``: ``<agent_id>``
      - ``event_broker_gap``: the topic string itself
    """

    kind: str
    target_id: str
    signal: str
    score: float
    rationale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_brief(self) -> dict[str, Any]:
        """Display-oriented projection; rounds score to 3 decimals."""
        return {
            "kind": self.kind,
            "target_id": self.target_id,
            "rationale": self.rationale,
            "score": round(float(self.score), 3),
            "signal": self.signal,
        }

    # Metadata fields that are small, scalar, and useful for routing /
    # triage decisions. Heavier fields (e.g. shared_tokens, keywords lists)
    # stay behind the ``full`` detail to keep ``compact`` genuinely leaner.
    _COMPACT_META_FIELDS = (
        "status", "target", "title_sim", "content_sim",
        "agent_id", "skill_name", "similarity",
        "subscriber_count", "expected",
    )

    def to_compact(self) -> dict[str, Any]:
        """Brief projection plus a lean subset of metadata.

        Sits between ``brief`` (name-only) and ``full`` (everything). Picks
        the small scalar metadata fields that help a caller decide which
        candidates warrant a ``full`` follow-up, without dragging along
        shared-token / keyword lists that make the response heavy.
        Rounds score to 3 decimals to match ``brief`` display behaviour.
        """
        out = self.to_brief()
        compact_meta = {
            k: v for k, v in self.metadata.items()
            if k in self._COMPACT_META_FIELDS
        }
        if compact_meta:
            out["metadata"] = compact_meta
        return out

    def to_full(self) -> dict[str, Any]:
        """Persistence-oriented projection; preserves unrounded score.

        ``to_full`` feeds the KnowledgeEntry that ``distill_integration_points``
        rehydrates and re-ranks. Re-using ``to_brief`` here would round
        the score and corrupt the ordering of close-score candidates
        after persist + rehydrate. Display rounding stays in ``to_brief``.
        """
        return {
            "kind": self.kind,
            "target_id": self.target_id,
            "rationale": self.rationale,
            "score": float(self.score),
            "signal": self.signal,
            "metadata": dict(self.metadata),
        }


def tokens(text: str) -> set[str]:
    """Lowercase alphanumeric tokens minus stopwords and very short tokens."""
    return {
        t
        for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(t) > 2 and t not in _STOPWORDS
    }


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Set-similarity in [0, 1]. Returns 0 when both sides are empty.

    Used as a cheap fallback for cosine similarity when the researcher's
    relevance scorer isn't reachable through the bus.
    """
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


def parse_pr_id(raw: str) -> Optional[tuple[str, str, int]]:
    """Accept either ``owner/repo#N`` or a canonical PR URL.

    Returns ``(owner, repo, pr_number)`` or ``None`` if the input doesn't
    match either shape. The narrow grammar is deliberate — we don't want
    to guess at malformed inputs silently.
    """
    if not raw:
        return None
    raw = raw.strip()
    m = _PR_SHORTHAND_RE.match(raw)
    if m:
        return (m.group(1), m.group(2), int(m.group(3)))
    m = _PR_URL_RE.match(raw)
    if m:
        return (m.group(1), m.group(2), int(m.group(3)))
    return None


# ---------------------------------------------------------------------------
# Feature-surface extraction
# ---------------------------------------------------------------------------


# Skill names mentioned in FR/PR/skill source text. The heuristic is
# snake_case words that look like bus operations — three or more chars,
# at least one underscore, all lowercase. Pure-prose words like
# ``integration`` aren't matched because they lack the underscore.
_SKILL_NAME_RE = re.compile(r"\b([a-z][a-z0-9_]*_[a-z0-9_]+)\b")

# Event topics: dotted names with at least one dot, all lowercase or
# lowercase with digits. Matches "pr.fleet_digest", "library.gap_identified".
_EVENT_TOPIC_RE = re.compile(r"\b([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)\b")


def _extract_skill_names(text: str) -> list[str]:
    seen: list[str] = []
    added: set[str] = set()
    for m in _SKILL_NAME_RE.finditer(text or ""):
        name = m.group(1)
        # Deduplicate repeated matches while preserving first-seen order.
        if name in added:
            continue
        added.add(name)
        seen.append(name)
    return seen


def _extract_event_topics(text: str) -> list[str]:
    seen: list[str] = []
    added: set[str] = set()
    for m in _EVENT_TOPIC_RE.finditer(text or ""):
        name = m.group(1)
        # Deduplicate repeated matches while preserving first-seen order.
        if name in added:
            continue
        added.add(name)
        seen.append(name)
    return seen


def _extract_type_names(text: str) -> list[str]:
    """CamelCase type-like identifiers. Heuristic, MVP-only."""
    seen: list[str] = []
    added: set[str] = set()
    for m in re.finditer(r"\b([A-Z][A-Za-z0-9]{2,})\b", text or ""):
        name = m.group(1)
        if name in added:
            continue
        added.add(name)
        seen.append(name)
    return seen


def extract_feature_surface_from_fr(fr: Any) -> FeatureSurface:
    """Build a surface from a developer FR record.

    ``fr`` is a duck-typed object with ``id``, ``title``, ``description``,
    and ``concept`` attributes (matching :class:`developer.fr_store.FR`).
    Using duck typing instead of an isinstance check means this module
    stays importable without pulling in the store module at import time.
    """
    title = getattr(fr, "title", "") or ""
    description = getattr(fr, "description", "") or ""
    concept = getattr(fr, "concept", "") or ""
    combined = f"{title}\n{description}\n{concept}"
    return FeatureSurface(
        kind="fr",
        id=getattr(fr, "id", "") or "",
        title=title,
        description=description,
        new_skills=_extract_skill_names(combined),
        new_events=_extract_event_topics(combined),
        new_types=_extract_type_names(combined),
        topic_concepts=[c.strip() for c in concept.split(",") if c.strip()],
        tokens=tokens(combined),
    )


def extract_feature_surface_from_pr(
    *, pr_id: str, title: str, body: str, owner: str = "", repo: str = "",
) -> FeatureSurface:
    combined = f"{title}\n{body}"
    return FeatureSurface(
        kind="pr",
        id=pr_id,
        title=title,
        description=body,
        new_skills=_extract_skill_names(combined),
        new_events=_extract_event_topics(combined),
        new_types=_extract_type_names(combined),
        topic_concepts=[],
        tokens=tokens(combined),
    )


def extract_feature_surface_from_skill(
    *, skill_id: str, description: str = "", agent_id: str = "",
    args_schema: Optional[dict] = None,
) -> FeatureSurface:
    # ``agent_id`` is the agent that hosts the skill (e.g. ``developer-primary``).
    # Earlier revisions called this ``agent_type``; the value was always an
    # agent id, not a type. Renamed for accuracy (PR #46 Copilot R3).
    # Fold ``args_schema`` into the combined haystack so parameter-name
    # tokens (e.g. ``bug_id``, ``source_id``) contribute to skill-name /
    # event-topic / token extraction. The JSON serialization is stable via
    # ``sort_keys`` so two equivalent schemas produce identical token sets.
    # PR #46 Copilot R8 finding #4.
    schema_blob = json.dumps(args_schema, sort_keys=True) if args_schema else ""
    combined = f"{skill_id}\n{description}\n{agent_id}\n{schema_blob}"
    # The skill name itself is the only *new skill* this surface contributes.
    # Extract bare skill name (strip ``<agent>.`` prefix if present).
    bare_name = skill_id.split(".")[-1] if "." in skill_id else skill_id
    return FeatureSurface(
        kind="skill",
        id=skill_id,
        title=bare_name,
        description=description,
        new_skills=[bare_name] if bare_name else [],
        new_events=_extract_event_topics(combined),
        new_types=_extract_type_names(combined),
        topic_concepts=[],
        tokens=tokens(combined),
    )


# ---------------------------------------------------------------------------
# Scan sources
# ---------------------------------------------------------------------------


def scan_fr_store(
    surface: FeatureSurface, existing: Iterable[Any],
) -> list[IntegrationCandidate]:
    """Scan sources #1 and #5 — FR semantic + regex-keyword mentions.

    Combines two sub-scans that both iterate the FR corpus:

    - Token-overlap → ``migrate`` (significant overlap) or
      ``direct_replace`` (near-exact title match).
    - Adoption-keyword co-occurrence (``TODO``/``follow-up``/``adopt``) →
      ``refactor_to_primitive``.

    One pass, two classifications — a single FR can legitimately surface
    under both signals, but we de-duplicate by ``(target_id, signal)``.
    """
    surface_tokens = surface.tokens
    surface_title = (surface.title or "").lower().strip()
    surface_id = surface.id
    # Hoist ``tokens(surface.title)`` once per scan — previously recomputed
    # per-FR inside the loop, which is measurable on large FR corpora
    # (every FR body already spawns one ``tokens()`` call via ``fr_tokens``).
    # PR #46 Copilot R8 finding #5.
    surface_title_tokens = tokens(surface.title)

    candidates: list[IntegrationCandidate] = []
    seen: set[tuple[str, str]] = set()

    for fr in existing:
        fr_id = getattr(fr, "id", "") or ""
        if not fr_id or fr_id == surface_id:
            # Skip the feature itself when it lives in the FR store.
            continue
        fr_title = getattr(fr, "title", "") or ""
        fr_body = getattr(fr, "description", "") or ""
        fr_concept = getattr(fr, "concept", "") or ""
        haystack = f"{fr_title}\n{fr_body}\n{fr_concept}"
        fr_tokens = tokens(haystack)

        # --- direct_replace: exact-ish title match ------------------------
        # Evaluating direct_replace does NOT short-circuit the rest of the
        # signals: the docstring promises that a single FR may surface under
        # multiple signals, deduplicated at the (target_id, signal) level.
        # An FR whose title matches AND whose body mentions "TODO" legitimately
        # belongs under both direct_replace and refactor_to_primitive.
        title_sim = jaccard(tokens(fr_title), surface_title_tokens)
        if surface_title and fr_title.lower().strip() == surface_title:
            key = (fr_id, SIGNAL_DIRECT_REPLACE)
            if key not in seen:
                seen.add(key)
                candidates.append(IntegrationCandidate(
                    kind="fr", target_id=fr_id,
                    signal=SIGNAL_DIRECT_REPLACE,
                    score=1.0,
                    rationale=f"FR title matches {surface.title!r} verbatim",
                    metadata={
                        "status": getattr(fr, "status", ""),
                        "target": getattr(fr, "target", ""),
                        "title_sim": 1.0,
                    },
                ))
        elif title_sim >= 0.8:
            key = (fr_id, SIGNAL_DIRECT_REPLACE)
            if key not in seen:
                seen.add(key)
                candidates.append(IntegrationCandidate(
                    kind="fr", target_id=fr_id,
                    signal=SIGNAL_DIRECT_REPLACE,
                    score=0.85,
                    rationale=f"FR title near-matches {surface.title!r}",
                    metadata={
                        "status": getattr(fr, "status", ""),
                        "target": getattr(fr, "target", ""),
                        "title_sim": round(title_sim, 3),
                    },
                ))

        # --- migrate: description cosine-fallback (jaccard) ---------------
        # ``content_sim`` (not ``body_sim``) because ``haystack`` covers
        # title + body + concept, not body-only. The broader signal
        # preserves recall for a first-pass ranking; renamed per PR #46
        # Copilot R9 finding #5.
        content_sim = jaccard(surface_tokens, fr_tokens)
        if content_sim >= 0.25:
            # 0.25 is an intentionally low threshold — recall beats
            # precision for a first pass, and the reviewer sees the score.
            # Score is mapped linearly from [0.25, 1.0] → [0.5, 1.0] so
            # migrate candidates stay below direct_replace's baseline.
            score = 0.5 + (content_sim - 0.25) * (0.5 / 0.75)
            key = (fr_id, SIGNAL_MIGRATE)
            if key not in seen:
                seen.add(key)
                # Full intersection drives the reported count; the preview
                # slice is for UI readability only. Conflating the two
                # under-reports overlap for highly similar FRs (every FR
                # with 9+ shared tokens used to read as exactly 8).
                full_overlap = sorted(surface_tokens & fr_tokens)
                shared_preview = full_overlap[:8]
                candidates.append(IntegrationCandidate(
                    kind="fr", target_id=fr_id,
                    signal=SIGNAL_MIGRATE,
                    score=min(0.99, score),
                    rationale=(
                        f"FR overlaps on {len(full_overlap)} tokens "
                        f"(e.g. {', '.join(shared_preview[:4])})"
                    ),
                    metadata={
                        "status": getattr(fr, "status", ""),
                        "target": getattr(fr, "target", ""),
                        "content_sim": round(content_sim, 3),
                        "shared_token_count": len(full_overlap),
                        "shared_tokens": shared_preview,
                    },
                ))

        # --- refactor_to_primitive: adoption-keyword co-occurrence --------
        # Word-boundary match via pre-compiled regexes: "adopt" must not
        # hit "adoptable", "todo" must not hit "todolist".
        # Scan body-only (not title/concept) — the signal is "FR body
        # mentions adoption verbs", so title/concept hits are false positives.
        hit_kw: list[str] = []
        for kw, kw_re in _ADOPT_KEYWORD_RES:
            if kw_re.search(fr_body):
                hit_kw.append(kw)
        if hit_kw:
            # Require some thematic link before flagging — an FR that
            # mentions "TODO" in an unrelated context shouldn't surface
            # just because the string appears. Demand at least one shared
            # token with the surface.
            shared = surface_tokens & fr_tokens
            if shared:
                key = (fr_id, SIGNAL_REFACTOR_TO_PRIMITIVE)
                if key not in seen:
                    seen.add(key)
                    candidates.append(IntegrationCandidate(
                        kind="fr", target_id=fr_id,
                        signal=SIGNAL_REFACTOR_TO_PRIMITIVE,
                        score=0.5,
                        rationale=(
                            f"FR body mentions {', '.join(hit_kw[:2])!r} "
                            f"alongside overlap on {len(shared)} token(s)"
                        ),
                        metadata={
                            "status": getattr(fr, "status", ""),
                            "target": getattr(fr, "target", ""),
                            "keywords": hit_kw,
                            "shared_tokens": sorted(shared)[:8],
                        },
                    ))
    return candidates


def scan_agent_skills(
    surface: FeatureSurface, skills: Iterable[dict],
) -> list[IntegrationCandidate]:
    """Scan source #2 — near-duplicate skills registered on other agents.

    ``skills`` is the shape returned by the bus ``/v1/skills`` endpoint —
    each row has ``agent_id``, ``name``, ``description`` at minimum.
    """
    # Surface's own agent (if we can identify it) is excluded so we don't
    # recommend the agent re-implement its own skill. For FR/PR sources
    # this is a no-op; for skill sources we extract the prefix.
    self_agent = ""
    self_skill = ""
    if surface.kind == "skill" and "." in surface.id:
        self_agent, self_skill = surface.id.split(".", 1)

    candidates: list[IntegrationCandidate] = []
    seen: set[tuple[str, str]] = set()
    surface_skill_names = {s.lower() for s in surface.new_skills}
    surface_tokens = surface.tokens

    for row in skills:
        agent_id = str(row.get("agent_id") or "")
        skill_name = str(row.get("name") or "")
        desc = str(row.get("description") or "")
        if not agent_id or not skill_name:
            continue
        if agent_id == self_agent and skill_name == self_skill:
            continue
        target_id = f"{agent_id}.{skill_name}"

        # Exact name match → direct_replace with score 1.0.
        if skill_name.lower() in surface_skill_names:
            key = (target_id, SIGNAL_DIRECT_REPLACE)
            if key not in seen:
                seen.add(key)
                candidates.append(IntegrationCandidate(
                    kind="skill_callsite", target_id=target_id,
                    signal=SIGNAL_DIRECT_REPLACE,
                    score=1.0,
                    rationale=(
                        f"Agent {agent_id!r} already exposes skill "
                        f"{skill_name!r} — collapse or delegate"
                    ),
                    metadata={
                        "agent_id": agent_id,
                        "skill_name": skill_name,
                        "description": desc,
                    },
                ))
                continue

        # Description overlap → migrate with jaccard-scaled score. Gate
        # on a minimum so very short skills don't flood the results.
        skill_tokens = tokens(f"{skill_name} {desc}")
        sim = jaccard(surface_tokens, skill_tokens)
        if sim >= 0.2:
            score = 0.4 + (sim - 0.2) * (0.4 / 0.8)
            key = (target_id, SIGNAL_MIGRATE)
            if key not in seen:
                seen.add(key)
                candidates.append(IntegrationCandidate(
                    kind="skill_callsite", target_id=target_id,
                    signal=SIGNAL_MIGRATE,
                    score=min(0.79, score),
                    rationale=(
                        f"{target_id} description overlaps "
                        f"(jaccard={sim:.2f}) — candidate migration site"
                    ),
                    metadata={
                        "agent_id": agent_id,
                        "skill_name": skill_name,
                        "description": desc,
                        "similarity": round(sim, 3),
                    },
                ))
    return candidates


def scan_event_subscribers(
    surface: FeatureSurface,
    subscriber_counts: dict[str, int],
    expected_consumers: Optional[dict[str, int]] = None,
) -> list[IntegrationCandidate]:
    """Scan source #3 — topics published with no subscribers.

    ``subscriber_counts`` maps topic → int. ``expected_consumers``
    (optional) gives the expected fan-out per topic; when provided, a
    topic with *some* subscribers but fewer than expected is flagged
    with a proportionally lower score.
    """
    expected_consumers = expected_consumers or {}
    candidates: list[IntegrationCandidate] = []
    for topic in surface.new_events:
        # Defensive coercion: ``subscriber_counts`` comes from bus data
        # whose shape may evolve over time. A single malformed value
        # (``None``, a non-numeric string, a dict) shouldn't crash the
        # whole scan — fall back to 0 (treat unknown as "no subscribers",
        # which is the signal we'd flag anyway for a missing topic).
        # PR #46 Copilot R9 finding #4.
        raw_count = subscriber_counts.get(topic, 0)
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = 0
        if count == 0:
            candidates.append(IntegrationCandidate(
                kind="event_broker_gap", target_id=topic,
                signal=SIGNAL_WIRE_SUBSCRIBER,
                score=1.0,
                rationale=(
                    f"Topic {topic!r} has no subscribers — adoption site"
                ),
                metadata={"subscriber_count": 0},
            ))
            continue
        expected = expected_consumers.get(topic, 0)
        if expected and count < expected:
            # Fractional gap. A topic expecting 4 consumers with only 1
            # registered is worth highlighting; a topic expecting 2 with
            # 1 still scores, just lower.
            # Score is stored unrounded so persist+rehydrate preserves the
            # full-precision ranking (``to_full`` also stores unrounded).
            # Display rounding happens in ``to_brief`` / ``to_compact``.
            # PR #46 Copilot R3 finding #6.
            score = 1.0 - (count / expected)
            candidates.append(IntegrationCandidate(
                kind="event_broker_gap", target_id=topic,
                signal=SIGNAL_WIRE_SUBSCRIBER,
                score=score,
                rationale=(
                    f"Topic {topic!r} has {count}/{expected} expected "
                    "subscribers — partial adoption"
                ),
                metadata={
                    "subscriber_count": count,
                    "expected": expected,
                },
            ))
    return candidates


# ---------------------------------------------------------------------------
# Ranking + assembly
# ---------------------------------------------------------------------------


def rank_candidates(candidates: list[IntegrationCandidate]) -> list[IntegrationCandidate]:
    """Sort by (signal priority, score desc, target_id).

    Signal priority mirrors the MVP spec's relative ordering — direct
    replacements are the most actionable, wire-subscriber gaps next,
    migrations, then refactor hints.
    """
    priority = {
        SIGNAL_DIRECT_REPLACE: 0,
        SIGNAL_WIRE_SUBSCRIBER: 1,
        SIGNAL_MIGRATE: 2,
        SIGNAL_REFACTOR_TO_PRIMITIVE: 3,
    }
    return sorted(
        candidates,
        key=lambda c: (
            priority.get(c.signal, 99),
            -float(c.score),
            c.target_id,
        ),
    )


def compute_scan_id(source: dict[str, Any], *, seed: Optional[int] = None) -> str:
    """Stable 16-hex identifier for a scan of ``source``.

    ``seed`` is a nanosecond epoch timestamp. Defaults to
    ``time.time_ns()`` so two scans of the same source in the same wall-clock
    second still produce distinct ids — matches the PR #29 snapshot_id fix.
    Callers with a fixed timestamp can pass one in for deterministic fixtures.

    The hex prefix is 16 chars (64 bits) rather than the original 8 (32 bits)
    so the birthday-bound collision probability stays negligible at scale:
    at 10M scans the collision probability is ~2.7e-7, vs ~1.2% at 8 hex.
    PR #46 Copilot R3 finding #5.
    """
    if seed is None:
        seed = time.time_ns()
    payload = json.dumps(
        {"source": source, "ts": int(seed)},
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"scan_integration_{digest}"


# ---------------------------------------------------------------------------
# LLM rationale budget helper
# ---------------------------------------------------------------------------


RationaleFn = Callable[[IntegrationCandidate, FeatureSurface], Awaitable[str]]


async def apply_llm_rationales(
    candidates: list[IntegrationCandidate],
    surface: FeatureSurface,
    rationale_fn: Optional[RationaleFn],
    *,
    top_n: int = MAX_LLM_RATIONALES,
) -> int:
    """Invoke ``rationale_fn`` for the first ``top_n`` candidates only.

    Returns the number of calls attempted (including failures) — this is
    the budget-relevant metric since each attempt burns a provider slot
    regardless of outcome. Template rationales (set by the scan sources)
    remain on everything past the cap, and on any candidate whose
    ``rationale_fn`` raised. Exceptions are swallowed per-candidate so
    one failure doesn't poison the whole pass. Callers that need the
    success count can derive it as ``attempts - failures`` by tracking
    exceptions themselves.

    PR #46 Copilot R4 finding #3: pre-fix the counter incremented only
    on success, which under-reported the budget consumed.
    """
    if rationale_fn is None or top_n <= 0:
        return 0
    attempts = 0
    for c in candidates[:top_n]:
        attempts += 1
        try:
            generated = await rationale_fn(c, surface)
        except Exception:
            continue
        if generated:
            c.rationale = generated
    return attempts

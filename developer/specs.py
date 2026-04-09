r"""Spec/milestone reading layer for the developer MCP server.

Built on ``LocalDocReader`` from ``khonliang-researcher-lib``. Adds
developer-specific glue:

  * Parse the project's bold-line spec metadata convention
    (``**FR:** \`fr_xxx\```, ``**Status:** ...``) into a typed
    :class:`SpecSummary`.
  * Glob spec files under ``{project.repo}/{project.specs_dir}/**/spec.md``.
  * Backward-traverse a milestone document to its linked specs and the FRs
    referenced therein. FRs are resolved via :class:`ResearcherClient`,
    which is stubbed in MS-01 (so all resolutions come back ``(unresolved)``).

No persistence. No LLM calls. Spec/milestone documents are workspace
artifacts and are never written to ``KnowledgeStore``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from khonliang_researcher.doc_reader import DocContent, LocalDocReader

from developer.config import ProjectConfig
from developer.researcher_client import FRRecord, ResearcherClient


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Bold-line metadata: ``**Key:** value`` (value may be backticked, linked, etc.)
_BOLD_META_RE = re.compile(r"^\*\*([^*:]+):\*\*\s*(.+?)\s*$", re.MULTILINE)

# Strip surrounding backticks from a metadata value.
_BACKTICKED_RE = re.compile(r"^`(.+)`$")

# Strict FR ID pattern: ``fr_<target>_<8 hex chars>``. Tighter than
# LocalDocReader's default so it does not match python identifiers like
# ``fr_status`` or ``fr_id`` that appear in prose. Examples:
#   fr_developer_28a11ce2
#   fr_researcher_c6b7dca8
#   fr_researcher-lib_d75b118c
FR_ID_PATTERN = r"\bfr_[\w-]+_[a-f0-9]{8}\b"

# Milestone ID pattern — ``MS-01``, ``MS-12``, etc.
MS_ID_PATTERN = r"MS-\d+"

# Markdown links to other doc files: ``[label](path/to/file.md)``.
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+\.md)\)")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class SpecSummary:
    """Spec metadata extracted from the bold-line convention.

    Populated by :meth:`SpecReader.summarize`. All fields are best-effort —
    missing keys come back as empty strings rather than raising, since the
    bold-line convention is informal.
    """

    path: str
    title: str = ""
    fr: str = ""
    priority: str = ""
    class_: str = ""  # ``class_`` because ``class`` is a keyword
    status: str = ""
    extras: dict[str, str] = field(default_factory=dict)


@dataclass
class FRResolution:
    """One FR reference + the result of resolving it via ResearcherClient.

    In MS-01 ``record`` is always ``None`` because the client is stubbed.
    Callers should treat ``None`` as "unresolved" and render accordingly.
    """

    fr_id: str
    record: FRRecord | None

    @property
    def resolved(self) -> bool:
        return self.record is not None


@dataclass
class MilestoneChain:
    """Backward traversal result: milestone → specs → FRs → (evidence)."""

    milestone_path: str
    milestone_summary: SpecSummary
    specs: list[SpecSummary] = field(default_factory=list)
    frs: list[FRResolution] = field(default_factory=list)
    unresolved_links: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class SpecReader:
    """Domain layer wrapping :class:`LocalDocReader` with project awareness."""

    def __init__(
        self,
        reader: LocalDocReader,
        projects: dict[str, ProjectConfig],
        researcher: ResearcherClient,
    ):
        self._reader = reader
        self._projects = projects
        self._researcher = researcher

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read(self, path: str) -> DocContent:
        """Passthrough to LocalDocReader.read() — keeps the seam thin."""
        return self._reader.read(path)

    def summarize(self, path: str) -> SpecSummary:
        """Parse a spec/milestone file's bold-line metadata into a typed summary.

        Bold lines like ``**FR:** `fr_developer_28a11ce2``` are extracted
        from the body. The first ``# Title`` heading becomes ``title``.
        Unknown bold-line keys land in ``extras`` so callers can introspect
        without losing data.
        """
        doc = self._reader.read(path)
        title = _extract_title(doc.text)
        meta = _parse_bold_metadata(doc.text)

        # The FR bold line often has extra prose after the backticked ID
        # (e.g. ``**FR:** `fr_xxx` (partial — closes 20%)``). Extract the
        # canonical FR ID via regex so consumers don't have to.
        fr_raw = meta.pop("fr", "")
        fr_match = re.search(FR_ID_PATTERN, fr_raw)
        fr = fr_match.group(0) if fr_match else fr_raw

        return SpecSummary(
            path=str(path),
            title=title,
            fr=fr,
            priority=meta.pop("priority", ""),
            class_=meta.pop("class", ""),
            status=meta.pop("status", ""),
            extras=meta,
        )

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    def list_specs(self, project: str) -> list[SpecSummary]:
        """Discover all ``spec.md`` files under a project's specs directory.

        Uses ``projects[project].specs_dir`` from config — never a hardcoded
        ``specs/`` path. Returns an empty list if the project is unknown or
        the specs directory does not exist.
        """
        proj = self._projects.get(project)
        if proj is None:
            return []
        root = proj.specs_root
        if not root.exists():
            return []
        paths = self._reader.glob_docs(str(root), pattern="**/spec.md")
        return [self.summarize(p) for p in paths]

    # ------------------------------------------------------------------
    # Traverse
    # ------------------------------------------------------------------

    async def traverse_milestone(self, path: str) -> MilestoneChain:
        """Walk backward from a milestone document to its FRs.

        Steps:
          1. Read the milestone file.
          2. Follow markdown links to ``*.md`` files (filtered to spec files
             that exist on disk).
          3. Read each linked spec.
          4. Collect every ``fr_*`` reference from the milestone body and
             from each linked spec.
          5. For each unique FR, call ``ResearcherClient.get_fr`` and wrap
             the result in :class:`FRResolution`.

        In MS-01 every FR comes back unresolved (the client is stubbed).
        Acceptance #6 verifies the call path is exercised and the
        unresolved markers render correctly.
        """
        milestone_doc = self._reader.read(path)
        milestone_summary = self.summarize(path)

        spec_paths, unresolved_links = _resolve_doc_links(
            base=Path(path).resolve().parent, links=_find_md_links(milestone_doc.text)
        )

        # Use the strict FR pattern, not LocalDocReader's default — the
        # default matches python identifiers like ``fr_status`` from prose.
        spec_summaries: list[SpecSummary] = []
        all_fr_refs: list[str] = list(
            self._reader.find_references(path, pattern=FR_ID_PATTERN)
        )
        for spec_path in spec_paths:
            spec_summary = self.summarize(str(spec_path))
            spec_summaries.append(spec_summary)
            for ref in self._reader.find_references(
                str(spec_path), pattern=FR_ID_PATTERN
            ):
                if ref not in all_fr_refs:
                    all_fr_refs.append(ref)

        frs: list[FRResolution] = []
        for fr_id in all_fr_refs:
            record = await self._researcher.get_fr(fr_id)
            frs.append(FRResolution(fr_id=fr_id, record=record))

        return MilestoneChain(
            milestone_path=str(path),
            milestone_summary=milestone_summary,
            specs=spec_summaries,
            frs=frs,
            unresolved_links=unresolved_links,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_title(text: str) -> str:
    """First top-level (#) heading text, or empty string."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _parse_bold_metadata(text: str) -> dict[str, str]:
    r"""Parse ``**Key:** value`` lines into a lowercase-keyed dict.

    Strips backticks from values, so ``**FR:** \`fr_xxx\``` yields
    ``{"fr": "fr_xxx"}``. Multiple occurrences of the same key keep the
    first one.
    """
    out: dict[str, str] = {}
    for match in _BOLD_META_RE.finditer(text):
        key = match.group(1).strip().lower()
        value = match.group(2).strip()
        bt = _BACKTICKED_RE.match(value)
        if bt:
            value = bt.group(1)
        out.setdefault(key, value)
    return out


def _find_md_links(text: str) -> list[str]:
    """Return all ``.md`` link targets from a markdown document, in order."""
    return [m.group(1) for m in _MD_LINK_RE.finditer(text)]


def _resolve_doc_links(
    base: Path, links: list[str]
) -> tuple[list[Path], list[str]]:
    """Resolve markdown link targets relative to ``base``.

    Returns ``(existing_paths, unresolved_link_strings)``. Duplicates are
    collapsed; only paths that exist on disk land in ``existing_paths``.
    """
    seen: set[Path] = set()
    found: list[Path] = []
    missing: list[str] = []
    for link in links:
        # Strip URL fragments and query strings, just in case.
        clean = link.split("#", 1)[0].split("?", 1)[0]
        if not clean:
            continue
        candidate = (base / clean).resolve()
        if candidate.exists() and candidate.is_file():
            if candidate not in seen:
                seen.add(candidate)
                found.append(candidate)
        else:
            missing.append(link)
    return found, missing

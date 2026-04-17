"""MCP server for khonliang-developer.

Mirrors khonliang-researcher's ``create_research_server`` factory pattern:
build a :class:`KhonliangMCPServer` against the pipeline's stores, register
the developer guide, then attach the developer-specific @mcp.tool()
functions.

MS-01 surface: ``read_spec``, ``traverse_milestone``, ``list_specs``,
``health_check``, ``developer_guide``, plus the inherited ``catalog`` /
``knowledge_search`` / ``triple_query`` from the base class.

Usage::

    python -m developer.server --config /abs/path/config.yaml
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from khonliang.mcp import (
    KhonliangMCPServer,
    compact_summary,
    format_response,
)

from developer.config import Config
from developer.pipeline import Pipeline
from developer.specs import PathNotAllowedError


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_developer_server(pipeline: Pipeline):
    """Create the developer MCP server with all MS-01 tools registered.

    Mirrors :func:`researcher.server.create_research_server` exactly for
    structure: same ``KhonliangMCPServer`` base, same two-step guide
    registration, same ``@mcp.tool()`` async pattern, same response
    convention via ``format_response``.
    """
    base = KhonliangMCPServer(
        knowledge_store=pipeline.knowledge,
        triple_store=pipeline.triples,
    )
    base.add_guide(
        "developer_guide",
        "development workflow + spec/milestone management",
    )
    mcp = base.create_app()

    guide_text = pipeline.developer_guide_text

    # ------------------------------------------------------------------
    # Developer guide
    # ------------------------------------------------------------------

    @mcp.tool()
    async def developer_guide() -> str:
        """development workflow + spec/milestone management"""
        return guide_text

    # ------------------------------------------------------------------
    # Spec/milestone tools
    # ------------------------------------------------------------------

    @mcp.tool()
    async def read_spec(path: str, detail: str = "brief") -> str:
        """Parse a spec or milestone file via LocalDocReader.

        Returns the file's bold-line metadata, section index, and FR
        references. Use ``detail='full'`` to get the raw text body too.
        """
        try:
            doc = pipeline.specs.read(path)
            summary = pipeline.specs.summarize(path)
        except PathNotAllowedError as e:
            return f"error: {e}"
        except FileNotFoundError:
            return f"error: file not found: {path}"
        except OSError as e:
            return f"error: {e}"

        return format_response(
            compact_fn=lambda: compact_summary(
                {
                    "path": doc.path,
                    "title": _compact_field(summary.title, 60),
                    "fr": summary.fr or "?",
                    "status": summary.status or "?",
                    "sections": len(doc.sections),
                    "refs": len(doc.references),
                }
            ),
            brief_fn=lambda: _format_spec_brief(doc, summary),
            full_fn=lambda: _format_spec_full(doc, summary),
            detail=detail,
        )

    @mcp.tool()
    async def traverse_milestone(path: str, detail: str = "brief") -> str:
        """Backward-walk a milestone document to its specs and FRs.

        FRs are resolved from developer's authoritative FR store. Unresolved
        markers mean the milestone references an FR id that has not been
        migrated or promoted into developer yet.
        """
        try:
            chain = await pipeline.specs.traverse_milestone(path)
        except PathNotAllowedError as e:
            return f"error: {e}"
        except FileNotFoundError:
            return f"error: file not found: {path}"
        except OSError as e:
            return f"error: {e}"

        return format_response(
            compact_fn=lambda: compact_summary(
                {
                    "milestone": Path(chain.milestone_path).name,
                    "title": _compact_field(chain.milestone_summary.title, 60),
                    "specs": len(chain.specs),
                    "frs": len(chain.frs),
                    "unresolved_links": len(chain.unresolved_links),
                }
            ),
            brief_fn=lambda: _format_chain_brief(chain),
            full_fn=lambda: _format_chain_full(chain),
            detail=detail,
        )

    @mcp.tool()
    async def list_specs(project: str, detail: str = "brief") -> str:
        """Discover ``spec.md`` files under a configured project.

        Uses ``projects[project].specs_dir`` from config — never a hardcoded
        ``specs/`` path. Returns an empty list (compact: ``count=0``) if
        the project is unknown or its specs dir doesn't exist.
        """
        specs = pipeline.specs.list_specs(project)
        return format_response(
            compact_fn=lambda: compact_summary(
                {
                    "project": project,
                    "count": len(specs),
                    "paths": ",".join(Path(s.path).name for s in specs) or "?",
                }
            ),
            brief_fn=lambda: _format_specs_brief(project, specs),
            full_fn=lambda: _format_specs_full(project, specs),
            detail=detail,
        )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    @mcp.tool()
    async def health_check() -> str:
        """Verify DB path/size, workspace presence, ResearcherClient config.

        MS-01 deliberately omits Ollama/model checks — no LLM calls land
        until MS-02 (per spec rev 2 §Acceptance #8).
        """
        db_path = Path(pipeline.config.db_path)
        db_size = db_path.stat().st_size if db_path.exists() else 0
        workspace = pipeline.config.workspace_root
        lines = [
            f"db: {db_path} ({db_size:,} bytes)",
            f"workspace_root: {workspace} ({'ok' if workspace.exists() else 'MISSING'})",
            f"prompts_dir: {pipeline.config.prompts_dir} "
            f"({'ok' if pipeline.config.prompts_dir.exists() else 'MISSING'})",
            f"projects: {len(pipeline.config.projects)} configured",
            f"researcher: bus_url={pipeline.researcher.bus_url}",
            f"bus: url={pipeline.config.bus.url}",
            "models: parsed but unused (MS-02)",
        ]
        return "\n".join(lines)

    return mcp


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _compact_field(value: object, limit: int = 80) -> str:
    """Sanitize a value for inclusion in compact (pipe-delimited) output.

    ``khonliang.mcp.compact.compact_summary`` escapes the field separator
    ``|`` but does NOT strip newlines. A multi-line value (e.g. a section
    excerpt) would silently corrupt downstream parsers. This helper folds
    newlines and carriage returns to single spaces and truncates to
    ``limit`` characters with an ellipsis.

    Future tools that pass user-content into ``compact_summary`` should
    funnel through here so they can't accidentally break the format.
    """
    if value is None or value == "":
        return "?"
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _format_spec_brief(doc, summary) -> str:
    lines = [
        f"# {summary.title or Path(doc.path).name}",
        f"path: {doc.path}",
        f"fr: {summary.fr or '?'}",
        f"priority: {summary.priority or '?'}",
        f"class: {summary.class_ or '?'}",
        f"status: {summary.status or '?'}",
        "",
        f"sections ({len(doc.sections)}):",
    ]
    for heading in doc.sections:
        lines.append(f"  - {heading}")
    if doc.references:
        lines.append("")
        lines.append(f"references ({len(doc.references)}):")
        for ref in doc.references:
            lines.append(f"  - {ref}")
    return "\n".join(lines)


def _format_spec_full(doc, summary) -> str:
    lines = [_format_spec_brief(doc, summary), "", "--- body ---", doc.text]
    return "\n".join(lines)


def _format_specs_brief(project, specs) -> str:
    if not specs:
        return f"no specs found for project {project!r}"
    lines = [f"specs for {project} ({len(specs)}):"]
    for s in specs:
        lines.append(
            f"  - {Path(s.path).name} | {s.title or '?'} "
            f"| fr={s.fr or '?'} status={s.status or '?'}"
        )
    return "\n".join(lines)


def _format_specs_full(project, specs) -> str:
    if not specs:
        return f"no specs found for project {project!r}"
    lines = [f"specs for {project} ({len(specs)}):", ""]
    for s in specs:
        lines.append(f"  path: {s.path}")
        lines.append(f"  title: {s.title or '?'}")
        lines.append(f"  fr: {s.fr or '?'}")
        lines.append(f"  priority: {s.priority or '?'}")
        lines.append(f"  class: {s.class_ or '?'}")
        lines.append(f"  status: {s.status or '?'}")
        if s.extras:
            lines.append(f"  extras: {s.extras}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_chain_brief(chain) -> str:
    lines = [
        f"milestone: {Path(chain.milestone_path).name}",
        f"title: {chain.milestone_summary.title or '?'}",
        f"fr: {chain.milestone_summary.fr or '?'}",
        f"status: {chain.milestone_summary.status or '?'}",
        "",
        f"specs ({len(chain.specs)}):",
    ]
    for s in chain.specs:
        lines.append(f"  - {Path(s.path).name} | {s.title or '?'}")
    lines.append("")
    lines.append(f"frs ({len(chain.frs)}):")
    for fr in chain.frs:
        marker = "resolved" if fr.resolved else "(unresolved)"
        lines.append(f"  - {fr.fr_id} {marker}")
    if chain.unresolved_links:
        lines.append("")
        lines.append(f"unresolved_links ({len(chain.unresolved_links)}):")
        for link in chain.unresolved_links:
            lines.append(f"  - {link}")
    return "\n".join(lines)


def _format_chain_full(chain) -> str:
    parts = [_format_chain_brief(chain)]
    if chain.frs:
        parts.append("")
        parts.append("--- fr details ---")
        for fr in chain.frs:
            if fr.record is None:
                parts.append(f"{fr.fr_id}: (unresolved - not in developer FR store)")
            else:
                parts.append(
                    f"{fr.fr_id}: {fr.record.title} "
                    f"[{fr.record.priority}] {fr.record.status}"
                )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="khonliang-developer MCP server")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio"],
        help="MCP transport (only stdio in MS-01)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    config = Config.load(args.config)
    pipeline = Pipeline.from_config(config)
    mcp = create_developer_server(pipeline)
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()

"""khonliang-developer: development lifecycle MCP server.

Inverse of khonliang-researcher: consumes the corpus to produce internal
artifacts (specs, milestones, FRs, code, worktrees) instead of ingesting
external knowledge into it.

See CLAUDE.md for the architecture boundary and ecosystem position.
"""

from khonliang_bus import resolve_version as _resolve_version

__version__ = _resolve_version(__name__) or "0.0.0"

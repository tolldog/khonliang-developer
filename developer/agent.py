"""Developer as a native bus agent.

Not a from_mcp wrapper — proper @handler methods returning structured
dicts. The MCP server (developer.server) still exists for direct Claude
connections; this agent is the bus-native path.

Declares collaborative skills with the researcher agent so the bus
exposes cross-agent workflows as single tools.

Usage::

    python -m developer.agent --id developer-primary --bus http://localhost:8787 --config config.yaml
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from khonliang_bus import BaseAgent, Skill, Collaboration, handler

logger = logging.getLogger(__name__)


class DeveloperAgent(BaseAgent):
    agent_type = "developer"
    module_name = "developer.agent"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._pipeline = None  # lazy init
        try:
            from importlib.metadata import version
            self.version = version("khonliang-developer")
        except Exception:
            self.version = "0.1.0"

    @property
    def pipeline(self):
        if self._pipeline is None:
            from developer.config import Config
            from developer.pipeline import Pipeline
            config = Config.load(self.config_path)
            self._pipeline = Pipeline.from_config(config)
        return self._pipeline

    def register_skills(self):
        return [
            Skill("read_spec", "Parse a spec file — metadata, sections, FR references",
                  {"path": {"type": "string", "required": True}, "detail": {"type": "string", "default": "brief"}},
                  since="0.1.0"),
            Skill("list_specs", "Discover spec files for a project",
                  {"project": {"type": "string", "required": True}},
                  since="0.1.0"),
            Skill("traverse_milestone", "Walk milestone → specs → FRs with evidence chain",
                  {"path": {"type": "string", "required": True}},
                  since="0.1.0"),
            Skill("health_check", "DB, workspace, and bus configuration status",
                  since="0.1.0"),
            Skill("developer_guide", "Development workflow guide",
                  since="0.1.0"),
            # Cross-agent skills (use self.request() via bus-lib)
            Skill("get_fr", "Look up an FR via researcher on the bus",
                  {"fr_id": {"type": "string", "required": True}},
                  since="0.2.0"),
            Skill("list_frs", "List FRs for a target via researcher on the bus",
                  {"target": {"type": "string", "required": True}},
                  since="0.2.0"),
            Skill("get_paper_context", "Get research evidence for a query via researcher",
                  {"query": {"type": "string", "required": True}},
                  since="0.2.0"),
        ]

    def register_collaborations(self):
        return [
            Collaboration(
                "evaluate_spec_against_corpus",
                "Evaluate a spec against research evidence via researcher",
                requires={"researcher": ">=0.1.0"},
                steps=[
                    {"call": "developer.read_spec", "args": {"path": "{{args.path}}"}, "output": "spec"},
                    {"call": "researcher.find_relevant", "args": {"query": "{{spec.title}}"}, "output": "papers"},
                    {"call": "researcher.paper_context", "args": {"query": "{{spec.title}}"}, "output": "evidence"},
                ],
            ),
            Collaboration(
                "full_fr_review",
                "Pull FRs, cluster, deduplicate, and rank for a target",
                requires={"researcher": ">=0.1.0"},
                steps=[
                    {"call": "researcher.feature_requests", "args": {"target": "{{args.target}}"}, "output": "frs"},
                    {"call": "researcher.cluster_frs", "args": {"target": "{{args.target}}"}, "output": "clusters"},
                    {"call": "researcher.auto_deduplicate_frs", "args": {"target": "{{args.target}}", "dry_run": True}, "output": "dedup"},
                ],
            ),
        ]

    # -- handlers --

    @handler("read_spec")
    async def handle_read_spec(self, args):
        path = args.get("path", "")
        detail = args.get("detail", "brief")

        try:
            doc = self.pipeline.specs.read(path)
            summary = self.pipeline.specs.summarize(path)
        except Exception as e:
            await self.report_gap("read_spec", f"Failed to read or summarize {path}: {e}")
            raise

        result = {
            "path": doc.path,
            "title": summary.title,
            "fr": summary.fr,
            "priority": summary.priority,
            "class": summary.class_,
            "status": summary.status,
            "sections": list(doc.sections.keys()),
            "references": doc.references,
            "section_count": len(doc.sections),
        }
        if detail == "full":
            result["text"] = doc.text
        return result

    @handler("list_specs")
    async def handle_list_specs(self, args):
        project = args.get("project", "")
        try:
            specs = self.pipeline.specs.list_specs(project)
        except Exception as e:
            await self.report_gap("list_specs", f"Failed to list specs for {project}: {e}")
            raise
        return {
            "project": project,
            "count": len(specs),
            "specs": [
                {
                    "path": s.path,
                    "title": s.title,
                    "fr": s.fr,
                    "status": s.status,
                    "priority": s.priority,
                }
                for s in specs
            ],
        }

    @handler("traverse_milestone")
    async def handle_traverse_milestone(self, args):
        path = args.get("path", "")
        try:
            chain = await self.pipeline.specs.traverse_milestone(path)
        except Exception as e:
            await self.report_gap("traverse_milestone", f"Failed to traverse {path}: {e}")
            raise

        return {
            "milestone_path": chain.milestone_path,
            "title": chain.milestone_summary.title,
            "fr": chain.milestone_summary.fr,
            "status": chain.milestone_summary.status,
            "specs": [
                {"path": s.path, "title": s.title, "fr": s.fr}
                for s in chain.specs
            ],
            "frs": [
                {"fr_id": f.fr_id, "resolved": f.resolved}
                for f in chain.frs
            ],
            "unresolved_links": chain.unresolved_links,
        }

    @handler("health_check")
    async def handle_health_check(self, args):
        try:
            config = self.pipeline.config
            db_path = Path(config.db_path)
            db_size = db_path.stat().st_size if db_path.exists() else 0
            workspace_ok = config.workspace_root.exists()
        except Exception as e:
            await self.report_gap("health_check", f"Failed to check health: {e}")
            raise
        return {
            "db_path": str(db_path),
            "db_size_bytes": db_size,
            "workspace_root": str(config.workspace_root),
            "workspace_exists": workspace_ok,
            "projects": len(config.projects),
            "bus_url": self.bus_url,
            "agent_id": self.agent_id,
        }

    @handler("developer_guide")
    async def handle_developer_guide(self, args):
        return {"guide": self.pipeline.developer_guide_text}

    # -- cross-agent skills (via bus-lib self.request) --

    @handler("get_fr")
    async def handle_get_fr(self, args):
        fr_id = args.get("fr_id", "")
        try:
            result = await self.request(
                agent_type="researcher",
                operation="knowledge_search",
                args={"query": fr_id, "detail": "full", "max_results": 1},
            )
        except Exception as e:
            await self.report_gap("get_fr", f"Bus request failed for {fr_id}: {e}")
            raise
        if not result:
            return {"error": "no result from researcher"}
        return result.get("result", {"error": "no result from researcher"})

    @handler("list_frs")
    async def handle_list_frs(self, args):
        target = args.get("target", "")
        try:
            result = await self.request(
                agent_type="researcher",
                operation="feature_requests",
                args={"target": target, "detail": "brief"},
            )
        except Exception as e:
            await self.report_gap("list_frs", f"Bus request failed for {target}: {e}")
            raise
        if not result:
            return {"error": "no result from researcher"}
        return result.get("result", {"error": "no result from researcher"})

    @handler("get_paper_context")
    async def handle_get_paper_context(self, args):
        query = args.get("query", "")
        try:
            result = await self.request(
                agent_type="researcher",
                operation="paper_context",
                args={"query": query, "detail": "full"},
            )
        except Exception as e:
            await self.report_gap("get_paper_context", f"Bus request failed for {query!r}: {e}")
            raise
        if not result:
            return {"error": "no result from researcher"}
        return result.get("result", {"error": "no result from researcher"})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="khonliang-developer bus agent")
    parser.add_argument("command", nargs="?", choices=["install", "uninstall"])
    parser.add_argument("--id", default="developer-primary")
    parser.add_argument("--bus", default="http://localhost:8787")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if args.command in ("install", "uninstall"):
        BaseAgent.from_cli([
            args.command,
            "--id", args.id,
            "--bus", args.bus,
            "--config", args.config,
        ])
        return

    agent = DeveloperAgent(
        agent_id=args.id,
        bus_url=args.bus,
        config_path=args.config,
    )
    asyncio.run(agent.start())


if __name__ == "__main__":
    main()

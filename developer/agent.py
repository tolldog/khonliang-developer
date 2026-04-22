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
import json
import logging
import re
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
            Skill("get_fr", "Look up an FR from developer's FR store",
                  {"fr_id": {"type": "string", "required": True}},
                  since="0.2.0"),
            Skill("list_frs", "List FRs from developer's FR store",
                  {"target": {"type": "string", "default": ""},
                   "status": {"type": "string", "default": ""},
                   "include_all": {"type": "boolean", "default": False}},
                  since="0.2.0"),
            Skill("get_paper_context", "Get research evidence for a query via researcher",
                  {"query": {"type": "string", "required": True}},
                  since="0.2.0"),
            Skill("fr_candidates_from_concepts",
                  "Generate promote-ready FR candidates from researcher concept bundles",
                  {"target": {"type": "string", "default": "developer"},
                   "max_concepts": {"type": "integer", "default": 8},
                   "min_score": {"type": "number", "default": 0.0},
                   "detail": {"type": "string", "default": "brief"},
                   "timeout_s": {"type": "number", "default": 90}},
                  since="0.9.0"),
            # Developer-owned FR work planning
            Skill("next_work_unit", "Get the highest-ranked developer FR work unit",
                  {"target": {"type": "string", "default": ""},
                   "max_frs": {"type": "integer", "default": 5,
                               "description": "maximum FRs per returned implementation bundle"}},
                  since="0.2.0"),
            Skill("work_units", "List developer-owned FR work units ranked by importance",
                  {"target": {"type": "string", "default": ""},
                   "max_frs": {"type": "integer", "default": 5,
                               "description": "maximum FRs per implementation bundle"}},
                  since="0.2.0"),
            Skill("propose_milestone_from_work_unit",
                  "Create a durable milestone from a provided or top-ranked work unit",
                  {"target": {"type": "string", "default": ""},
                   "title": {"type": "string", "default": ""},
                   "summary": {"type": "string", "default": ""},
                   "work_unit": {"type": "string", "default": "",
                                 "description": "optional JSON work unit; omitted uses next_work_unit"}},
                  since="0.8.0"),
            Skill("get_milestone", "Look up a developer milestone by id",
                  {"milestone_id": {"type": "string", "required": True}},
                  since="0.8.0"),
            Skill("list_milestones", "List developer milestones",
                  {"target": {"type": "string", "default": ""},
                   "status": {"type": "string", "default": ""},
                   "include_archived": {"type": "boolean", "default": False}},
                  since="0.8.0"),
            Skill("draft_spec_from_milestone", "Return the milestone's deterministic draft spec",
                  {"milestone_id": {"type": "string", "required": True}},
                  since="0.8.0"),
            Skill("review_milestone_scope",
                  "Flag duplicate or review-term FRs before implementing a milestone",
                  {"milestone_id": {"type": "string", "required": True},
                   "review_terms": {"type": "string", "default": "AutoGen,GRA"}},
                  since="0.8.0"),
            Skill("migrate_frs_from_researcher",
                  "One-way migration of researcher-owned FRs into developer's store",
                  {"source_db": {"type": "string", "required": True},
                   "apply": {"type": "boolean", "default": False}},
                  since="0.10.0"),
            Skill("prepare_development_handoff",
                  "Return a compact bundle → milestone → draft spec handoff for implementation",
                  {"target": {"type": "string", "default": ""},
                   "title": {"type": "string", "default": ""},
                   "summary": {"type": "string", "default": ""},
                   "work_unit": {"type": "string", "default": "",
                                 "description": "optional JSON work unit; omitted uses next_work_unit"},
                   "review_terms": {"type": "string", "default": "AutoGen,GRA"}},
                  since="0.10.0"),
            Skill("create_session_checkpoint",
                  "Create a compact checkpoint for disposable external LLM sessions",
                  {"fr_id": {"type": "string", "default": ""},
                   "cwd": {"type": "string", "required": True},
                   "repo": {"type": "string", "default": ""},
                   "pr_number": {"type": "integer", "default": 0},
                   "work_unit": {"type": "string", "default": "",
                                 "description": "optional JSON work unit"},
                   "context_tokens": {"type": "integer", "default": 0},
                   "context_limit": {"type": "integer", "default": 0},
                   "idle_minutes": {"type": "number", "default": 0},
                   "tests": {"type": "string", "default": "",
                             "description": "optional JSON test digest"},
                   "evidence_query": {"type": "string", "default": ""},
                   "next_actions": {"type": "string", "default": ""}},
                  since="0.12.0"),
            Skill("resume_session_checkpoint",
                  "Build a launch briefing from a checkpoint and detect stale git/PR state",
                  {"checkpoint": {"type": "string", "required": True},
                   "cwd": {"type": "string", "required": True},
                   "repo": {"type": "string", "default": ""},
                   "pr_number": {"type": "integer", "default": 0}},
                  since="0.12.0"),
            Skill("audit_repo_hygiene",
                  "Audit a repo for stale paths, docs drift, config hygiene, and test plan",
                  {"repo_path": {"type": "string", "required": True},
                   "include_text_scan": {"type": "boolean", "default": True},
                   "max_text_files": {"type": "integer", "default": 120}},
                  since="0.13.0"),
            Skill("apply_repo_hygiene_plan",
                  "Write the generated repo hygiene audit artifact into the target repo",
                  {"repo_path": {"type": "string", "required": True},
                   "audit_path": {"type": "string", "default": "docs/repo-hygiene-audit.md"},
                   "overwrite": {"type": "boolean", "default": True},
                   "include_text_scan": {"type": "boolean", "default": True},
                   "max_text_files": {"type": "integer", "default": 120}},
                  since="0.13.0"),
            # Test run + distill (token-arbitrage: pay local pytest cost once,
            # serve cheap digest to Claude instead of raw pytest output)
            Skill("run_tests", "Run project pytest suite and return a distilled digest",
                  {"project": {"type": "string", "required": True},
                   "target": {"type": "string", "default": ""},
                   "detail": {"type": "string", "default": "brief"},
                   "timeout_s": {"type": "number", "default": 300}},
                  since="0.3.0"),
            Skill("pr_ready", "Classify GitHub PR review and merge readiness",
                  {"repo": {"type": "string", "required": True,
                            "description": "GitHub repo in owner/name form"},
                   "pr_number": {"type": "integer", "required": True}},
                  since="0.11.0"),
            # Developer-owned FR lifecycle. Researcher is no longer an FR
            # authority; migrate old researcher FR ids into developer before
            # executing against them so external references keep working.
            Skill("promote_fr", "Create a new FR in developer's store",
                  {"target": {"type": "string", "required": True},
                   "title": {"type": "string", "required": True},
                   "description": {"type": "string", "required": True},
                   "priority": {"type": "string", "default": "medium"},
                   "concept": {"type": "string", "default": ""},
                   "classification": {"type": "string", "default": "app"},
                   "backing_papers": {"type": "string", "default": ""}},
                  since="0.4.0"),
            Skill("update_fr_status", "Advance an FR's lifecycle status",
                  {"fr_id": {"type": "string", "required": True},
                   "status": {"type": "string", "required": True},
                   "branch": {"type": "string", "default": ""},
                   "notes": {"type": "string", "default": ""}},
                  since="0.4.0"),
            Skill("set_fr_dependency", "Set an FR's depends_on list (replaces prior deps)",
                  {"fr_id": {"type": "string", "required": True},
                   "depends_on": {"type": "string", "required": True}},
                  since="0.4.0"),
            Skill("merge_frs", "Merge multiple FRs into a new one. Old FRs go "
                  "to terminal 'merged' state; dep edges redirect to the new FR.",
                  {"source_ids": {"type": "string", "required": True,
                                  "description": "comma-separated FR ids"},
                   "title": {"type": "string", "required": True},
                   "description": {"type": "string", "required": True},
                   "priority": {"type": "string", "default": ""},
                   "concept": {"type": "string", "default": ""},
                   "classification": {"type": "string", "default": "app"},
                   "merge_note": {"type": "string", "default": ""},
                   "merge_roles": {"type": "string", "default": "",
                                   "description": "optional; 'id1=role1,id2=role2'"}},
                  since="0.5.0"),
            Skill("get_fr_local", "Look up an FR from developer's own store "
                  "(follows merge redirects by default)",
                  {"fr_id": {"type": "string", "required": True},
                   "follow_redirect": {"type": "boolean", "default": True}},
                  since="0.4.0"),
            Skill("list_frs_local", "List FRs from developer's own store. "
                  "Default filters out terminal states.",
                  {"target": {"type": "string", "default": ""},
                   "status": {"type": "string", "default": ""},
                   "include_all": {"type": "boolean", "default": False}},
                  since="0.4.0"),
            Skill("update_fr", "Edit an existing FR in place. Terminal FRs "
                  "are immutable.",
                  {"fr_id": {"type": "string", "required": True},
                   "title": {"type": "string", "default": ""},
                   "description": {"type": "string", "default": ""},
                   "priority": {"type": "string", "default": ""},
                   "concept": {"type": "string", "default": ""},
                   "classification": {"type": "string", "default": ""},
                   "backing_papers": {"type": "string", "default": "__NOCHANGE__",
                                      "description": "comma-separated; '__NOCHANGE__' keeps existing, empty string clears"},
                   "notes": {"type": "string", "default": ""}},
                  since="0.6.0"),
            Skill("next_fr_local", "Pick the highest-priority open/planned FR "
                  "whose deps are completed. Returns null when nothing's ready.",
                  {"target": {"type": "string", "default": ""}},
                  since="0.6.0"),
            # Native git operations (fr_developer_e778b9bf). Each takes a
            # `cwd` (repo path); destructive ops require explicit flags.
            Skill("git_status", "Working-tree status for a repo",
                  {"cwd": {"type": "string", "required": True}},
                  since="0.5.0"),
            Skill("git_log", "Recent commits for a repo",
                  {"cwd": {"type": "string", "required": True},
                   "ref": {"type": "string", "default": "HEAD"},
                   "limit": {"type": "integer", "default": 20}},
                  since="0.5.0"),
            Skill("git_diff", "Diff working-tree vs ref, or between two refs",
                  {"cwd": {"type": "string", "required": True},
                   "ref_a": {"type": "string", "default": "HEAD"},
                   "ref_b": {"type": "string", "default": ""}},
                  since="0.5.0"),
            Skill("git_branches", "List local and/or remote branches",
                  {"cwd": {"type": "string", "required": True},
                   "local": {"type": "boolean", "default": True},
                   "remote": {"type": "boolean", "default": False}},
                  since="0.5.0"),
            Skill("git_commit", "Commit staged changes",
                  {"cwd": {"type": "string", "required": True},
                   "message": {"type": "string", "required": True},
                   "co_authors": {"type": "string", "default": "",
                                  "description": "comma-separated Name <email> list"}},
                  since="0.5.0"),
            Skill("git_stage", "Stage paths for commit",
                  {"cwd": {"type": "string", "required": True},
                   "paths": {"type": "string", "required": True,
                             "description": "comma-separated paths"}},
                  since="0.7.0"),
            Skill("git_unstage", "Unstage paths while keeping working-tree changes",
                  {"cwd": {"type": "string", "required": True},
                   "paths": {"type": "string", "required": True,
                             "description": "comma-separated paths"}},
                  since="0.7.0"),
            Skill("git_checkout", "Checkout a ref or create and switch to a new branch",
                  {"cwd": {"type": "string", "required": True},
                   "ref": {"type": "string", "required": True},
                   "new_branch": {"type": "boolean", "default": False}},
                  since="0.7.0"),
            Skill("git_create_branch", "Create a local branch without switching",
                  {"cwd": {"type": "string", "required": True},
                   "name": {"type": "string", "required": True},
                   "base": {"type": "string", "default": ""}},
                  since="0.7.0"),
            Skill("git_delete_branch", "Delete a local branch; force requires explicit flag",
                  {"cwd": {"type": "string", "required": True},
                   "name": {"type": "string", "required": True},
                   "force": {"type": "boolean", "default": False}},
                  since="0.7.0"),
            Skill("git_fetch", "Fetch from a remote",
                  {"cwd": {"type": "string", "required": True},
                   "remote": {"type": "string", "default": "origin"}},
                  since="0.7.0"),
            Skill("git_pull", "Pull into the current branch, ff-only by default",
                  {"cwd": {"type": "string", "required": True},
                   "remote": {"type": "string", "default": ""},
                   "branch": {"type": "string", "default": ""},
                   "ff_only": {"type": "boolean", "default": True}},
                  since="0.7.0"),
            Skill("git_push", "Push a branch to a remote",
                  {"cwd": {"type": "string", "required": True},
                   "remote": {"type": "string", "default": "origin"},
                   "branch": {"type": "string", "default": ""},
                   "force": {"type": "boolean", "default": False},
                   "set_upstream": {"type": "boolean", "default": False}},
                  since="0.7.0"),
            Skill("git_show", "Resolve and summarize a commit",
                  {"cwd": {"type": "string", "required": True},
                   "ref": {"type": "string", "required": True}},
                  since="0.7.0"),
            Skill("git_rev_parse", "Resolve a ref to its full SHA",
                  {"cwd": {"type": "string", "required": True},
                   "ref": {"type": "string", "required": True}},
                  since="0.7.0"),
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

    # -- researcher evidence/concept skills (via bus-lib self.request) --

    @handler("get_fr")
    async def handle_get_fr(self, args):
        return await self.handle_get_fr_local(args)

    @handler("list_frs")
    async def handle_list_frs(self, args):
        return await self.handle_list_frs_local(args)

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
        return (result and result.get("result")) or {"error": "no result from researcher"}

    @handler("fr_candidates_from_concepts")
    async def handle_fr_candidates_from_concepts(self, args):
        """Turn researcher concept bundles into developer-owned FR candidates."""
        target = str(args.get("target") or "developer").strip() or "developer"
        max_concepts = int(args.get("max_concepts") or 8)
        min_score = float(args.get("min_score") or 0.0)
        detail = args.get("detail", "brief")
        raw_timeout_s = args.get("timeout_s", 90)
        try:
            timeout_s = float(raw_timeout_s)
        except (TypeError, ValueError):
            message = f"timeout_s must be a number, got {raw_timeout_s!r}"
            await self._safe_report_gap("fr_candidates_from_concepts", message)
            return {"error": message}
        if timeout_s <= 0:
            message = f"timeout_s must be greater than 0, got {raw_timeout_s!r}"
            await self._safe_report_gap("fr_candidates_from_concepts", message)
            return {"error": message}
        try:
            result = await self.request(
                agent_type="researcher",
                operation="synergize_concepts",
                args={
                    "detail": detail,
                    "max_concepts": max_concepts,
                    "min_score": min_score,
                },
                timeout=timeout_s,
            )
        except Exception as e:
            await self.report_gap(
                "fr_candidates_from_concepts",
                f"Failed to get concept bundles: {e}",
            )
            raise

        raw = (result and result.get("result")) or {}
        text = raw.get("result", "") if isinstance(raw, dict) else str(raw)
        bundles = _parse_concept_bundles(text)
        existing = self.pipeline.frs.list(target=target or None, include_all=True)
        candidates = [
            _candidate_from_concept_bundle(bundle, target=target, existing=existing)
            for bundle in bundles
        ]
        return {
            "source": "researcher.synergize_concepts",
            "target": target,
            "bundle_count": len(bundles),
            "candidate_count": len(candidates),
            "new_count": sum(1 for c in candidates if c["status"] == "new_candidate"),
            "existing_match_count": sum(1 for c in candidates if c["existing_matches"]),
            "candidates": candidates,
        }

    # -- developer-owned FR work planning --

    @handler("work_units")
    async def handle_work_units(self, args):
        """Return developer-owned FR work units ranked by local FR state."""
        target = args.get("target") or None
        max_frs = _positive_int_arg(args, "max_frs", default=5)
        if isinstance(max_frs, dict):
            await self._safe_report_gap("work_units", max_frs["error"])
            return max_frs
        frs = self.pipeline.frs.list(target=target)
        if not frs:
            return {"work_units": [], "source": "none", "error": "no developer-owned FRs available"}

        work_units = _work_units_from_local_frs(frs, max_size=max_frs)
        return {
            "work_units": work_units,
            "source": "developer_local",
            "count": len(work_units),
            "max_frs": max_frs,
        }

    @handler("next_work_unit")
    async def handle_next_work_unit(self, args):
        """Get the single highest-ranked work unit."""
        result = await self.handle_work_units(args)
        if "error" in result:
            return {"error": result["error"]}
        units = result.get("work_units", [])
        if not units:
            return {"error": "no work units available"}
        return {
            "work_unit": units[0],
            "remaining": len(units) - 1,
            "source": result.get("source", "unknown"),
            "max_frs": result.get("max_frs", 5),
        }

    @handler("propose_milestone_from_work_unit")
    async def handle_propose_milestone_from_work_unit(self, args):
        from developer.milestone_store import MilestoneError

        try:
            work_unit = _parse_work_unit_arg(args.get("work_unit"))
            source = "provided_work_unit"
            if not work_unit:
                next_result = await self.handle_next_work_unit(args)
                if "error" in next_result:
                    return next_result
                work_unit = next_result.get("work_unit") or {}
                source = next_result.get("source", "work_unit")
            milestone = self.pipeline.milestones.propose_from_work_unit(
                work_unit,
                target=args.get("target", ""),
                title=args.get("title", ""),
                summary=args.get("summary", ""),
                source=source,
            )
        except (MilestoneError, ValueError, TypeError) as e:
            await self.report_gap("propose_milestone_from_work_unit", str(e))
            return {"error": str(e)}
        return {"milestone": milestone.to_public_dict()}

    @handler("get_milestone")
    async def handle_get_milestone(self, args):
        milestone_id = args.get("milestone_id", "")
        if not milestone_id:
            return {"error": "milestone_id is required"}
        milestone = self.pipeline.milestones.get(milestone_id)
        if milestone is None:
            return {"milestone": None, "reason": f"unknown milestone id: {milestone_id}"}
        return {"milestone": milestone.to_public_dict()}

    @handler("list_milestones")
    async def handle_list_milestones(self, args):
        from developer.milestone_store import MilestoneError

        try:
            milestones = self.pipeline.milestones.list(
                target=args.get("target", ""),
                status=args.get("status", ""),
                include_archived=bool(args.get("include_archived", False)),
            )
        except MilestoneError as e:
            await self.report_gap("list_milestones", str(e))
            return {"error": str(e)}
        return {
            "count": len(milestones),
            "milestones": [m.to_public_dict() for m in milestones],
        }

    @handler("draft_spec_from_milestone")
    async def handle_draft_spec_from_milestone(self, args):
        milestone_id = args.get("milestone_id", "")
        if not milestone_id:
            return {"error": "milestone_id is required"}
        milestone = self.pipeline.milestones.get(milestone_id)
        if milestone is None:
            return {"error": f"unknown milestone id: {milestone_id}"}
        return {
            "milestone_id": milestone.id,
            "title": milestone.title,
            "draft_spec": milestone.draft_spec,
        }

    @handler("review_milestone_scope")
    async def handle_review_milestone_scope(self, args):
        from developer.milestone_store import MilestoneError

        milestone_id = args.get("milestone_id", "")
        if not milestone_id:
            return {"error": "milestone_id is required"}
        try:
            return self.pipeline.milestones.review_scope(
                milestone_id,
                review_terms=_parse_paths(args.get("review_terms", "AutoGen,GRA")),
            )
        except MilestoneError as e:
            await self.report_gap("review_milestone_scope", str(e))
            return {"error": str(e)}

    @handler("migrate_frs_from_researcher")
    async def handle_migrate_frs_from_researcher(self, args):
        """Copy researcher FR records into developer's authoritative FR store."""
        from developer.migrations.fr_data_from_researcher import migrate

        source_db = args.get("source_db", "")
        if not source_db:
            return {"error": "source_db is required"}
        try:
            report = migrate(source_db, self.pipeline.frs, apply=bool(args.get("apply", False)))
        except Exception as e:
            await self.report_gap("migrate_frs_from_researcher", str(e))
            return {"error": str(e)}
        return {
            "dry_run": report.dry_run,
            "source_db": report.source_db,
            "target_db": report.target_db,
            "frs_found": report.frs_found,
            "frs_migrated": report.frs_migrated,
            "frs_already_present": report.frs_already_present,
            "frs_skipped": list(report.frs_skipped),
            "capabilities_found": report.capabilities_found,
            "capabilities_migrated": report.capabilities_migrated,
            "capabilities_already_present": report.capabilities_already_present,
            "summary": report.summary(),
        }

    @handler("prepare_development_handoff")
    async def handle_prepare_development_handoff(self, args):
        """Create the compact handoff a coding session needs to start work."""
        from developer.milestone_store import MilestoneError

        try:
            work_unit = _parse_work_unit_arg(args.get("work_unit"))
            source = "provided_work_unit"
            remaining = None
            if not work_unit:
                next_result = await self.handle_next_work_unit(args)
                if "error" in next_result:
                    return next_result
                work_unit = next_result.get("work_unit") or {}
                source = next_result.get("source", "work_unit")
                remaining = next_result.get("remaining")

            milestone = self.pipeline.milestones.propose_from_work_unit(
                work_unit,
                target=args.get("target", ""),
                title=args.get("title", ""),
                summary=args.get("summary", ""),
                source=source,
            )
            review_terms = _parse_paths(args.get("review_terms", "AutoGen,GRA"))
            review = self.pipeline.milestones.review_scope(
                milestone.id,
                review_terms=review_terms,
            )
        except (MilestoneError, ValueError, TypeError) as e:
            await self.report_gap("prepare_development_handoff", str(e))
            return {"error": str(e)}

        recommendation = review.get("recommendation", "")
        response = {
            "status": "ready" if recommendation == "ready_for_spec" else "needs_review",
            "source": source,
            "milestone": milestone.to_public_dict(),
            "work_unit": work_unit,
            "scope_review": review,
            "draft_spec": milestone.draft_spec,
            "suggested_next_actions": _handoff_next_actions(
                milestone.id,
                recommendation,
                review_terms=review_terms,
            ),
        }
        if remaining is not None:
            response["remaining_work_units"] = remaining
        return response

    @handler("create_session_checkpoint")
    async def handle_create_session_checkpoint(self, args):
        """Create a compact checkpoint for cache-aware session exit/resume."""
        from developer.git_client import GitClient, GitClientError
        from developer.session_checkpoint import build_session_checkpoint

        cwd = args.get("cwd", "")
        if not cwd:
            return {"error": "cwd is required"}
        fr_id = args.get("fr_id", "")
        fr = None
        if fr_id:
            fr = self.pipeline.frs.get(fr_id)
            if fr is None:
                return {"error": f"unknown FR id: {fr_id}"}
        try:
            git = GitClient(cwd)
            status = git.status()
            head_sha = git.rev_parse("HEAD")
        except GitClientError as e:
            await self._safe_report_gap("create_session_checkpoint", str(e))
            return {"error": str(e)}

        work_unit = _parse_json_dict(args.get("work_unit"))
        if isinstance(work_unit, dict) and "error" in work_unit:
            return work_unit
        pr_ready = await self._optional_pr_ready(args)
        if isinstance(pr_ready, dict) and "error" in pr_ready:
            return pr_ready
        tests = _parse_json_dict(args.get("tests"))
        if isinstance(tests, dict) and "error" in tests:
            return tests
        evidence = []
        evidence_query = str(args.get("evidence_query") or "").strip()
        if evidence_query:
            try:
                evidence_result = await self.handle_get_paper_context({"query": evidence_query})
            except Exception as e:
                await self._safe_report_gap("create_session_checkpoint", str(e))
                evidence_result = {"error": str(e)}
            evidence.append({
                "query": evidence_query,
                "summary": _compact_jsonish(evidence_result, limit=1200),
            })
        checkpoint = build_session_checkpoint(
            fr=fr,
            work_unit=work_unit,
            repo_path=str(cwd),
            git_status=status,
            head_sha=head_sha,
            pr_ready=pr_ready,
            tests=tests or None,
            evidence=evidence,
            agent_state={
                "agent_id": self.agent_id,
                "bus_url": self.bus_url,
                "developer_db": str(self.pipeline.config.db_path),
            },
            next_actions=_parse_paths(args.get("next_actions", "")),
            context_tokens=_int_arg(args, "context_tokens", 0),
            context_limit=_int_arg(args, "context_limit", 0),
            idle_minutes=_float_arg(args, "idle_minutes", 0.0),
        )
        return {"checkpoint": checkpoint}

    @handler("resume_session_checkpoint")
    async def handle_resume_session_checkpoint(self, args):
        """Build a compact launch briefing from a prior checkpoint."""
        from developer.git_client import GitClient, GitClientError
        from developer.session_checkpoint import build_resume_briefing

        cwd = args.get("cwd", "")
        if not cwd:
            return {"error": "cwd is required"}
        checkpoint = _parse_json_dict(args.get("checkpoint"))
        if not checkpoint:
            return {"error": "checkpoint is required"}
        if "error" in checkpoint:
            return checkpoint
        try:
            git = GitClient(cwd)
            status = git.status()
            head_sha = git.rev_parse("HEAD")
        except GitClientError as e:
            await self._safe_report_gap("resume_session_checkpoint", str(e))
            return {"error": str(e)}

        pr_ready = await self._optional_pr_ready(args)
        if isinstance(pr_ready, dict) and "error" in pr_ready:
            return pr_ready
        return {
            "resume": build_resume_briefing(
                checkpoint,
                current_git_status=status,
                current_head_sha=head_sha,
                current_pr_ready=pr_ready,
            )
        }

    @handler("audit_repo_hygiene")
    async def handle_audit_repo_hygiene(self, args):
        """Return compact repo hygiene sections without writing files."""
        from developer.repo_hygiene import audit_repo_hygiene

        repo_path = args.get("repo_path", "")
        if not repo_path:
            return {"error": "repo_path is required"}
        try:
            audit = audit_repo_hygiene(
                repo_path,
                include_text_scan=_bool_arg(args, "include_text_scan", True),
                max_text_files=_int_arg(args, "max_text_files", 120),
            )
        except Exception as e:
            await self._safe_report_gap("audit_repo_hygiene", str(e))
            return {"error": str(e)}
        return {"audit": audit.to_dict()}

    @handler("apply_repo_hygiene_plan")
    async def handle_apply_repo_hygiene_plan(self, args):
        """Write the generated repo hygiene audit artifact into a repo."""
        from developer.repo_hygiene import (
            apply_repo_hygiene_plan,
            audit_repo_hygiene,
        )

        repo_path = args.get("repo_path", "")
        if not repo_path:
            return {"error": "repo_path is required"}
        try:
            audit = audit_repo_hygiene(
                repo_path,
                include_text_scan=_bool_arg(args, "include_text_scan", True),
                max_text_files=_int_arg(args, "max_text_files", 120),
            )
            applied = apply_repo_hygiene_plan(
                audit,
                audit_path=args.get("audit_path") or "docs/repo-hygiene-audit.md",
                overwrite=_bool_arg(args, "overwrite", True),
            )
        except Exception as e:
            await self._safe_report_gap("apply_repo_hygiene_plan", str(e))
            return {"error": str(e)}
        result = audit.to_dict()
        result["applied_changes"] = applied.get("applied_changes", [])
        result["skipped"] = applied.get("skipped", "")
        return {"audit": result}

    # -- developer-owned FR lifecycle --

    @handler("promote_fr")
    async def handle_promote_fr(self, args):
        from developer.fr_store import FRError

        try:
            backing = [p.strip() for p in (args.get("backing_papers") or "").split(",") if p.strip()]
            fr = self.pipeline.frs.promote(
                target=args.get("target", ""),
                title=args.get("title", ""),
                description=args.get("description", ""),
                priority=args.get("priority", "medium"),
                concept=args.get("concept", ""),
                classification=args.get("classification", "app"),
                backing_papers=backing,
            )
        except FRError as e:
            await self.report_gap("promote_fr", str(e))
            return {"error": str(e)}
        except Exception as e:
            await self.report_gap("promote_fr", f"unexpected failure: {e}")
            raise
        return {
            "fr_id": fr.id,
            "target": fr.target,
            "priority": fr.priority,
            "status": fr.status,
        }

    @handler("update_fr_status")
    async def handle_update_fr_status(self, args):
        from developer.fr_store import FRError

        try:
            fr = self.pipeline.frs.update_status(
                fr_id=args.get("fr_id", ""),
                status=args.get("status", ""),
                branch=args.get("branch", ""),
                notes=args.get("notes", ""),
            )
        except FRError as e:
            await self.report_gap("update_fr_status", str(e))
            return {"error": str(e)}
        except Exception as e:
            await self.report_gap("update_fr_status", f"unexpected failure: {e}")
            raise
        return {
            "fr_id": fr.id,
            "status": fr.status,
            "branch": fr.branch,
            "updated_at": fr.updated_at,
        }

    @handler("set_fr_dependency")
    async def handle_set_fr_dependency(self, args):
        from developer.fr_store import FRError

        deps_raw = args.get("depends_on", "")
        if isinstance(deps_raw, list):
            deps = [str(d).strip() for d in deps_raw if str(d).strip()]
        else:
            deps = [d.strip() for d in str(deps_raw).split(",") if d.strip()]
        try:
            fr = self.pipeline.frs.set_dependency(
                fr_id=args.get("fr_id", ""),
                depends_on=deps,
            )
        except FRError as e:
            await self.report_gap("set_fr_dependency", str(e))
            return {"error": str(e)}
        except Exception as e:
            await self.report_gap("set_fr_dependency", f"unexpected failure: {e}")
            raise
        return {"fr_id": fr.id, "depends_on": fr.depends_on}

    @handler("merge_frs")
    async def handle_merge_frs(self, args):
        """Merge multiple FRs into a new consolidated FR.

        ``source_ids`` is a comma-separated id list. ``merge_roles`` is an
        optional ``id1=role description 1, id2=role description 2`` map.
        Priority empty → inherit max priority across sources.
        """
        from developer.fr_store import FRError

        raw_ids = args.get("source_ids", "")
        if isinstance(raw_ids, list):
            source_ids = [str(i).strip() for i in raw_ids if str(i).strip()]
        else:
            source_ids = [i.strip() for i in str(raw_ids).split(",") if i.strip()]

        # Parse merge_roles from "id1=role1, id2=role2" format.
        merge_roles: dict[str, str] = {}
        raw_roles = args.get("merge_roles", "")
        if raw_roles:
            for part in str(raw_roles).split(","):
                part = part.strip()
                if "=" not in part:
                    continue
                key, _, value = part.partition("=")
                merge_roles[key.strip()] = value.strip()

        priority = args.get("priority") or None

        try:
            fr = self.pipeline.frs.merge(
                source_ids=source_ids,
                title=args.get("title", ""),
                description=args.get("description", ""),
                priority=priority,
                concept=args.get("concept", ""),
                classification=args.get("classification", "app"),
                merge_note=args.get("merge_note", ""),
                merge_roles=merge_roles or None,
            )
        except FRError as e:
            await self.report_gap("merge_frs", str(e))
            return {"error": str(e)}
        except Exception as e:
            await self.report_gap("merge_frs", f"unexpected failure: {e}")
            raise
        return {
            "fr_id": fr.id,
            "merged_from": list(fr.merged_from),
            "priority": fr.priority,
            "status": fr.status,
        }

    @handler("get_fr_local")
    async def handle_get_fr_local(self, args):
        from developer.fr_store import FRError

        follow_redirect = bool(args.get("follow_redirect", True))
        try:
            fr = self.pipeline.frs.get(
                fr_id=args.get("fr_id", ""),
                follow_redirect=follow_redirect,
            )
        except FRError as e:
            # FRStore.get raises on redirect cycle / exceeded depth. Surface
            # as a structured error rather than crashing the skill.
            await self.report_gap("get_fr_local", str(e))
            return {"error": str(e), "fr_id": args.get("fr_id", "")}
        except Exception as e:
            await self.report_gap("get_fr_local", f"unexpected failure: {e}")
            raise
        if fr is None:
            return {"error": "not found", "fr_id": args.get("fr_id", "")}
        return fr.to_public_dict()

    @handler("list_frs_local")
    async def handle_list_frs_local(self, args):
        status = args.get("status") or None
        target = args.get("target") or None
        include_all = bool(args.get("include_all", False))
        frs = self.pipeline.frs.list(
            target=target, status=status, include_all=include_all,
        )
        return {
            "count": len(frs),
            "frs": [f.to_public_dict() for f in frs],
        }

    @handler("update_fr")
    async def handle_update_fr(self, args):
        """Edit an FR in place.

        Empty-string values for title/description/priority/concept/
        classification are treated as "no change" (so callers can omit
        fields). backing_papers uses the sentinel '__NOCHANGE__' to mean
        "don't touch," since an empty string legitimately means "clear."
        """
        from developer.fr_store import FRError

        fr_id = args.get("fr_id", "")
        if not fr_id:
            return {"error": "fr_id is required"}

        # Translate empty-string "no change" inputs to Python None.
        def _opt(key: str) -> str | None:
            v = args.get(key, "")
            return v if v else None

        backing_raw = args.get("backing_papers", "__NOCHANGE__")
        if backing_raw == "__NOCHANGE__":
            backing = None
        elif isinstance(backing_raw, list):
            backing = [str(b).strip() for b in backing_raw if str(b).strip()]
        else:
            backing = [b.strip() for b in str(backing_raw).split(",") if b.strip()]

        try:
            fr = self.pipeline.frs.update(
                fr_id=fr_id,
                title=_opt("title"),
                description=_opt("description"),
                priority=_opt("priority"),
                concept=_opt("concept"),
                classification=_opt("classification"),
                backing_papers=backing,
                notes=args.get("notes", ""),
            )
        except FRError as e:
            await self.report_gap("update_fr", str(e))
            return {"error": str(e)}
        except Exception as e:
            await self.report_gap("update_fr", f"unexpected failure: {e}")
            raise
        return fr.to_public_dict()

    @handler("next_fr_local")
    async def handle_next_fr_local(self, args):
        target = args.get("target") or None
        fr = self.pipeline.frs.next_fr(target=target)
        if fr is None:
            return {"fr": None, "reason": "no ready FRs (all in-progress, blocked, or terminal)"}
        return {"fr": fr.to_public_dict()}

    # -- native git operations (fr_developer_e778b9bf) --
    #
    # Each handler takes an explicit `cwd` since developer can operate
    # on multiple project workspaces. Destructive flags default to safe
    # values and must be explicitly opted into by callers.

    async def _safe_report_gap(self, operation: str, reason: str) -> None:
        """Report a gap, swallowing 'not connected' errors.

        Git handlers return {'error': ...} on every GitClientError path, so
        callers always see the failure. The report_gap is audit signal on
        top of that — if the bus isn't connected (tests, standalone runs)
        we still want the handler to return its error dict cleanly.
        """
        try:
            await self.report_gap(operation, reason)
        except RuntimeError:
            # Not connected to bus — handler still returns the error dict
            pass

    @handler("git_status")
    async def handle_git_status(self, args):
        from developer.git_client import GitClient, GitClientError
        cwd = args.get("cwd", "")
        if not cwd:
            return {"error": "cwd is required"}
        try:
            s = GitClient(cwd).status()
        except GitClientError as e:
            await self._safe_report_gap("git_status", str(e))
            return {"error": str(e)}
        return {
            "branch": s.branch,
            "is_dirty": s.is_dirty,
            "untracked": s.untracked,
            "modified": s.modified,
            "staged": s.staged,
            "deleted": s.deleted,
            "ahead": s.ahead,
            "behind": s.behind,
            "detached": s.detached,
        }

    @handler("pr_ready")
    async def handle_pr_ready(self, args):
        from developer.github_client import GithubClient, GithubClientError

        repo = args.get("repo", "")
        try:
            pr_number = int(args.get("pr_number", 0))
        except (TypeError, ValueError):
            return {"error": "pr_number must be an integer"}
        if not repo or pr_number < 1:
            return {"error": "repo and pr_number are required"}

        try:
            readiness = await GithubClient().pr_readiness(repo, pr_number)
        except GithubClientError as e:
            await self._safe_report_gap("pr_ready", str(e))
            return {"error": str(e)}

        return {
            "state": readiness.state,
            "recommended_action": readiness.recommended_action,
            "copilot_verdict": readiness.copilot_verdict,
            "latest_copilot_comment": readiness.latest_copilot_comment,
            "actionable_comments": readiness.actionable_comments,
            "review_decision": readiness.review_decision,
            "merge_state": readiness.merge_state,
            "head_ref": readiness.head_ref,
            "head_sha": readiness.head_sha,
            "url": readiness.url,
        }

    async def _optional_pr_ready(self, args):
        repo = args.get("repo", "")
        try:
            pr_number = int(args.get("pr_number", 0) or 0)
        except (TypeError, ValueError):
            return {"error": "pr_number must be an integer"}
        if not repo or pr_number < 1:
            return None
        return await self.handle_pr_ready({"repo": repo, "pr_number": pr_number})

    @handler("git_log")
    async def handle_git_log(self, args):
        from developer.git_client import GitClient, GitClientError
        cwd = args.get("cwd", "")
        if not cwd:
            return {"error": "cwd is required"}
        # Defensive int parse — callers that come in over the bus may
        # pass limit as a string; ``int(...)`` would ValueError and
        # escape our GitClientError catch.
        try:
            limit = int(args.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        try:
            commits = GitClient(cwd).log(
                ref=args.get("ref", "HEAD"),
                limit=limit,
            )
        except GitClientError as e:
            await self._safe_report_gap("git_log", str(e))
            return {"error": str(e)}
        return {
            "count": len(commits),
            "commits": [
                {
                    "sha": c.sha,
                    "short_sha": c.short_sha,
                    "author": c.author,
                    "committed_at": c.committed_at,
                    "message": c.message,
                }
                for c in commits
            ],
        }

    @handler("git_diff")
    async def handle_git_diff(self, args):
        from developer.git_client import GitClient, GitClientError
        cwd = args.get("cwd", "")
        if not cwd:
            return {"error": "cwd is required"}
        ref_b = args.get("ref_b") or None
        try:
            diff = GitClient(cwd).diff(
                ref_a=args.get("ref_a", "HEAD"),
                ref_b=ref_b,
            )
        except GitClientError as e:
            await self._safe_report_gap("git_diff", str(e))
            return {"error": str(e)}
        return {"diff": diff}

    @handler("git_branches")
    async def handle_git_branches(self, args):
        from developer.git_client import GitClient, GitClientError
        cwd = args.get("cwd", "")
        if not cwd:
            return {"error": "cwd is required"}
        try:
            branches = GitClient(cwd).list_branches(
                local=bool(args.get("local", True)),
                remote=bool(args.get("remote", False)),
            )
        except GitClientError as e:
            await self._safe_report_gap("git_branches", str(e))
            return {"error": str(e)}
        return {
            "count": len(branches),
            "branches": [
                {"name": b.name, "is_remote": b.is_remote, "head_sha": b.head_sha,
                 "is_current": b.is_current}
                for b in branches
            ],
        }

    @handler("git_commit")
    async def handle_git_commit(self, args):
        from developer.git_client import GitClient, GitClientError
        cwd = args.get("cwd", "")
        message = args.get("message", "")
        if not cwd or not message:
            return {"error": "cwd and message are required"}
        co_raw = args.get("co_authors", "")
        if isinstance(co_raw, list):
            co_authors = [str(c).strip() for c in co_raw if str(c).strip()]
        else:
            co_authors = [c.strip() for c in str(co_raw).split(",") if c.strip()]
        try:
            commit = GitClient(cwd).commit(
                message=message,
                co_authors=co_authors or None,
            )
        except GitClientError as e:
            await self._safe_report_gap("git_commit", str(e))
            return {"error": str(e)}
        return {
            "sha": commit.sha,
            "short_sha": commit.short_sha,
            "message": commit.message,
        }

    @handler("git_stage")
    async def handle_git_stage(self, args):
        from developer.git_client import GitClient, GitClientError
        cwd = args.get("cwd", "")
        paths = _parse_paths(args.get("paths", []))
        if not cwd or not paths:
            return {"error": "cwd and paths are required"}
        try:
            staged = GitClient(cwd).stage(paths)
        except GitClientError as e:
            await self._safe_report_gap("git_stage", str(e))
            return {"error": str(e)}
        return {"staged": staged}

    @handler("git_unstage")
    async def handle_git_unstage(self, args):
        from developer.git_client import GitClient, GitClientError
        cwd = args.get("cwd", "")
        paths = _parse_paths(args.get("paths", []))
        if not cwd or not paths:
            return {"error": "cwd and paths are required"}
        try:
            unstaged = GitClient(cwd).unstage(paths)
        except GitClientError as e:
            await self._safe_report_gap("git_unstage", str(e))
            return {"error": str(e)}
        return {"unstaged": unstaged}

    @handler("git_checkout")
    async def handle_git_checkout(self, args):
        from developer.git_client import GitClient, GitClientError
        cwd = args.get("cwd", "")
        ref = args.get("ref", "")
        if not cwd or not ref:
            return {"error": "cwd and ref are required"}
        try:
            branch = GitClient(cwd).checkout(
                ref,
                new_branch=bool(args.get("new_branch", False)),
            )
        except GitClientError as e:
            await self._safe_report_gap("git_checkout", str(e))
            return {"error": str(e)}
        return {"branch": branch}

    @handler("git_create_branch")
    async def handle_git_create_branch(self, args):
        from developer.git_client import GitClient, GitClientError
        cwd = args.get("cwd", "")
        name = args.get("name", "")
        if not cwd or not name:
            return {"error": "cwd and name are required"}
        try:
            branch = GitClient(cwd).create_branch(
                name,
                base=args.get("base") or None,
            )
        except GitClientError as e:
            await self._safe_report_gap("git_create_branch", str(e))
            return {"error": str(e)}
        return {"branch": branch}

    @handler("git_delete_branch")
    async def handle_git_delete_branch(self, args):
        from developer.git_client import GitClient, GitClientError
        cwd = args.get("cwd", "")
        name = args.get("name", "")
        if not cwd or not name:
            return {"error": "cwd and name are required"}
        try:
            branch = GitClient(cwd).delete_branch(
                name,
                force=bool(args.get("force", False)),
            )
        except GitClientError as e:
            await self._safe_report_gap("git_delete_branch", str(e))
            return {"error": str(e)}
        return {"deleted": branch, "force": bool(args.get("force", False))}

    @handler("git_fetch")
    async def handle_git_fetch(self, args):
        from developer.git_client import GitClient, GitClientError
        cwd = args.get("cwd", "")
        if not cwd:
            return {"error": "cwd is required"}
        try:
            remote = GitClient(cwd).fetch(remote=args.get("remote") or "origin")
        except GitClientError as e:
            await self._safe_report_gap("git_fetch", str(e))
            return {"error": str(e)}
        return {"remote": remote}

    @handler("git_pull")
    async def handle_git_pull(self, args):
        from developer.git_client import GitClient, GitClientError
        cwd = args.get("cwd", "")
        if not cwd:
            return {"error": "cwd is required"}
        try:
            branch = GitClient(cwd).pull(
                remote=args.get("remote") or None,
                branch=args.get("branch") or None,
                ff_only=bool(args.get("ff_only", True)),
            )
        except GitClientError as e:
            await self._safe_report_gap("git_pull", str(e))
            return {"error": str(e)}
        return {"branch": branch}

    @handler("git_push")
    async def handle_git_push(self, args):
        from developer.git_client import GitClient, GitClientError
        cwd = args.get("cwd", "")
        if not cwd:
            return {"error": "cwd is required"}
        try:
            client = GitClient(cwd)
            branch = args.get("branch") or None
            if branch is None and client.current_branch() == "HEAD":
                return {"error": "cannot push detached HEAD without an explicit branch"}
            result = client.push(
                remote=args.get("remote") or "origin",
                branch=branch,
                force=bool(args.get("force", False)),
                set_upstream=bool(args.get("set_upstream", False)),
            )
        except GitClientError as e:
            await self._safe_report_gap("git_push", str(e))
            return {"error": str(e)}
        return result

    @handler("git_show")
    async def handle_git_show(self, args):
        from developer.git_client import GitClient, GitClientError
        cwd = args.get("cwd", "")
        ref = args.get("ref", "")
        if not cwd or not ref:
            return {"error": "cwd and ref are required"}
        try:
            commit = GitClient(cwd).show(ref)
        except GitClientError as e:
            await self._safe_report_gap("git_show", str(e))
            return {"error": str(e)}
        return {
            "sha": commit.sha,
            "short_sha": commit.short_sha,
            "author": commit.author,
            "committed_at": commit.committed_at,
            "message": commit.message,
            "full_message": commit.full_message,
        }

    @handler("git_rev_parse")
    async def handle_git_rev_parse(self, args):
        from developer.git_client import GitClient, GitClientError
        cwd = args.get("cwd", "")
        ref = args.get("ref", "")
        if not cwd or not ref:
            return {"error": "cwd and ref are required"}
        try:
            sha = GitClient(cwd).rev_parse(ref)
        except GitClientError as e:
            await self._safe_report_gap("git_rev_parse", str(e))
            return {"error": str(e)}
        return {"ref": ref, "sha": sha}

    # -- test run + distill --

    @handler("run_tests")
    async def handle_run_tests(self, args):
        """Run pytest in a configured project and return a distilled digest.

        Pay the test-run cost locally, serve a compact digest to Claude.
        ``detail=full`` adds per-failure trace excerpts (5–10 lines each)
        on top of ``brief``; raw pytest output is only inlined as a fallback
        when parsing fails entirely.
        """
        from developer import tests_runner

        project = args.get("project", "")
        if not project:
            return {"error": "project is required (must be a configured project name)"}

        config = self.pipeline.config
        if project not in config.projects:
            return {
                "error": f"unknown project {project!r}",
                "known_projects": sorted(config.projects.keys()),
            }

        target = args.get("target", "")
        detail = args.get("detail", "brief")
        try:
            timeout_s = float(args.get("timeout_s", tests_runner.DEFAULT_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            timeout_s = tests_runner.DEFAULT_TIMEOUT_SECONDS

        cwd = config.projects[project].repo
        try:
            result = await tests_runner.run_pytest(
                cwd=cwd, target=target, timeout_s=timeout_s,
            )
        except Exception as e:
            await self.report_gap("run_tests", f"pytest launch failed in {cwd}: {e}")
            raise

        return {
            "project": project,
            "cwd": str(cwd),
            "returncode": result.returncode,
            "elapsed_s": round(result.elapsed_s, 3),
            "passed": result.passed,
            "failed": result.failed,
            "errors": result.errors,
            "skipped": result.skipped,
            "timed_out": result.timed_out,
            "parsed": result.parsed,
            "digest": tests_runner.format_response(result, detail=detail),
        }


def _parse_paths(value) -> list[str]:
    """Parse list or comma-separated path input from bus args."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(p).strip() for p in value if str(p).strip()]
    return [p.strip() for p in str(value).split(",") if p.strip()]


def _parse_json_dict(value) -> dict:
    """Parse an optional JSON object arg from bus args."""
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            return {"error": f"JSON object parse failed: {e}"}
        if not isinstance(parsed, dict):
            return {"error": "value must decode to a JSON object"}
        return parsed
    return {"error": "value must be a JSON object or JSON string"}


def _int_arg(args: dict, name: str, default: int = 0) -> int:
    try:
        return int(args.get(name, default) or default)
    except (TypeError, ValueError):
        return default


def _float_arg(args: dict, name: str, default: float = 0.0) -> float:
    try:
        return float(args.get(name, default) or default)
    except (TypeError, ValueError):
        return default


def _bool_arg(args: dict, name: str, default: bool = False) -> bool:
    raw = args.get(name, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(raw)


def _work_units_from_local_frs(frs, *, max_size: int = 5) -> list[dict]:
    """Build deterministic work units from developer-owned FR records."""
    max_size = max(1, int(max_size))
    groups: dict[tuple[str, str], list] = {}
    for fr in frs:
        concept = (fr.concept or "general").strip().lower()
        groups.setdefault((fr.target, concept), []).append(fr)

    units = []
    for target, concept in sorted(groups):
        group = sorted(groups[(target, concept)], key=lambda fr: fr.id)
        title_concept = concept if concept != "general" else "general"
        for chunk_index, chunk_start in enumerate(range(0, len(group), max_size), start=1):
            chunk = group[chunk_start:chunk_start + max_size]
            priorities = [fr.priority for fr in chunk]
            max_priority = _max_priority(priorities)
            suffix = "" if len(group) <= max_size else f", slice {chunk_index}"
            units.append({
                "name": (
                    f"Developer FR work unit ({len(chunk)} FRs, target: {target}, "
                    f"concept: {title_concept}{suffix})"
                ),
                "rank": 0,
                "size": len(chunk),
                "targets": [target],
                "concept": title_concept,
                "max_priority": max_priority,
                "source": "developer_local",
                "frs": [
                    {
                        "fr_id": fr.id,
                        "description": f"{fr.title} → {fr.target} [{fr.priority}]",
                        "priority": fr.priority,
                        "status": fr.status,
                        "target": fr.target,
                    }
                    for fr in chunk
                ],
            })

    priority_order = {"high": 0, "medium": 1, "low": 2}
    units.sort(key=lambda u: (
        priority_order.get(u["max_priority"], 99),
        -int(u["size"]),
        u["targets"][0],
        u["concept"],
    ))
    for idx, unit in enumerate(units, start=1):
        unit["rank"] = idx
    return units


def _positive_int_arg(args: dict, name: str, *, default: int) -> int | dict:
    raw = args.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return {"error": f"{name} must be a positive integer, got {raw!r}"}
    if value < 1:
        return {"error": f"{name} must be a positive integer, got {raw!r}"}
    return value


def _max_priority(priorities: list[str]) -> str:
    order = {"high": 3, "medium": 2, "low": 1}
    best = "low"
    for priority in priorities:
        if order.get(priority, 0) > order.get(best, 0):
            best = priority
    return best


def _handoff_next_actions(
    milestone_id: str,
    recommendation: str,
    *,
    review_terms: list[str],
) -> list[str]:
    review_action = f"review_milestone_scope milestone_id={milestone_id}"
    if review_terms:
        review_action += f" review_terms={','.join(review_terms)}"
    actions = [
        f"draft_spec_from_milestone milestone_id={milestone_id}",
        review_action,
    ]
    if recommendation == "ready_for_spec":
        actions.append("create implementation branch and start the scoped milestone")
    else:
        actions.append("refine or split the milestone before implementation")
    return actions


def _parse_work_unit_arg(value) -> dict:
    """Parse an optional work_unit arg from bus JSON/string input."""
    parsed = _parse_json_dict(value)
    if "error" in parsed:
        raise ValueError(parsed["error"])
    return parsed


def _compact_jsonish(value, *, limit: int = 1200) -> str:
    """Bound a JSON-ish value for inline checkpoint evidence."""
    try:
        text = json.dumps(value, sort_keys=True)
    except TypeError:
        text = str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _parse_concept_bundles(text: str) -> list[dict]:
    """Parse researcher.synergize_concepts brief text into bundle dicts."""
    bundles = []
    current = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        strength_match = re.match(r"^(?P<title>.+?)\s+\(strength:\s*(?P<strength>\d+)%\)$", line)
        if strength_match:
            if current:
                bundles.append(current)
            current = {
                "title": strength_match.group("title").strip(),
                "strength": int(strength_match.group("strength")),
                "concepts": [],
                "summary": "",
            }
            continue
        if current is None:
            continue
        if line.lower().startswith("concepts:"):
            concepts = line.split(":", 1)[1]
            current["concepts"] = [c.strip() for c in concepts.split(",") if c.strip()]
        elif not current["summary"]:
            current["summary"] = line
        else:
            current["summary"] = f"{current['summary']} {line}".strip()
    if current:
        bundles.append(current)
    return bundles


def _candidate_from_concept_bundle(bundle: dict, *, target: str, existing: list) -> dict:
    concepts = list(bundle.get("concepts") or [])
    title = f"Apply {bundle.get('title', 'concept bundle')} to {target} workflow"
    summary = bundle.get("summary") or "Use this concept bundle to improve developer workflow."
    strength = int(bundle.get("strength") or 0)
    priority = "high" if strength >= 80 else "medium" if strength >= 50 else "low"
    concept = ", ".join(concepts) or str(bundle.get("title", "")).strip()
    description = (
        f"{summary}\n\n"
        f"Concepts: {concept or 'unspecified'}.\n"
        "Acceptance criteria: developer can review this as a concrete FR, "
        "compare it against existing local FRs, and promote it without relying "
        "on researcher to own FR creation."
    )
    matches = _existing_fr_matches(title=title, concepts=concepts, existing=existing)
    return {
        "title": title,
        "description": description,
        "priority": priority,
        "target": target,
        "concept": concept,
        "classification": "app",
        "source_bundle": bundle,
        "status": "existing_match" if matches else "new_candidate",
        "existing_matches": matches,
    }


def _existing_fr_matches(*, title: str, concepts: list[str], existing: list) -> list[dict]:
    title_tokens = _match_tokens(title)
    concept_tokens = set()
    for concept in concepts:
        concept_tokens.update(_match_tokens(concept))

    matches = []
    for fr in existing:
        haystack = " ".join([
            getattr(fr, "title", ""),
            getattr(fr, "description", ""),
            getattr(fr, "concept", ""),
        ])
        tokens = _match_tokens(haystack)
        shared_title = title_tokens & tokens
        shared_concepts = concept_tokens & tokens
        if len(shared_title) >= 3 or shared_concepts:
            matches.append({
                "fr_id": fr.id,
                "title": fr.title,
                "status": fr.status,
                "priority": fr.priority,
                "shared_terms": sorted(shared_title | shared_concepts),
            })
    return matches


def _match_tokens(text: str) -> set[str]:
    stop = {
        "the", "and", "for", "from", "into", "with", "this", "that",
        "apply", "developer", "workflow",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(token) > 2 and token not in stop
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    import argparse

    from khonliang_bus import add_version_flag

    parser = argparse.ArgumentParser(
        prog="developer.agent",
        description="khonliang-developer bus agent",
    )
    add_version_flag(parser)
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

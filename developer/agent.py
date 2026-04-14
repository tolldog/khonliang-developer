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
            Skill("get_fr", "Look up an FR via researcher on the bus",
                  {"fr_id": {"type": "string", "required": True}},
                  since="0.2.0"),
            Skill("list_frs", "List FRs for a target via researcher on the bus",
                  {"target": {"type": "string", "required": True}},
                  since="0.2.0"),
            Skill("get_paper_context", "Get research evidence for a query via researcher",
                  {"query": {"type": "string", "required": True}},
                  since="0.2.0"),
            # Clustered FR work planning
            Skill("next_work_unit", "Get the highest-ranked FR cluster as a work unit",
                  {"target": {"type": "string", "default": ""},
                   "threshold": {"type": "number", "default": 0.85}},
                  since="0.2.0"),
            Skill("work_units", "List all FR clusters ranked by importance",
                  {"target": {"type": "string", "default": ""},
                   "threshold": {"type": "number", "default": 0.85}},
                  since="0.2.0"),
            # Test run + distill (token-arbitrage: pay local pytest cost once,
            # serve cheap digest to Claude instead of raw pytest output)
            Skill("run_tests", "Run project pytest suite and return a distilled digest",
                  {"project": {"type": "string", "required": True},
                   "target": {"type": "string", "default": ""},
                   "detail": {"type": "string", "default": "brief"},
                   "timeout_s": {"type": "number", "default": 300}},
                  since="0.3.0"),
            # Developer-owned FR lifecycle (Bundle A PR 1). These write to
            # developer.db; legacy get_fr/list_frs still proxy to researcher
            # for pre-migration records until fr_developer_0ab2aa9b lands.
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
        return (result and result.get("result")) or {"error": "no result from researcher"}

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
        return (result and result.get("result")) or {"error": "no result from researcher"}

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

    # -- clustered FR work planning --

    @handler("work_units")
    async def handle_work_units(self, args):
        """Pull FR clusters from researcher, rank by aggregate importance."""
        target = args.get("target", "")
        threshold = args.get("threshold", 0.85)

        # Get clusters from researcher via bus
        try:
            cluster_result = await self.request(
                agent_type="researcher",
                operation="cluster_frs",
                args={"target": target, "threshold": threshold, "detail": "full"},
            )
        except Exception as e:
            await self.report_gap("work_units", f"Failed to get clusters: {e}")
            raise

        cluster_text = ""
        if cluster_result and cluster_result.get("result"):
            r = cluster_result["result"]
            cluster_text = r.get("result", "") if isinstance(r, dict) else str(r)

        # Parse clusters — if parsing yields nothing, fall back to flat list.
        # This handles: empty text, "No clusters" message, or a format
        # change that doesn't produce any parseable cluster headers.
        work_units = self._parse_and_rank_clusters(cluster_text, target) if cluster_text else []

        if not work_units:
            try:
                fr_result = await self.request(
                    agent_type="researcher",
                    operation="feature_requests",
                    args={"target": target, "detail": "full"},
                )
            except Exception:
                return {"work_units": [], "source": "none", "error": "no clusters and no FRs available"}

            return {
                "work_units": [{"type": "flat", "description": "No clusters found — flat FR list",
                                "frs": (fr_result and fr_result.get("result")) or {}}],
                "source": "flat_list",
            }

        return {
            "work_units": work_units,
            "source": "clusters",
            "threshold": threshold,
            "count": len(work_units),
        }

    @handler("next_work_unit")
    async def handle_next_work_unit(self, args):
        """Get the single highest-ranked work unit."""
        result = await self.handle_work_units(args)
        units = result.get("work_units", [])
        if not units:
            return {"error": "no work units available"}
        return {
            "work_unit": units[0],
            "remaining": len(units) - 1,
            "source": result.get("source", "unknown"),
        }

    def _parse_and_rank_clusters(self, cluster_text: str, target: str) -> list[dict]:
        """Parse cluster_frs output and rank by aggregate importance.

        Ranking signals (highest to lowest weight):
          1. Highest priority in the cluster
          2. Cluster size (more FRs = more evidence of need)
          3. Number of targets touched (cross-cutting = higher value)
          4. Whether any FRs mention the requested target
        """
        PRIORITY_SCORE = {"high": 3, "medium": 2, "low": 1}
        clusters = []
        current_cluster = None

        for line in cluster_text.splitlines():
            line = line.strip()
            if not line:
                continue

            # Detect cluster headers like "## Cluster 1 (4 FRs, targets: khonliang,developer)"
            # Skip summary lines like "# FR Clusters (3 clusters, 10 FRs)"
            if "Cluster" in line and "FRs" in line and "clusters" not in line.lower():
                if current_cluster:
                    clusters.append(current_cluster)
                # Extract size and targets from header
                # Format: "## Cluster 1 (4 FRs, targets: khonliang,developer)"
                size = 0
                targets = set()
                if "(" in line:
                    meta = line.split("(", 1)[1].rstrip(")")
                    # Size is before "FRs"
                    size_match = re.search(r"(\d+)\s*FRs?", meta)
                    if size_match:
                        size = int(size_match.group(1))
                    # Targets are after "targets:"
                    if "targets:" in meta:
                        targets_str = meta.split("targets:")[1].strip()
                        targets = {t.strip() for t in targets_str.split(",") if t.strip()}
                current_cluster = {
                    "name": line.lstrip("#").strip(),
                    "size": size,
                    "targets": sorted(targets),
                    "frs": [],
                    "max_priority": "low",
                }
                continue

            # Detect FR lines like "  [fr_xxx] Title → target [priority]"
            if current_cluster and line.startswith("[fr_"):
                fr_id = line.split("]")[0].lstrip("[")
                rest = line.split("]", 1)[1].strip() if "]" in line else ""
                priority = "medium"
                for p in ("high", "medium", "low"):
                    if f"[{p}]" in rest:
                        priority = p
                        break
                current_cluster["frs"].append({
                    "fr_id": fr_id,
                    "description": rest,
                    "priority": priority,
                })
                if PRIORITY_SCORE.get(priority, 0) > PRIORITY_SCORE.get(current_cluster["max_priority"], 0):
                    current_cluster["max_priority"] = priority

        if current_cluster:
            clusters.append(current_cluster)

        # Rank clusters
        def rank_key(c):
            return (
                PRIORITY_SCORE.get(c["max_priority"], 0),  # highest priority first
                c["size"],                                   # larger clusters first
                len(c["targets"]),                           # cross-cutting first
                1 if target and target in c["targets"] else 0,  # matching target first
            )

        clusters.sort(key=rank_key, reverse=True)

        # Add rank
        for i, c in enumerate(clusters, 1):
            c["rank"] = i

        return clusters

    # -- developer-owned FR lifecycle (Bundle A PR 1) --
    #
    # These write to developer.db. The existing get_fr / list_frs handlers
    # (which proxy to researcher.knowledge_search / researcher.feature_requests)
    # stay in place for backward compat with pre-migration records. After
    # the data migration (fr_developer_0ab2aa9b), those proxies get updated
    # to read from developer's store first, falling back to researcher.

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

    # -- native git operations (fr_developer_e778b9bf) --
    #
    # Each handler takes an explicit `cwd` since developer can operate
    # on multiple project workspaces. Destructive flags (force, amend)
    # are deliberately NOT surfaced via these initial skills — if a
    # caller needs them they call the Python API directly; the bus skill
    # surface stays safe by default.

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

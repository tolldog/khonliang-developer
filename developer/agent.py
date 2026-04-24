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
import time
from pathlib import Path
from typing import Any, Optional

from khonliang_bus import BaseAgent, Skill, Collaboration, handler

from developer import integration_scan
from developer.project_store import ProjectDuplicateError


# Forward reference — `_parse_json_dict` is defined further down in this
# module (lifecycle skills land ahead of it in source order). Imported via
# late name-lookup at call time, so the forward reference is fine.


def _parse_repos_arg(raw):
    """Accept comma-list, JSON list, or already-materialized list.

    Strict on JSON-shaped input: any string whose first non-space char is
    ``[`` or ``{`` is parsed as JSON. If decoding fails OR the decoded
    value isn't a list, raises :class:`ValueError`. Silently CSV-splitting
    a JSON object like ``{"path":"/x"}`` would persist a garbage path.
    """

    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        # Any JSON-shaped string is parsed strictly; both '[...]' and
        # '{...}' must decode to a list or we raise. Prevents a JSON
        # object from silently falling through to CSV and producing a
        # single-element list with the raw object string as a path.
        if s.startswith("[") or s.startswith("{"):
            try:
                parsed = json.loads(s)
            except json.JSONDecodeError as e:
                raise ValueError(f"repos is not valid JSON: {e}") from e
            if not isinstance(parsed, list):
                raise ValueError(
                    f"repos JSON must be a list (got {type(parsed).__name__})"
                )
            return parsed
        # Comma-separated path list fallback (non-JSON-shaped string).
        return [piece.strip() for piece in s.split(",") if piece.strip()]
    # Explicitly reject a dict — ProjectStore.create iterates its arg, and
    # iterating a dict yields the keys as "paths", persisting garbage.
    if isinstance(raw, dict):
        raise ValueError(
            "repos must be a list of paths / dicts, not a bare dict "
            "(pass a JSON list or wrap in [...])"
        )
    # Unknown shape — delegate rejection to ProjectStore.create() which
    # raises TypeError for non-str/dict/RepoRef entries.
    return raw


# (Removed duplicate `_parse_json_object_arg` — use `_parse_json_dict`
# defined later in this file. That helper returns either a parsed dict
# or a one-key {"error": "..."} dict; handlers translate the error-shape
# into the structured {error: ...} response path. One JSON-object-parse
# convention for the whole module.)

logger = logging.getLogger(__name__)


class DeveloperAgent(BaseAgent):
    agent_type = "developer"
    module_name = "developer.agent"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._pipeline = None  # lazy init
        # Lazy-initialized PR watcher registry (see pr_watcher.py). Kept
        # lazy so agents that never call watch_pr_fleet don't pay the
        # SQLite-schema-init cost, and so tests that replace the
        # registry factory can do so before the first skill call.
        self._pr_watcher_registry = None
        # Version is set by BaseAgent.__init__ via khonliang_bus.resolve_version
        # (pyproject walk → importlib.metadata → default). We used to duplicate
        # the metadata lookup here with a hardcoded "0.1.0" fallback, but that
        # both shadowed BaseAgent's fresher on-disk read AND drifted silently
        # when pyproject was bumped without touching this string.

    @property
    def pipeline(self):
        if self._pipeline is None:
            from developer.config import Config
            from developer.pipeline import Pipeline
            config = Config.load(self.config_path)
            self._pipeline = Pipeline.from_config(config)
        return self._pipeline

    @property
    def pr_watcher_registry(self):
        """Lazily construct the PR watcher registry.

        Tied to the pipeline's ``db_path`` so the watcher tables live
        alongside FRs / milestones in developer.db. Publish is bound to
        :meth:`BaseAgent.publish`; when the agent isn't bus-connected
        (tests, dry runs) publish raises ``RuntimeError`` and the
        watcher swallows it via its internal try/except — a real run
        with a real bus is where events actually flow.
        """
        if self._pr_watcher_registry is None:
            from developer.pr_watcher import PRWatcherRegistry, PRWatcherStore
            store = PRWatcherStore(str(self.pipeline.config.db_path))
            self._pr_watcher_registry = PRWatcherRegistry(
                store=store, publish=self.publish,
            )
        return self._pr_watcher_registry

    async def start(self):
        """Rehydrate persisted PR watchers, then run the normal agent loop.

        :meth:`BaseAgent.start` connects to the bus and blocks on the
        message loop, so rehydration runs as a background task that
        waits for the connector to register before spawning watchers —
        ``publish`` needs a live connector or the watcher's first
        event fails. If rehydration itself raises we log and continue;
        a failed rehydrate must not prevent the agent from starting.
        """
        if self._pr_watcher_registry is None:
            # Touch the property so the registry exists in memory before
            # the background task tries to use it. Safe to do here:
            # pipeline (and DB path) is already resolvable by this point
            # in the normal launch path.
            try:
                _ = self.pr_watcher_registry
            except Exception as e:
                logger.warning(
                    "developer agent: pr watcher registry init failed; "
                    "rehydration skipped: %s", e,
                )
                return await super().start()
        rehydrate_task = asyncio.create_task(
            self._rehydrate_pr_watchers_when_ready(),
            name="pr_watcher_rehydrate",
        )
        try:
            await super().start()
        finally:
            # Ensure the rehydrate task never outlives the agent.
            if not rehydrate_task.done():
                rehydrate_task.cancel()
                try:
                    await rehydrate_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _rehydrate_pr_watchers_when_ready(self) -> None:
        """Wait until the bus connector is registered, then rehydrate.

        Split out so :meth:`start` stays a thin wrapper. The poll-until-
        connected loop avoids racing registration — a watcher that
        publishes before ``_connector.connected`` would hit the
        ``RuntimeError("Cannot publish ...")`` branch in BaseAgent.
        Backoff is bounded: 15s of waiting is plenty for a healthy
        bus; past that we assume a broken bus and skip rehydration
        (the agent's heartbeat/reconnect path will surface the
        breakage separately).
        """
        deadline = 15.0
        waited = 0.0
        step = 0.1
        while waited < deadline:
            connector = getattr(self, "_connector", None)
            if connector is not None and getattr(connector, "connected", False):
                break
            await asyncio.sleep(step)
            waited += step
        else:
            logger.warning(
                "developer agent: connector not ready within %.1fs; "
                "skipping pr watcher rehydration", deadline,
            )
            return
        try:
            spawned = await self._pr_watcher_registry.rehydrate()
        except Exception as e:
            logger.warning("developer agent: pr watcher rehydrate failed: %s", e)
            return
        if spawned:
            logger.info(
                "developer agent: rehydrated %d pr watcher(s): %s",
                len(spawned), ", ".join(spawned),
            )

    async def shutdown(self):
        """Cancel live PR watchers before disconnecting from the bus.

        Overrides :meth:`BaseAgent.shutdown` so watchers get a clean
        stop path on SIGTERM/SIGINT. Persistent state (registry + dedupe
        rows) is left intact, so a restart inherits the watchers via
        the DB rather than losing state on every signal.
        """
        if self._pr_watcher_registry is not None:
            try:
                # Use a dedicated in-memory cancel so we don't DELETE the
                # DB rows — we want the watchers to come back after
                # restart. Iterate a snapshot of live handles and cancel
                # their tasks without removing from the store.
                live_handles = list(self._pr_watcher_registry._watchers.values())
                for live in live_handles:
                    live.stop_event.set()
                # Collect any tasks we end up cancelling so we can await
                # them together at the end — missing the await was the
                # previous bug (tasks logged "Task was destroyed but it
                # is pending" during shutdown).
                cancelled: list[asyncio.Task] = []
                for live in live_handles:
                    try:
                        await asyncio.wait_for(live.task, timeout=3.0)
                    except asyncio.TimeoutError:
                        live.task.cancel()
                        cancelled.append(live.task)
                    except asyncio.CancelledError:
                        # Task already cancelled elsewhere — nothing to
                        # do; it's already done.
                        pass
                    except Exception:
                        # Task raised a non-cancellation exception while
                        # running; it's finished, no await needed.
                        pass
                if cancelled:
                    await asyncio.gather(*cancelled, return_exceptions=True)
            except Exception as e:
                logger.warning("developer agent: pr watcher shutdown failed: %s", e)
        await super().shutdown()

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
            # Project lifecycle (fr_developer_5d0a8711 Phase 2). Thin
            # wrappers over ProjectStore; cross-store migration (adding
            # `project` dimension to FR / milestone / spec / bug / dogfood)
            # lands in Phase 3.
            Skill("project_init",
                  "Register a new project: name, repos, optional domain + config.",
                  {"slug": {"type": "string", "required": True,
                            "description": "unique project slug (1-64 chars; lowercase a-z0-9_-; must start with a letter or digit)"},
                   "repos": {"type": "string", "required": True,
                             "description": "comma-separated repo paths, or JSON list of {path, role, install_name} dicts"},
                   "name": {"type": "string", "default": ""},
                   "domain": {"type": "string", "default": "generic"},
                   "config": {"type": "string", "default": "",
                              "description": "optional JSON object with project-scoped config overrides"}},
                  since="0.19.0"),
            Skill("list_projects",
                  "List registered projects. Filters retired by default.",
                  {"include_retired": {"type": "boolean", "default": False},
                   "detail": {"type": "string", "default": "brief",
                              "description": "compact / brief / full"}},
                  since="0.19.0"),
            Skill("get_project",
                  "Look up a project by slug. Returns null when missing.",
                  {"slug": {"type": "string", "required": True}},
                  since="0.19.0"),
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
            # Symmetric partner to fr_candidates_from_concepts — turns a
            # merged feature into ranked ecosystem-adoption proposals.
            # MVP covers 4 of 5 scan sources; source-code grep across
            # dev repos is deferred until a `dev_repos` registry lands
            # (fr_developer_82fe7309).
            Skill("suggest_integration_points",
                  "Scan the ecosystem for adoption sites of a merged feature "
                  "(FR / PR / skill). Returns ranked candidates by signal.",
                  {"source": {"type": "object", "required": True,
                              "description": "{'kind':'fr'|'pr'|'skill','id':str}"},
                   "detail": {"type": "string", "default": "brief",
                              "description": "brief / compact / full"},
                   "audience": {"type": "string", "default": "",
                                "description": "optional filter, e.g. 'builder'"},
                   "top_n": {"type": "integer", "default": 20,
                             "description": "max candidates returned in brief"}},
                  since="0.17.0"),
            Skill("distill_integration_points",
                  "Re-project a prior suggest_integration_points scan artifact "
                  "without rescanning. Filter by signal / top_n / detail.",
                  {"scan_id": {"type": "string", "required": True},
                   "top_n": {"type": "integer", "default": 20},
                   "signal": {"type": "string", "default": "",
                              "description": "optional signal filter"},
                   "detail": {"type": "string", "default": "brief"}},
                  since="0.17.0"),
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
            # Milestone lifecycle mutations (fr_developer_91a5a072).
            Skill("update_milestone_status",
                  "Advance a milestone's lifecycle status "
                  "(proposed/planned/in_progress/completed/abandoned). "
                  "'archived' is a legacy on-disk synonym for 'abandoned' — "
                  "still accepted for older records but new work should use 'abandoned'. "
                  "Use supersede_milestone for the 'superseded' transition "
                  "(requires a superseded_by pointer that this skill cannot supply).",
                  {"milestone_id": {"type": "string", "required": True},
                   "status": {"type": "string", "required": True},
                   "notes": {"type": "string", "default": ""},
                   "force": {"type": "boolean", "default": False,
                             "description": "allow any transition not in the forward graph "
                             "(including backward edges between active states and rollback "
                             "from terminal states)"}},
                  since="0.16.0"),
            Skill("supersede_milestone",
                  "Mark one milestone as superseded by another. Does not cascade to FRs.",
                  {"superseded_id": {"type": "string", "required": True},
                   "superseded_by_id": {"type": "string", "required": True},
                   "rationale": {"type": "string", "default": ""}},
                  since="0.16.0"),
            Skill("update_milestone_frs",
                  "Add or remove FRs from a proposed milestone's bundle",
                  {"milestone_id": {"type": "string", "required": True},
                   "add_fr_ids": {"type": "string", "default": "",
                                  "description": "comma-separated FR ids to add"},
                   "remove_fr_ids": {"type": "string", "default": "",
                                     "description": "comma-separated FR ids to remove"},
                   "notes": {"type": "string", "default": ""}},
                  since="0.16.0"),
            Skill("delete_milestone",
                  "Hard-delete a milestone. Refuses if any bundled FR is in_progress "
                  "or if notes_history has non-seed entries (use supersede instead).",
                  {"milestone_id": {"type": "string", "required": True},
                   "reason": {"type": "string", "default": ""}},
                  since="0.16.0"),
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
            # Long-running PR fleet watcher (fr_developer_6c8ec260). Spawns
            # a background poll loop and returns the watcher id immediately;
            # pr.* bus events fire on every transition.
            Skill("watch_pr_fleet",
                  "Start a long-running PR watcher; publishes pr.* bus events on transitions",
                  {"repos": {"type": "string", "required": True,
                             "description": "comma-separated owner/name list"},
                   "pr_numbers": {"type": "string", "default": "",
                                  "description": "optional comma-separated PR numbers; "
                                  "omitted watches all open PRs in each repo"},
                   "interval_s": {"type": "integer", "default": 60}},
                  since="0.14.0"),
            Skill("list_pr_watchers",
                  "List active PR watchers with their poll metadata",
                  {},
                  since="0.14.0"),
            Skill("stop_pr_watcher",
                  "Stop a PR watcher by id and clean its persistent state",
                  {"watcher_id": {"type": "string", "required": True}},
                  since="0.14.0"),
            # fr_developer_fafb36f1: one-shot snapshot of the fleet
            # (every active watcher's PRs in the compact shape used by
            # the pr.fleet_digest event). No polling, no subscription —
            # just a read. Distinct from list_pr_watchers in that the
            # response also projects every PR's current state, not just
            # the watcher metadata.
            Skill("pr_fleet_status",
                  "One-shot snapshot of every active PR watcher's fleet",
                  {"watcher_id": {"type": "string", "default": "",
                                  "description": "optional — scope to a single watcher"}},
                  since="0.15.0"),
            # Tracking-infrastructure (Phase 1 of fr_developer_f669bd33 +
            # fr_developer_1324440c). CRUD-only slice: triage promotion,
            # report_gap integration, and GH-issue ingest land in Phase 2.
            Skill("file_bug", "File a new bug in developer's bug store",
                  {"target": {"type": "string", "required": True},
                   "title": {"type": "string", "required": True},
                   "description": {"type": "string", "required": True},
                   "reproduction": {"type": "string", "default": ""},
                   "observed_entity": {"type": "string", "default": ""},
                   "severity": {"type": "string", "default": "medium"},
                   "reporter": {"type": "string", "default": ""}},
                  since="0.15.0"),
            Skill("list_bugs", "List bugs. Default filters terminal statuses.",
                  {"target": {"type": "string", "default": ""},
                   "severity_min": {"type": "string", "default": ""},
                   "status": {"type": "string", "default": "open,triaged,in_progress",
                              "description": "comma-separated names or 'all'"},
                   "detail": {"type": "string", "default": "brief"}},
                  since="0.15.0"),
            Skill("get_bug", "Look up a bug by id",
                  {"bug_id": {"type": "string", "required": True},
                   "detail": {"type": "string", "default": "brief"}},
                  since="0.15.0"),
            Skill("update_bug_status", "Advance a bug's lifecycle status",
                  {"bug_id": {"type": "string", "required": True},
                   "status": {"type": "string", "required": True},
                   "notes": {"type": "string", "default": ""}},
                  since="0.15.0"),
            Skill("link_bug_pr", "Record the PR URL fixing this bug",
                  {"bug_id": {"type": "string", "required": True},
                   "pr_url": {"type": "string", "required": True}},
                  since="0.15.0"),
            Skill("close_bug", "Terminal-close a bug as fixed or wontfix",
                  {"bug_id": {"type": "string", "required": True},
                   "resolution": {"type": "string", "required": True,
                                  "description": "'fixed' or 'wontfix'"}},
                  since="0.15.0"),
            Skill("log_dogfood", "Cheap local capture of a friction/UX observation. "
                  "No LLM / embedding / network calls — <100ms local write.",
                  {"observation": {"type": "string", "required": True},
                   "kind": {"type": "string", "default": "friction",
                            "description": "friction / bug / ux / docs / other"},
                   "target": {"type": "string", "default": ""},
                   "context": {"type": "string", "default": ""},
                   "reporter": {"type": "string", "default": ""}},
                  since="0.15.0"),
            Skill("list_dogfood", "List dogfood observations, newest first. "
                  "Default filters terminal statuses.",
                  {"kind": {"type": "string", "default": ""},
                   "target": {"type": "string", "default": ""},
                   "since": {"type": "string", "default": ""},
                   "status": {"type": "string", "default": "observed,triaged",
                              "description": "comma-separated names or 'all'"},
                   "limit": {"type": "integer", "default": 20},
                   "detail": {"type": "string", "default": "brief"}},
                  since="0.15.0"),
            Skill("get_dogfood", "Look up a dogfood observation by id",
                  {"dog_id": {"type": "string", "required": True},
                   "detail": {"type": "string", "default": "brief"}},
                  since="0.15.0"),
            # Tracking-infrastructure Phase 2A (fr_developer_f669bd33 +
            # fr_developer_1324440c): triage loop. Phase 2B (GH issue
            # ingest, fr_developer_47271f34) is a separate follow-up PR.
            Skill("triage_bug", "Triage a bug: optionally update severity/status "
                  "and optionally escalate to a new FR (idempotent).",
                  {"bug_id": {"type": "string", "required": True},
                   "severity": {"type": "string", "default": "",
                                "description": "optional new severity"},
                   "status": {"type": "string", "default": "",
                              "description": "optional new status (non-terminal only)"},
                   "escalate_to_fr": {"type": "boolean", "default": False,
                                      "description": "if true, create a companion FR "
                                      "via promote_fr and wire linked_frs"},
                   "notes": {"type": "string", "default": ""}},
                  since="0.16.0"),
            Skill("link_bug_fr", "Manually attach an existing FR to a bug. "
                  "Idempotent: re-linking the same pair is a no-op.",
                  {"bug_id": {"type": "string", "required": True},
                   "fr_id": {"type": "string", "required": True}},
                  since="0.16.0"),
            Skill("triage_dogfood", "Triage a dogfood observation. Promotion "
                  "preserves the original observation verbatim.",
                  {"dog_id": {"type": "string", "required": True},
                   "action": {"type": "string", "required": True,
                              "description": "'promote_to_bug' / 'promote_to_fr' / "
                              "'dismiss' / 'mark_duplicate'"},
                   "target_id": {"type": "string", "default": "",
                                 "description": "required for mark_duplicate — "
                                 "the dog_id to de-duplicate against"},
                   "notes": {"type": "string", "default": ""}},
                  since="0.16.0"),
            Skill("dogfood_triage_queue", "Ranked queue of observed-status "
                  "dogfood entries for a periodic triage session.",
                  {"limit": {"type": "integer", "default": 10},
                   "detail": {"type": "string", "default": "compact"}},
                  since="0.16.0"),
            Skill("report_gap", "Report a capability gap (bus telemetry). "
                  "Pass bug=true to also file a BugStore entry for the same "
                  "gap; returns the new bug_id alongside the telemetry ack.",
                  {"operation": {"type": "string", "required": True},
                   "reason": {"type": "string", "required": True},
                   "bug": {"type": "boolean", "default": False},
                   "severity": {"type": "string", "default": "medium",
                                "description": "severity for the filed bug (when bug=true)"},
                   "target": {"type": "string", "default": "",
                              "description": "optional bug target override; defaults "
                              "to the agent's own type"}},
                  since="0.16.0"),
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

    # ------------------------------------------------------------------
    # Project lifecycle (fr_developer_5d0a8711 Phase 2)
    # ------------------------------------------------------------------

    @handler("project_init")
    async def handle_project_init(self, args):
        """Create a new project record via ProjectStore.

        Accepts ``repos`` as either a comma-separated string of paths or a
        JSON-encoded list of dicts. ``config`` is an optional JSON object
        (string) for per-project overrides. Other fields flow straight
        through to :meth:`ProjectStore.create`.
        """

        slug = str(args.get("slug") or "").strip()
        if not slug:
            return {"error": "slug is required"}

        name = str(args.get("name") or "").strip() or None
        domain = str(args.get("domain") or "generic").strip() or "generic"

        # Argument-shape parsing may raise ValueError — route it through
        # the structured-error response path alongside downstream store
        # validation errors, so callers see one consistent error shape.
        try:
            repos = _parse_repos_arg(args.get("repos"))
            # Skill schema marks `repos` required; enforce at least one
            # repo after normalization. Empty list → {error}, consistent
            # with the docs and the project-as-1..N-repos contract.
            if not repos:
                return {"error": "repos is required (provide at least one path)"}
            config = _parse_json_dict(args.get("config"))
            # `_parse_json_dict` returns a one-key {"error": ...} dict on
            # parse failure. Translate to the handler's structured error.
            if isinstance(config, dict) and set(config.keys()) == {"error"}:
                return {"error": f"invalid argument: config: {config['error']}"}
            project = self.pipeline.projects.create(
                slug=slug,
                repos=repos,
                name=name,
                domain=domain,
                config=config or {},
            )
        except ProjectDuplicateError as e:
            return {"error": f"duplicate: {e}"}
        except (ValueError, TypeError) as e:
            return {"error": f"invalid argument: {e}"}

        return project.to_dict()

    @handler("list_projects")
    async def handle_list_projects(self, args):
        # Use `_bool_arg` for string-boolean safety: `"false"`, `"no"`,
        # `"0"`, and empty string all correctly stay falsey. Naive
        # `bool(args.get(...))` would treat any non-empty string as True.
        include_retired = _bool_arg(args, "include_retired", default=False)
        # Strip detail — consistent with other handlers (e.g.
        # suggest_integration_points). `' full '` → `'full'`.
        detail = str(args.get("detail") or "brief").strip()

        projects = self.pipeline.projects.list(include_retired=include_retired)

        if detail == "compact":
            return {
                "count": len(projects),
                "slugs": [p.slug for p in projects],
            }
        # brief + full share a base shape; full adds the repos[] array.
        rows = []
        for p in projects:
            row = {
                "slug": p.slug,
                "name": p.name or p.slug,
                "domain": p.domain,
                "status": p.status,
                "repo_count": len(p.repos),
            }
            if detail == "full":
                row["repos"] = [r.to_dict() for r in p.repos]
                row["config"] = dict(p.config)
                row["created_at"] = p.created_at
                row["updated_at"] = p.updated_at
            rows.append(row)
        return {"count": len(rows), "projects": rows}

    @handler("get_project")
    async def handle_get_project(self, args):
        slug = str(args.get("slug") or "").strip()
        if not slug:
            return {"error": "slug is required"}
        try:
            project = self.pipeline.projects.get(slug)
        except ValueError as e:
            return {"error": f"invalid slug: {e}"}
        if project is None:
            return {"project": None}
        return {"project": project.to_dict()}

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

    # -- integration-point scanning (fr_developer_82fe7309) --

    @handler("suggest_integration_points")
    async def handle_suggest_integration_points(self, args):
        """Scan the ecosystem for adoption sites of a merged feature.

        Symmetric partner to ``fr_candidates_from_concepts`` — that skill
        turns concept bundles into new-FR proposals; this one turns merged
        features into integration proposals. MVP covers 4 of 5 documented
        scan sources (FR store, agent-registered skills, bus events without
        subscribers, FR-body keyword mentions). Source-code grep across dev
        repos is deferred.
        """
        source = args.get("source")
        if not isinstance(source, dict):
            msg = f"source must be an object with 'kind' and 'id', got {type(source).__name__}"
            await self._safe_report_gap("suggest_integration_points", msg)
            return {"error": msg}

        kind = str(source.get("kind") or "").strip().lower()
        source_id = str(source.get("id") or "").strip()
        if kind not in ("fr", "pr", "skill"):
            msg = f"source.kind must be 'fr', 'pr', or 'skill', got {kind!r}"
            await self._safe_report_gap("suggest_integration_points", msg)
            return {"error": msg}
        if not source_id:
            msg = "source.id is required"
            await self._safe_report_gap("suggest_integration_points", msg)
            return {"error": msg}

        detail = str(args.get("detail") or "brief").strip() or "brief"
        audience = str(args.get("audience") or "").strip()
        try:
            top_n = int(args.get("top_n") or 20)
        except (TypeError, ValueError):
            top_n = 20
        top_n = max(1, top_n)

        # Resolve the feature surface. Each branch raises a descriptive
        # error that we surface verbatim rather than mapping to a generic
        # "invalid source" — callers want to know which field broke.
        # For ``kind == "skill"`` the registry is fetched here as a
        # side-effect of resolution and threaded through to ``scan_agent_skills``
        # below to avoid a second bus round-trip. PR #46 Copilot R7 finding #4.
        try:
            surface, prefetched_registry = await self._resolve_feature_surface(
                kind, source_id,
            )
        except (ValueError, LookupError) as e:
            msg = str(e)
            await self._safe_report_gap("suggest_integration_points", msg)
            return {"error": msg}

        # Gather candidates from the four active scan sources. Each scan
        # is independent — a failure in the bus-skills lookup mustn't
        # stop FR-store scanning. We accumulate and rank the survivors.
        candidates: list[integration_scan.IntegrationCandidate] = []

        try:
            existing = self.pipeline.frs.list(include_all=True)
        except Exception as e:
            logger.warning("suggest_integration_points: fr_store.list failed: %s", e)
            existing = []
        candidates.extend(integration_scan.scan_fr_store(surface, existing))

        if prefetched_registry is not None:
            remote_skills = prefetched_registry
        else:
            try:
                remote_skills = await self._fetch_remote_skills()
            except Exception as e:
                logger.warning(
                    "suggest_integration_points: bus skill fetch failed: %s", e,
                )
                remote_skills = []
        skill_candidates = integration_scan.scan_agent_skills(surface, remote_skills)
        # Defensive self-reference filter. ``scan_agent_skills`` already
        # excludes the source's own ``(agent_id, skill_name)`` pair when
        # the source is itself a skill, but any future scan source that
        # emits skill_callsite candidates could miss this gate. Filter
        # here against the authoritative source identity so a scan can
        # never recommend the feature replace itself. PR #46 Copilot R5.
        if kind == "skill" and "." in source_id:
            src_agent, src_skill = source_id.split(".", 1)
            skill_candidates = [
                c for c in skill_candidates
                if not (
                    c.metadata.get("agent_id") == src_agent
                    and c.metadata.get("skill_name") == src_skill
                )
            ]
        candidates.extend(skill_candidates)

        try:
            subscribers = await self._fetch_subscriber_counts(surface.new_events)
        except Exception as e:
            logger.warning("suggest_integration_points: subscriber fetch failed: %s", e)
            subscribers = {}
        candidates.extend(integration_scan.scan_event_subscribers(surface, subscribers))

        # Audience filter is applied post-scan: the metadata on each
        # candidate may carry an 'audience' hint (e.g. from FR tags).
        # MVP keeps this simple — filter out candidates whose metadata
        # carries an explicit audience that doesn't match. Candidates
        # without an audience tag pass through unfiltered.
        if audience:
            candidates = [
                c for c in candidates
                if not c.metadata.get("audience")
                or c.metadata.get("audience") == audience
            ]

        ranked = integration_scan.rank_candidates(candidates)

        # Best-effort LLM rationales for the top slice — cap at the
        # module's MAX_LLM_RATIONALES to keep the budget bounded even
        # if a caller passes a huge top_n.
        rationale_cap = min(top_n, integration_scan.MAX_LLM_RATIONALES)
        rationale_fn = getattr(self, "_llm_rationale", None)
        llm_calls = await integration_scan.apply_llm_rationales(
            ranked, surface, rationale_fn, top_n=rationale_cap,
        )

        # Persist the full scan artifact so distill_integration_points
        # can re-project without rescanning. Storage uses the local
        # knowledge store with a 'scan_integration' tag — keeps the
        # pattern consistent with FR / milestone storage.
        scan_id = integration_scan.compute_scan_id({"kind": kind, "id": source_id})
        persistence_error: Optional[str] = None
        try:
            self._store_integration_scan(
                scan_id=scan_id,
                source={"kind": kind, "id": source_id},
                surface=surface,
                ranked=ranked,
                audience=audience,
            )
        except Exception as e:
            logger.warning("suggest_integration_points: artifact write failed: %s", e)
            persistence_error = str(e)

        extra: dict[str, Any] = {"llm_rationale_calls": llm_calls}
        # Signal persistence failure explicitly so callers can't accidentally
        # call ``distill_integration_points`` on an id that won't resolve.
        # We still return the computed scan id (as ``scan_id_unpersisted``)
        # so the value isn't lost, but ``scan_id`` itself is nulled — the
        # live-scan result is still fully available in ``top_candidates``.
        # PR #46 Copilot R3 finding #4.
        if persistence_error is not None:
            return self._project_scan_response(
                scan_id="",  # distill will not resolve — signal via explicit empty
                source={"kind": kind, "id": source_id, "surface": surface.to_public_dict()},
                ranked=ranked,
                top_n=top_n,
                detail=detail,
                extra={
                    **extra,
                    "persistence_error": persistence_error,
                    "scan_id_unpersisted": scan_id,
                },
            )

        return self._project_scan_response(
            scan_id=scan_id,
            source={"kind": kind, "id": source_id, "surface": surface.to_public_dict()},
            ranked=ranked,
            top_n=top_n,
            detail=detail,
            extra=extra,
        )

    @handler("distill_integration_points")
    async def handle_distill_integration_points(self, args):
        """Re-project a stored scan artifact without rescanning.

        ``scan_id`` is the id returned by ``suggest_integration_points``.
        Filtering is pure re-ranking — no new scan, no bus requests,
        no LLM calls. The same artifact id is echoed back unchanged.
        """
        scan_id = str(args.get("scan_id") or "").strip()
        if not scan_id:
            return {"error": "scan_id is required"}
        try:
            top_n = int(args.get("top_n") or 20)
        except (TypeError, ValueError):
            top_n = 20
        top_n = max(1, top_n)
        signal = str(args.get("signal") or "").strip()
        detail = str(args.get("detail") or "brief").strip() or "brief"

        entry = self.pipeline.knowledge.get(scan_id)
        if entry is None:
            msg = f"scan artifact not found: {scan_id!r}"
            await self._safe_report_gap("distill_integration_points", msg)
            return {"error": msg}

        # Artifact-type gate: refuse to distill entries that weren't
        # produced by ``suggest_integration_points``. The caller could
        # pass any KnowledgeEntry id whose content happens to parse as
        # JSON; without this check we'd return a misleading
        # ``from_artifact`` response instead of a clear error. PR #46
        # Copilot R4 finding #1.
        tags = getattr(entry, "tags", None) or []
        if "scan_integration" not in tags:
            msg = f"not an integration-scan artifact: {scan_id}"
            await self._safe_report_gap("distill_integration_points", msg)
            return {"error": msg}

        try:
            payload = json.loads(entry.content)
        except (ValueError, TypeError) as e:
            msg = f"scan artifact {scan_id!r} is malformed: {e}"
            await self._safe_report_gap("distill_integration_points", msg)
            return {"error": msg}

        # Defensive shape validation — older / truncated / hand-edited
        # KnowledgeEntry rows must not crash the handler. Each gate
        # surfaces a specific error so a caller can diagnose without
        # pulling the raw artifact.
        if not isinstance(payload, dict):
            msg = (
                f"scan artifact {scan_id!r} malformed: payload is "
                f"{type(payload).__name__}, expected dict"
            )
            await self._safe_report_gap("distill_integration_points", msg)
            return {"error": msg}

        # Payload-shape gate: even with the scan_integration tag, the
        # JSON body must carry the expected integration-scan keys
        # (candidates + source). Otherwise it's either a corrupted
        # artifact or a mis-tagged row — either way we shouldn't pretend
        # to distill it. PR #46 Copilot R4 finding #1.
        if "candidates" not in payload or "source" not in payload:
            msg = f"not an integration-scan artifact: {scan_id}"
            await self._safe_report_gap("distill_integration_points", msg)
            return {"error": msg}

        raw_candidates = payload.get("candidates")
        if raw_candidates is None:
            raw_candidates = []
        if not isinstance(raw_candidates, list):
            msg = (
                f"scan artifact {scan_id!r} malformed: candidates is "
                f"{type(raw_candidates).__name__}, expected list"
            )
            await self._safe_report_gap("distill_integration_points", msg)
            return {"error": msg}
        # The stored shape matches IntegrationCandidate.to_full(); rehydrate
        # into objects for uniform ranking with the live-scan path.
        # Non-dict entries and individual items with non-coercible fields
        # (e.g. a non-numeric score string, a metadata value that's a list
        # instead of a mapping) are skipped with a warning rather than
        # crashing the handler — an artifact full of bad rows still returns
        # whatever good rows survived. The response carries a
        # ``skipped_items`` counter so callers can tell the artifact was
        # partial without diffing against a fresh scan.
        #
        # Signal filtering happens post-rehydration so malformed non-dict
        # items are still counted in skipped_items — filtering them out
        # pre-rehydration would silently drop them, undermining the
        # "partial artifact" signal. PR #46 Copilot R7 finding #2.
        rehydrated: list[integration_scan.IntegrationCandidate] = []
        skipped = 0
        for c in raw_candidates:
            if not isinstance(c, dict):
                skipped += 1
                logger.warning(
                    "distill_integration_points: skipping non-dict candidate "
                    "in scan %s: %r",
                    scan_id, type(c).__name__,
                )
                continue
            # Per-item type gates: score must coerce to float; metadata
            # must be mapping-shaped (accept a missing/None field, reject
            # lists/strings which would raise in ``dict(...)``). PR #46
            # Copilot R3 finding #3.
            raw_score = c.get("score")
            try:
                score = float(raw_score) if raw_score is not None else 0.0
            except (TypeError, ValueError):
                skipped += 1
                logger.warning(
                    "distill_integration_points: skipping candidate with "
                    "non-numeric score in scan %s: %r (score=%r)",
                    scan_id, c.get("target_id"), raw_score,
                )
                continue
            raw_meta = c.get("metadata")
            if raw_meta is None:
                meta: dict[str, Any] = {}
            elif isinstance(raw_meta, dict):
                meta = dict(raw_meta)
            else:
                skipped += 1
                logger.warning(
                    "distill_integration_points: skipping candidate with "
                    "non-dict metadata in scan %s: %r (metadata type=%s)",
                    scan_id, c.get("target_id"), type(raw_meta).__name__,
                )
                continue
            rehydrated.append(integration_scan.IntegrationCandidate(
                kind=str(c.get("kind", "")),
                target_id=str(c.get("target_id", "")),
                signal=str(c.get("signal", "")),
                score=score,
                rationale=str(c.get("rationale", "")),
                metadata=meta,
            ))
        if signal:
            rehydrated = [r for r in rehydrated if r.signal == signal]
        ranked = integration_scan.rank_candidates(rehydrated)
        extra: dict[str, Any] = {"from_artifact": True, "signal_filter": signal}
        if skipped:
            extra["skipped_items"] = skipped
        # Rebuild the live-scan response shape: ``source.surface`` is a
        # nested key on the suggest path (see handle_suggest_integration_points)
        # but stored as a sibling of ``source`` in the artifact payload. Merge
        # them back so distill and suggest responses are symmetric — callers
        # reproducing a scan from the artifact can see what surface was
        # scanned. PR #46 Copilot R4 finding #2.
        #
        # Defensive type gate: a mis-tagged or corrupted artifact with a
        # non-dict ``source`` (string / list / number) would crash
        # ``dict(raw_source)`` on list/str inputs in non-obvious ways
        # (``dict(["a", "b"])`` raises ``ValueError``; ``dict("ab")`` raises
        # ``ValueError`` too). Surface a clear validation error rather than
        # bypass the earlier payload-shape gates. Same class as the
        # per-candidate validation (Copilot R3 finding #3) — PR #46 Copilot
        # R6 finding #1.
        # The earlier ``"source" not in payload`` gate catches a missing
        # key but lets ``"source": null`` through. Previously we defaulted
        # ``None`` to ``{}`` and proceeded silently, which produced
        # responses missing ``source.kind`` / ``source.id`` — consumers
        # expecting those fields would break without a clear error. A
        # single ``isinstance(..., dict)`` check cleanly rejects both
        # ``None`` and non-dict types (list/str/number) with one error
        # message. PR #46 Copilot R9 findings #2, #3.
        raw_source = payload.get("source")
        if not isinstance(raw_source, dict):
            msg = (
                f"scan artifact {scan_id!r} malformed: source is "
                f"{type(raw_source).__name__}, expected dict"
            )
            await self._safe_report_gap("distill_integration_points", msg)
            return {"error": msg}
        merged_source: dict[str, Any] = dict(raw_source)
        stored_surface = payload.get("surface")
        if stored_surface is not None and "surface" not in merged_source:
            merged_source["surface"] = stored_surface
        return self._project_scan_response(
            scan_id=scan_id,
            source=merged_source,
            ranked=ranked,
            top_n=top_n,
            detail=detail,
            extra=extra,
        )

    # -- integration scan helpers --

    async def _resolve_feature_surface(
        self, kind: str, source_id: str,
    ) -> tuple[integration_scan.FeatureSurface, Optional[list[dict]]]:
        """Load the underlying feature and extract a scan surface.

        Returns ``(surface, prefetched_registry)``. The second slot is
        the bus skill registry when ``kind == "skill"`` (fetched here to
        resolve the surface) and ``None`` otherwise. The handler threads
        the prefetched registry into ``scan_agent_skills`` to avoid a
        second bus round-trip — one fetch, one failure surface, lower
        latency. PR #46 Copilot R7 finding #4.

        Raises ``LookupError`` when the source can't be found (includes
        bus / network faults while resolving the skill registry — an
        unreachable registry is semantically a lookup miss) and
        ``ValueError`` when the id shape is unparseable. Callers convert
        both into an error-dict response.
        """
        if kind == "fr":
            fr = self.pipeline.frs.get(source_id)
            if fr is None:
                raise LookupError(f"FR {source_id!r} not found in developer store")
            return integration_scan.extract_feature_surface_from_fr(fr), None

        if kind == "pr":
            parsed = integration_scan.parse_pr_id(source_id)
            if parsed is None:
                raise ValueError(
                    f"PR id must be 'owner/repo#N' or a canonical GitHub URL, got {source_id!r}"
                )
            owner, repo_name, pr_number = parsed
            gh = self._github_client()
            repo_slug = f"{owner}/{repo_name}"
            # ``gh.get_pr`` raises ``GithubClientError`` for HTTP/network
            # faults and 404s; the handler's caller only handles
            # ``(ValueError, LookupError)`` so we normalise GitHub faults
            # into ``LookupError`` here (the PR can't be resolved without
            # a reachable API — semantically a lookup miss). Same shape
            # as the ``_fetch_remote_skills`` wrap (PR #46 Copilot R2).
            from developer.github_client import GithubClientError
            try:
                pr_data = await gh.get_pr(repo_slug, pr_number)
            except (ValueError, LookupError):
                raise
            except GithubClientError as e:
                raise LookupError(
                    f"GitHub API unavailable while resolving PR {source_id!r}: {e}"
                ) from e
            except Exception as e:  # noqa: BLE001 — any httpx/network error
                raise LookupError(
                    f"GitHub API unavailable while resolving PR {source_id!r}: {e}"
                ) from e
            if not pr_data.get("merged"):
                raise ValueError(
                    f"PR {source_id!r} is not merged (state={pr_data.get('state')!r}); "
                    "MVP accepts merged PRs only"
                )
            # Body isn't returned by GithubClient.get_pr's default projection;
            # title covers most of the signal for MVP extraction.
            return integration_scan.extract_feature_surface_from_pr(
                pr_id=source_id,
                title=pr_data.get("title", ""),
                body=pr_data.get("body", "") or "",
                owner=owner, repo=repo_name,
            ), None

        if kind == "skill":
            # source_id is '<agent>.<skill>'; resolve via the bus skill
            # registry to pull the description + args schema.
            # ``_fetch_remote_skills`` can raise network / HTTP errors when
            # the bus is unreachable; the handler's caller only handles
            # ``ValueError`` / ``LookupError`` so we normalise bus/network
            # faults into ``LookupError`` here (the skill can't be located
            # without a reachable registry; that's semantically a lookup miss
            # from the handler's perspective).
            try:
                skills = await self._fetch_remote_skills()
            except (ValueError, LookupError):
                raise
            except Exception as e:  # noqa: BLE001 — any httpx/bus error
                raise LookupError(
                    f"skill registry unavailable while resolving {source_id!r}: {e}"
                ) from e
            # Require fully-qualified ``<agent>.<skill>`` form. A bare
            # skill name is ambiguous — multiple agents commonly expose
            # the same name (``health_check``, ``file_bug``, …) and
            # first-match-wins is non-deterministic. Worse, the resolved
            # skill would often match itself against the scan (see
            # ``_filter_self_reference_skill_candidates``), producing a
            # nonsensical self-direct_replace suggestion. PR #46 Copilot R5.
            if "." not in source_id:
                raise LookupError(
                    f"source_id must be 'agent.skill', got {source_id!r}"
                )
            agent_id, skill_name = source_id.split(".", 1)
            if not agent_id or not skill_name:
                raise LookupError(
                    f"source_id must be 'agent.skill', got {source_id!r}"
                )
            match = next(
                (
                    row for row in skills
                    if row.get("name") == skill_name
                    and row.get("agent_id") == agent_id
                ),
                None,
            )
            if match is None:
                raise LookupError(
                    f"skill {source_id!r} not found in bus registry"
                )
            return integration_scan.extract_feature_surface_from_skill(
                skill_id=source_id,
                description=match.get("description", ""),
                agent_id=match.get("agent_id", ""),
                args_schema=match.get("parameters"),
            ), skills

        # Unreachable — validated at handler entry, but belt-and-braces.
        raise ValueError(f"unknown feature kind: {kind!r}")

    async def _fetch_remote_skills(self) -> list[dict]:
        """Return the bus-side skill registry as a list of dicts.

        Uses the agent's own :attr:`_http` client + ``self.bus_url`` so
        tests can monkeypatch the attribute directly. Exceptions from
        ``client.get`` (network faults) or ``response.raise_for_status()``
        (HTTP 4xx/5xx) propagate to the caller; callers are responsible
        for catching + logging and degrading to an empty-registry scan
        path. The ``_http is None`` branch below still returns ``[]``
        (pre-init / older base class) — that's the only fall-through
        default. PR #46 Copilot R9 finding #1.
        """
        # _http is set by BaseAgent.__init__. If we somehow got constructed
        # without it (older base class), degrade to empty list.
        client = getattr(self, "_http", None)
        if client is None:
            return []
        response = await client.get(f"{self.bus_url}/v1/skills")
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else []

    async def _fetch_subscriber_counts(self, topics: list[str]) -> dict[str, int]:
        """Per-topic subscriber counts.

        No canonical bus endpoint exposes per-topic subscribers yet — the
        relevant data lives in bus-side ``subscriptions`` table but isn't
        surfaced via HTTP. For MVP we treat the map as empty (every
        listed topic counts as having zero subscribers, which is what
        wire_subscriber is meant to flag). Tests inject a real map by
        monkeypatching this method. When the bus grows a proper endpoint
        (tracked as follow-up), this method picks it up without touching
        the handler.
        """
        if not topics:
            return {}
        return {}

    def _store_integration_scan(
        self, *, scan_id: str,
        source: dict[str, Any],
        surface: integration_scan.FeatureSurface,
        ranked: list[integration_scan.IntegrationCandidate],
        audience: str,
    ) -> None:
        """Persist the scan as a derived KnowledgeEntry.

        Matches the ``identify_gaps`` precedent in librarian_agent in
        spirit — a single JSON-serialized artifact indexed by a stable
        id — but uses the developer-owned KnowledgeStore rather than the
        bus artifact service so the distill path is a pure local read
        with no round-trip.
        """
        from khonliang.knowledge.store import (
            EntryStatus, KnowledgeEntry, Tier,
        )
        payload = {
            "source": source,
            "surface": surface.to_public_dict(),
            "candidates": [c.to_full() for c in ranked],
            "audience": audience,
            "generated_at": time.time(),
        }
        entry = KnowledgeEntry(
            id=scan_id,
            tier=Tier.DERIVED,
            title=f"Integration scan for {source.get('kind')}:{source.get('id')}",
            content=json.dumps(payload, default=str, separators=(",", ":")),
            source="developer.integration_scan",
            scope="development",
            confidence=1.0,
            status=EntryStatus.DISTILLED,
            tags=["scan_integration", f"kind:{source.get('kind', '')}"],
            metadata={
                "source_kind": source.get("kind", ""),
                "source_id": source.get("id", ""),
                "candidate_count": len(ranked),
                "audience": audience,
            },
        )
        self.pipeline.knowledge.add(entry)

    def _project_scan_response(
        self, *, scan_id: str, source: dict[str, Any],
        ranked: list[integration_scan.IntegrationCandidate],
        top_n: int, detail: str, extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        # ``detail`` is a tri-state — ``brief`` (default), ``compact``, or
        # ``full``. Unknown values fall through to ``brief`` so a typo in
        # the caller doesn't surface as a scan error; the skill schema
        # enumerates the valid values.
        top = ranked[:top_n]
        if detail == "full":
            projected = [c.to_full() for c in top]
        elif detail == "compact":
            projected = [c.to_compact() for c in top]
        else:
            projected = [c.to_brief() for c in top]
        # Hint selection depends on whether the scan actually persisted.
        # When ``scan_id`` is empty the handler has signalled a persistence
        # failure (see ``handle_suggest_integration_points``); pointing
        # callers at ``distill_integration_points`` would send them into
        # a guaranteed "scan_id is required" error. Explain the situation
        # and recommend the retry path instead. PR #46 Copilot R5.
        if scan_id:
            hint = (
                "Use distill_integration_points(scan_id, top_n=..., signal=...) "
                "to filter without rescanning"
            )
        else:
            hint = (
                "scan persistence failed; re-run suggest_integration_points "
                "to retry — distill_integration_points is unavailable without "
                "a persisted scan_id"
            )
        response = {
            "scan_id": scan_id,
            "source": source,
            "total_candidates": len(ranked),
            "top_candidates": projected,
            "hint": hint,
        }
        if extra:
            response.update(extra)
        return response

    def _github_client(self):
        """Lazy GithubClient factory, overridable for tests.

        Kept as a method (not a property) so tests can monkeypatch it
        with a plain function returning a stub without worrying about
        descriptor semantics.
        """
        from developer.github_client import GithubClient
        return GithubClient()

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

    @handler("update_milestone_status")
    async def handle_update_milestone_status(self, args):
        from developer.milestone_store import MilestoneError

        milestone_id = args.get("milestone_id", "")
        status = args.get("status", "")
        if not milestone_id:
            return {"error": "milestone_id is required"}
        if not status:
            return {"error": "status is required"}
        try:
            milestone = self.pipeline.milestones.update_status(
                milestone_id,
                status,
                notes=args.get("notes", ""),
                force=_bool_arg(args, "force", False),
            )
        except MilestoneError as e:
            await self._safe_report_gap("update_milestone_status", str(e))
            return {"error": str(e)}
        return {"milestone": milestone.to_public_dict()}

    @handler("supersede_milestone")
    async def handle_supersede_milestone(self, args):
        from developer.milestone_store import MilestoneError

        superseded_id = args.get("superseded_id", "")
        superseded_by_id = args.get("superseded_by_id", "")
        if not superseded_id or not superseded_by_id:
            return {
                "error": "both superseded_id and superseded_by_id are required"
            }
        try:
            milestone = self.pipeline.milestones.supersede(
                superseded_id,
                superseded_by_id,
                rationale=args.get("rationale", ""),
            )
        except MilestoneError as e:
            await self._safe_report_gap("supersede_milestone", str(e))
            return {"error": str(e)}
        return {"milestone": milestone.to_public_dict()}

    @handler("update_milestone_frs")
    async def handle_update_milestone_frs(self, args):
        from developer.milestone_store import MilestoneError

        milestone_id = args.get("milestone_id", "")
        if not milestone_id:
            return {"error": "milestone_id is required"}
        try:
            milestone = self.pipeline.milestones.update_frs(
                milestone_id,
                add_fr_ids=_parse_paths(args.get("add_fr_ids", "")),
                remove_fr_ids=_parse_paths(args.get("remove_fr_ids", "")),
                notes=args.get("notes", ""),
            )
        except MilestoneError as e:
            await self._safe_report_gap("update_milestone_frs", str(e))
            return {"error": str(e)}
        return {"milestone": milestone.to_public_dict()}

    @handler("delete_milestone")
    async def handle_delete_milestone(self, args):
        from developer.milestone_store import MilestoneError

        milestone_id = args.get("milestone_id", "")
        if not milestone_id:
            return {"error": "milestone_id is required"}
        try:
            return self.pipeline.milestones.delete(
                milestone_id,
                reason=args.get("reason", ""),
                fr_store=self.pipeline.frs,
            )
        except MilestoneError as e:
            await self._safe_report_gap("delete_milestone", str(e))
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

    # -- long-running PR fleet watcher --

    @handler("watch_pr_fleet")
    async def handle_watch_pr_fleet(self, args):
        """Start a background PR watcher and return its id immediately.

        The watcher continues polling after this skill returns — subsequent
        ``list_pr_watchers`` / ``stop_pr_watcher`` / ``bus_wait_for_event``
        calls interact with the already-running loop.
        """
        from developer.pr_watcher import parse_pr_numbers_arg, parse_repos_arg

        repos = parse_repos_arg(args.get("repos"))
        if not repos:
            return {"error": "repos is required (comma-separated owner/name list)"}
        try:
            pr_numbers = parse_pr_numbers_arg(args.get("pr_numbers"))
        except ValueError as e:
            return {"error": str(e)}
        raw_interval = args.get("interval_s", 60)
        # Distinguish "caller omitted" from "caller passed 0/negative".
        # Empty-string and None default to 60; anything else must parse
        # cleanly and be strictly positive.
        if raw_interval in (None, ""):
            interval_s = 60
        else:
            try:
                interval_s = int(raw_interval)
            except (TypeError, ValueError):
                return {"error": "interval_s must be a positive integer"}
            if interval_s <= 0:
                return {"error": "interval_s must be a positive integer"}
        try:
            watcher_id = await self.pr_watcher_registry.start(
                repos=repos, pr_numbers=pr_numbers, interval_s=interval_s,
            )
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            # Registry construction itself can fail when the DB path is
            # unavailable (e.g. a broken config) — surface cleanly.
            await self._safe_report_gap("watch_pr_fleet", f"watcher start failed: {e}")
            return {"error": str(e)}
        return {
            "watcher_id": watcher_id,
            "repos": repos,
            "pr_numbers": pr_numbers,
            "interval_s": interval_s,
        }

    @handler("list_pr_watchers")
    async def handle_list_pr_watchers(self, args):
        try:
            watchers = self.pr_watcher_registry.list_watchers()
        except Exception as e:
            await self._safe_report_gap("list_pr_watchers", str(e))
            return {"error": str(e)}
        return {"count": len(watchers), "watchers": watchers}

    @handler("stop_pr_watcher")
    async def handle_stop_pr_watcher(self, args):
        watcher_id = args.get("watcher_id", "")
        if not watcher_id:
            return {"error": "watcher_id is required"}
        try:
            stopped = await self.pr_watcher_registry.stop(watcher_id)
        except Exception as e:
            await self._safe_report_gap("stop_pr_watcher", str(e))
            return {"error": str(e)}
        return {"watcher_id": watcher_id, "stopped": stopped}

    @handler("pr_fleet_status")
    async def handle_pr_fleet_status(self, args):
        """Return a one-shot fleet snapshot without polling GitHub.

        Delegates to :meth:`PRWatcherRegistry.fleet_snapshot`. An empty
        / omitted ``watcher_id`` means "every live watcher"; a concrete
        id scopes the response to that watcher. Unknown ids return an
        empty ``watchers`` + ``fleet`` rather than an error — the
        caller learns about the typo without our side having to model
        "missing watcher" as an error state.
        """
        raw_id = args.get("watcher_id", "")
        watcher_id: str | None
        if raw_id in (None, "", 0):
            watcher_id = None
        else:
            watcher_id = str(raw_id)
        try:
            return self.pr_watcher_registry.fleet_snapshot(watcher_id=watcher_id)
        except asyncio.CancelledError:
            # CancelledError subclasses Exception; don't let the
            # catch-Exception fallback below convert cooperative
            # cancellation into a returned error dict.
            raise
        except Exception as e:
            await self._safe_report_gap("pr_fleet_status", str(e))
            return {"error": str(e)}

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

    # -- tracking infrastructure (Phase 1: CRUD-only) --
    #
    # Phase 2 will add triage_bug / triage_dogfood / dogfood_triage_queue /
    # report_gap(bug=True) / GH-issue ingest. Those cross the bug<->fr and
    # dogfood<->bug<->fr boundaries; Phase 1 keeps each store independent.

    @handler("file_bug")
    async def handle_file_bug(self, args):
        from developer.bug_store import BugError

        try:
            bug = self.pipeline.bugs.file_bug(
                target=args.get("target", ""),
                title=args.get("title", ""),
                description=args.get("description", ""),
                reproduction=args.get("reproduction", ""),
                observed_entity=args.get("observed_entity", ""),
                severity=args.get("severity", "medium"),
                reporter=args.get("reporter", ""),
            )
        except BugError as e:
            await self._safe_report_gap("file_bug", str(e))
            return {"error": str(e)}
        except Exception as e:
            await self._safe_report_gap("file_bug", f"unexpected failure: {e}")
            raise
        return {
            "bug_id": bug.id,
            "target": bug.target,
            "severity": bug.severity,
            "status": bug.status,
        }

    @handler("list_bugs")
    async def handle_list_bugs(self, args):
        from developer.bug_store import BugError

        target = args.get("target", "") or ""
        severity_min = args.get("severity_min", "") or ""
        status_arg = args.get("status", "")
        detail = args.get("detail", "brief")

        try:
            bugs = self.pipeline.bugs.list_bugs(
                target=target,
                severity_min=severity_min,
                status=status_arg or None,
            )
        except BugError as e:
            return {"error": str(e)}

        if detail == "full":
            serialized = [b.to_public_dict() for b in bugs]
        elif detail == "compact":
            serialized = [b.to_compact_dict() for b in bugs]
        else:
            serialized = [b.to_brief_dict() for b in bugs]
        return {"count": len(bugs), "bugs": serialized}

    @handler("get_bug")
    async def handle_get_bug(self, args):
        bug_id = args.get("bug_id", "")
        detail = args.get("detail", "brief")
        bug = self.pipeline.bugs.get_bug(bug_id)
        if bug is None:
            return {"error": "not found", "bug_id": bug_id}
        if detail == "full":
            return bug.to_public_dict()
        if detail == "compact":
            return bug.to_compact_dict()
        return bug.to_brief_dict()

    @handler("update_bug_status")
    async def handle_update_bug_status(self, args):
        from developer.bug_store import BugError

        try:
            bug = self.pipeline.bugs.update_bug_status(
                bug_id=args.get("bug_id", ""),
                status=args.get("status", ""),
                notes=args.get("notes", ""),
            )
        except BugError as e:
            await self._safe_report_gap("update_bug_status", str(e))
            return {"error": str(e)}
        return {
            "bug_id": bug.id,
            "status": bug.status,
            "updated_at": bug.updated_at,
        }

    @handler("link_bug_pr")
    async def handle_link_bug_pr(self, args):
        from developer.bug_store import BugError

        try:
            bug = self.pipeline.bugs.link_bug_pr(
                bug_id=args.get("bug_id", ""),
                pr_url=args.get("pr_url", ""),
            )
        except BugError as e:
            await self._safe_report_gap("link_bug_pr", str(e))
            return {"error": str(e)}
        return {
            "bug_id": bug.id,
            "linked_pr": bug.linked_pr,
            "status": bug.status,
        }

    @handler("close_bug")
    async def handle_close_bug(self, args):
        from developer.bug_store import BugError

        try:
            bug = self.pipeline.bugs.close_bug(
                bug_id=args.get("bug_id", ""),
                resolution=args.get("resolution", ""),
            )
        except BugError as e:
            await self._safe_report_gap("close_bug", str(e))
            return {"error": str(e)}
        return {
            "bug_id": bug.id,
            "status": bug.status,
            "updated_at": bug.updated_at,
        }

    @handler("log_dogfood")
    async def handle_log_dogfood(self, args):
        """Cheap local capture — keep this hot.

        Must not block on LLM, embedding, or network. Only ``DogfoodStore.
        log_dogfood`` (pure SQLite write) runs here. Errors surface as a
        structured ``error`` dict rather than exceptions so the friction
        of *capturing* friction stays near-zero.
        """
        from developer.dogfood_store import DogfoodError

        try:
            dog = self.pipeline.dogfood.log_dogfood(
                args.get("observation", ""),
                kind=args.get("kind", "friction"),
                target=args.get("target", ""),
                context=args.get("context", ""),
                reporter=args.get("reporter", ""),
            )
        except DogfoodError as e:
            return {"error": str(e)}
        return {
            "dog_id": dog.id,
            "kind": dog.kind,
            "status": dog.status,
        }

    @handler("list_dogfood")
    async def handle_list_dogfood(self, args):
        from developer.dogfood_store import DogfoodError

        kind = args.get("kind", "") or ""
        target = args.get("target", "") or ""
        since_raw = args.get("since", "")
        detail = args.get("detail", "brief")
        # Preserve explicit None from the caller so the MCP surface can
        # express "no cap" — matches DogfoodStore.list_dogfood's API where
        # limit=None means unbounded. Omitted limit defaults to 20.
        _MISSING = object()
        raw_limit = args.get("limit", _MISSING)
        limit: int | None
        if raw_limit is _MISSING:
            limit = 20
        elif raw_limit is None:
            limit = None
        else:
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                limit = 20
        if limit is not None and limit < 0:
            limit = 0

        since: float | None
        if since_raw == "" or since_raw is None:
            since = None
        else:
            try:
                since = float(since_raw)
            except (TypeError, ValueError):
                return {"error": f"since must be a number (epoch seconds), got {since_raw!r}"}

        try:
            dogs = self.pipeline.dogfood.list_dogfood(
                kind=kind,
                target=target,
                since=since,
                status=args.get("status") or None,
                limit=limit,
            )
        except DogfoodError as e:
            return {"error": str(e)}

        if detail == "full":
            serialized = [d.to_public_dict() for d in dogs]
        elif detail == "compact":
            serialized = [d.to_compact_dict() for d in dogs]
        else:
            serialized = [d.to_brief_dict() for d in dogs]
        return {"count": len(dogs), "dogfood": serialized}

    @handler("get_dogfood")
    async def handle_get_dogfood(self, args):
        dog_id = args.get("dog_id", "")
        detail = args.get("detail", "brief")
        dog = self.pipeline.dogfood.get_dogfood(dog_id)
        if dog is None:
            return {"error": "not found", "dog_id": dog_id}
        if detail == "full":
            return dog.to_public_dict()
        if detail == "compact":
            return dog.to_compact_dict()
        return dog.to_brief_dict()

    # -- tracking infrastructure (Phase 2A: triage loop) --

    @handler("triage_bug")
    async def handle_triage_bug(self, args):
        """Triage a bug: optional severity/status update + optional FR escalation.

        Idempotent re-escalation: if the bug already has ``linked_frs``
        and ``escalate_to_fr`` is true, return the existing linkage
        rather than creating another FR.
        """
        from developer.bug_store import BugError, TERMINAL_STATUSES
        from developer.fr_store import FRError

        bug_id = args.get("bug_id", "")
        severity = args.get("severity", "") or ""
        status = args.get("status", "") or ""
        escalate = _bool_arg(args, "escalate_to_fr", False)
        notes = args.get("notes", "")

        bug = self.pipeline.bugs.get_bug(bug_id)
        if bug is None:
            return {"error": f"unknown bug id: {bug_id}", "bug_id": bug_id}

        # Reject terminal+escalate in the same call up-front: otherwise the
        # status transition would land first, then escalate_to_fr refuses the
        # now-terminal bug — but the FR was already created, leaving an
        # orphan FR and an inconsistent state. Callers that want both must
        # escalate first, then close separately.
        if escalate and status and status in TERMINAL_STATUSES:
            msg = (
                f"cannot escalate_to_fr with status={status!r} in the same call "
                "(terminal transition would strand the created FR); "
                "escalate first, then close separately"
            )
            await self._safe_report_gap("triage_bug", msg)
            return {"error": msg, "bug_id": bug_id}

        # Symmetric guard: bug is ALREADY terminal and caller wants to
        # escalate. Without this, promote() creates the FR and then
        # BugStore.escalate_to_fr() refuses the terminal bug — same
        # orphan-FR failure mode as the explicit-terminal-status case
        # above, just triggered by an empty status arg on an
        # already-closed bug. Only applies when no status change was
        # requested (with status set, re-opening into a non-terminal
        # state before escalating is a legitimate path we don't want
        # to block here; update_bug_status handles the transition
        # legality).
        if escalate and not status and not bug.linked_frs and bug.status in TERMINAL_STATUSES:
            msg = (
                f"cannot escalate_to_fr on bug in terminal status {bug.status!r} "
                "(would strand the created FR); re-open the bug first, or "
                "link an existing FR via link_bug_fr instead"
            )
            await self._safe_report_gap("triage_bug", msg)
            return {"error": msg, "bug_id": bug_id}

        try:
            if severity:
                bug = self.pipeline.bugs.update_severity(bug_id, severity, notes=notes)
            if status:
                bug = self.pipeline.bugs.update_bug_status(bug_id, status, notes=notes)
        except BugError as e:
            await self._safe_report_gap("triage_bug", str(e))
            return {"error": str(e), "bug_id": bug_id}

        created_fr_id = ""
        escalation_reused = False
        if escalate:
            if bug.linked_frs:
                # Idempotent: don't double-create. Return the first linked
                # FR as the "existing" escalation target.
                created_fr_id = bug.linked_frs[0]
                escalation_reused = True
            else:
                try:
                    fr = self.pipeline.frs.promote(
                        target=bug.target,
                        title=f"[bug] {bug.title}",
                        description=_compose_fr_description_from_bug(bug),
                        priority=_severity_to_priority(bug.severity),
                        concept="triage",
                        classification="app",
                        backing_papers=[bug.id],
                    )
                    created_fr_id = fr.id
                except FRError as e:
                    await self._safe_report_gap("triage_bug", str(e))
                    return {"error": str(e), "bug_id": bug_id}
                try:
                    bug = self.pipeline.bugs.escalate_to_fr(
                        bug_id, created_fr_id,
                        notes=notes or "triage_bug",
                    )
                except BugError as e:
                    await self._safe_report_gap("triage_bug", str(e))
                    return {"error": str(e), "bug_id": bug_id}

        response = {
            "bug_id": bug.id,
            "severity": bug.severity,
            "status": bug.status,
            "linked_frs": list(bug.linked_frs),
            "updated_at": bug.updated_at,
        }
        if escalate:
            response["escalated_fr_id"] = created_fr_id
            response["escalation_reused"] = escalation_reused
        return response

    @handler("link_bug_fr")
    async def handle_link_bug_fr(self, args):
        """Manually attach an FR to a bug's linked_frs (idempotent)."""
        from developer.bug_store import BugError
        from developer.fr_store import FRError

        bug_id = args.get("bug_id", "")
        fr_id = args.get("fr_id", "")

        # Validate the FR exists before recording the link — a dangling
        # linked_frs entry would mask typos/drift. Follow redirects so
        # linking a merged FR lands on the terminal id.
        try:
            fr = self.pipeline.frs.get(fr_id, follow_redirect=True)
        except FRError as e:
            await self._safe_report_gap("link_bug_fr", str(e))
            return {"error": str(e), "bug_id": bug_id, "fr_id": fr_id}
        if fr is None:
            return {"error": f"unknown fr id: {fr_id}", "bug_id": bug_id, "fr_id": fr_id}

        try:
            bug = self.pipeline.bugs.escalate_to_fr(
                bug_id, fr.id, notes="link_bug_fr",
            )
        except BugError as e:
            await self._safe_report_gap("link_bug_fr", str(e))
            return {"error": str(e), "bug_id": bug_id, "fr_id": fr_id}
        return {
            "bug_id": bug.id,
            "fr_id": fr.id,
            "linked_frs": list(bug.linked_frs),
        }

    @handler("triage_dogfood")
    async def handle_triage_dogfood(self, args):
        """Triage a dogfood observation.

        Promote paths preserve the observation row verbatim — only
        status and promoted_to change. Dismiss / mark_duplicate route
        straight through to the Phase-1 terminal methods.
        """
        from developer.bug_store import BugError
        from developer.dogfood_store import (
            DOGFOOD_STATUS_DISMISSED,
            DOGFOOD_STATUS_DUPLICATE,
            DOGFOOD_STATUS_PROMOTED,
            DogfoodError,
        )
        from developer.fr_store import FRError

        dog_id = args.get("dog_id", "")
        action = args.get("action", "")
        target_id = args.get("target_id", "") or ""
        notes = args.get("notes", "")

        dog = self.pipeline.dogfood.get_dogfood(dog_id)
        if dog is None:
            return {"error": f"unknown dog id: {dog_id}", "dog_id": dog_id}

        # Up-front guard for promote_to_{bug,fr} on an already-terminal
        # dogfood. Without this, file_bug / promote runs first, then
        # record_promotion refuses the terminal status, leaving an
        # orphan bug / FR. The two terminal statuses that refuse
        # promotion outright are dismissed and duplicate; the
        # promoted status is handled separately by the idempotent
        # re-promotion path (see below) which reuses the existing
        # downstream id — point the caller at that flow explicitly so
        # they don't confuse a terminal refusal with a missing guard.
        if action in ("promote_to_bug", "promote_to_fr"):
            if dog.status in (DOGFOOD_STATUS_DISMISSED, DOGFOOD_STATUS_DUPLICATE):
                msg = (
                    f"cannot {action} on dogfood in terminal status "
                    f"{dog.status!r} (would strand the created downstream "
                    "record); re-open is not supported for dismissed/"
                    "duplicate observations"
                )
                await self._safe_report_gap("triage_dogfood", msg)
                return {"error": msg, "dog_id": dog_id}
            # status=promoted + empty promoted_to is a corrupted state
            # (status lies about reality), so refuse rather than create
            # an orphan. Cross-kind double-promotion is a legitimate
            # workflow: a dog already promoted to an FR may later be
            # promoted to a bug (and vice-versa), in which case
            # record_promotion appends the new id to promoted_to and
            # keeps status='promoted'. So the guard fires ONLY when
            # promoted_to is entirely empty. Same-kind re-promotion is
            # handled below by the idempotent-reuse paths which return
            # the existing id without re-creating the downstream record.
            if dog.status == DOGFOOD_STATUS_PROMOTED and not dog.promoted_to:
                msg = (
                    f"dogfood {dog_id} is in status promoted but "
                    f"promoted_to is empty — refusing {action} to avoid "
                    "orphan downstream record (corrupted state)"
                )
                await self._safe_report_gap("triage_dogfood", msg)
                return {"error": msg, "dog_id": dog_id}

        if action == "dismiss":
            try:
                dog = self.pipeline.dogfood.mark_dismissed(dog_id, notes=notes)
            except DogfoodError as e:
                await self._safe_report_gap("triage_dogfood", str(e))
                return {"error": str(e), "dog_id": dog_id}
            return {
                "dog_id": dog.id,
                "status": dog.status,
                "action": action,
            }

        if action == "mark_duplicate":
            if not target_id:
                return {"error": "mark_duplicate requires target_id", "dog_id": dog_id}
            try:
                dog = self.pipeline.dogfood.mark_duplicate(dog_id, target_id)
            except DogfoodError as e:
                await self._safe_report_gap("triage_dogfood", str(e))
                return {"error": str(e), "dog_id": dog_id}
            return {
                "dog_id": dog.id,
                "status": dog.status,
                "action": action,
                "duplicate_of": dog.duplicate_of,
            }

        if action == "promote_to_bug":
            # Idempotent re-promotion: if this dog has already been
            # promoted to a bug, return that existing bug_id instead of
            # calling file_bug again. The downstream id is deterministic
            # in (target, title, description, observed_entity), so a retry
            # of the full path would raise a collision on file_bug BEFORE
            # record_promotion could no-op — leaving the handler stuck.
            # Mirror triage_bug's escalation_reused flag so callers can
            # tell it was a replay.
            existing_bug_id = next(
                (tid for tid in dog.promoted_to if tid.startswith("bug_")),
                "",
            )
            if existing_bug_id:
                return {
                    "dog_id": dog.id,
                    "status": dog.status,
                    "action": action,
                    "promoted_to": list(dog.promoted_to),
                    "bug_id": existing_bug_id,
                    "promotion_reused": True,
                }
            try:
                bug = self.pipeline.bugs.file_bug(
                    target=dog.target or "developer",
                    title=_title_from_observation(dog.observation),
                    description=_compose_bug_description_from_dogfood(dog),
                    observed_entity=dog.context,
                    severity=_kind_to_severity(dog.kind),
                    reporter=dog.reporter,
                )
            except BugError as e:
                await self._safe_report_gap("triage_dogfood", str(e))
                return {"error": str(e), "dog_id": dog_id}
            try:
                dog = self.pipeline.dogfood.record_promotion(
                    dog_id, bug.id, "bug", notes=notes or "triage_dogfood",
                )
            except DogfoodError as e:
                await self._safe_report_gap("triage_dogfood", str(e))
                return {"error": str(e), "dog_id": dog_id}
            return {
                "dog_id": dog.id,
                "status": dog.status,
                "action": action,
                "promoted_to": list(dog.promoted_to),
                "bug_id": bug.id,
                "promotion_reused": False,
            }

        if action == "promote_to_fr":
            # See promote_to_bug above — mirrored idempotency guard.
            existing_fr_id = next(
                (tid for tid in dog.promoted_to if tid.startswith("fr_")),
                "",
            )
            if existing_fr_id:
                return {
                    "dog_id": dog.id,
                    "status": dog.status,
                    "action": action,
                    "promoted_to": list(dog.promoted_to),
                    "fr_id": existing_fr_id,
                    "promotion_reused": True,
                }
            try:
                fr = self.pipeline.frs.promote(
                    target=dog.target or "developer",
                    title=f"[dogfood] {_title_from_observation(dog.observation)}",
                    description=_compose_fr_description_from_dogfood(dog),
                    priority=_kind_to_priority(dog.kind),
                    concept="dogfood",
                    classification="app",
                    backing_papers=[dog.id],
                )
            except FRError as e:
                await self._safe_report_gap("triage_dogfood", str(e))
                return {"error": str(e), "dog_id": dog_id}
            try:
                dog = self.pipeline.dogfood.record_promotion(
                    dog_id, fr.id, "fr", notes=notes or "triage_dogfood",
                )
            except DogfoodError as e:
                await self._safe_report_gap("triage_dogfood", str(e))
                return {"error": str(e), "dog_id": dog_id}
            return {
                "dog_id": dog.id,
                "status": dog.status,
                "action": action,
                "promoted_to": list(dog.promoted_to),
                "fr_id": fr.id,
                "promotion_reused": False,
            }

        return {
            "error": f"unknown action: {action!r} "
                     "(expected promote_to_bug / promote_to_fr / dismiss / mark_duplicate)",
            "dog_id": dog_id,
        }

    @handler("dogfood_triage_queue")
    async def handle_dogfood_triage_queue(self, args):
        """Ranked queue of observed-status dogfood entries.

        Designed as input to a periodic triage session — oldest urgent
        items first. Rank is kind-priority × recency; see
        :meth:`DogfoodStore.triage_queue`.
        """
        from developer.dogfood_store import DogfoodError

        detail = args.get("detail", "compact")
        _MISSING = object()
        raw_limit = args.get("limit", _MISSING)
        if raw_limit is _MISSING:
            limit = 10
        elif raw_limit is None:
            limit = None
        else:
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                limit = 10
        if limit is not None and limit < 0:
            limit = 0

        try:
            dogs = self.pipeline.dogfood.triage_queue(limit=limit)
        except DogfoodError as e:
            return {"error": str(e)}

        if detail == "full":
            serialized = [d.to_public_dict() for d in dogs]
        elif detail == "brief":
            serialized = [d.to_brief_dict() for d in dogs]
        else:
            serialized = [d.to_compact_dict() for d in dogs]
        return {"count": len(dogs), "dogfood": serialized}

    @handler("report_gap")
    async def handle_report_gap(self, args):
        """Bus-exposed report_gap with optional bug filing.

        Thin wrapper around :meth:`report_gap`. Fires the normal
        telemetry event (so subscribers see the gap), and when
        ``bug=True`` also calls :meth:`BugStore.file_bug` so the gap
        lands in the tracker alongside the bus event. Returns the
        created bug_id when applicable.
        """
        operation = (args.get("operation", "") or "").strip()
        reason = (args.get("reason", "") or "").strip()
        if not operation or not reason:
            return {"error": "report_gap requires non-empty operation and reason"}
        file_bug = _bool_arg(args, "bug", False)
        severity = (args.get("severity", "medium") or "medium").strip() or "medium"
        target_override = (args.get("target", "") or "").strip()

        result = await self.report_gap(operation, reason, bug=file_bug,
                                       severity=severity, target=target_override)
        # Always return the telemetry acknowledgement shape so callers
        # can tell the event fired; ``result`` is a dict when the
        # developer-level wrapper handled it (both True and False
        # paths), and the shape matches ``{event, operation, reason,
        # bug_id?}``.
        return result

    # ------------------------------------------------------------------
    # report_gap override
    # ------------------------------------------------------------------

    async def report_gap(
        self,
        operation: str,
        reason: str,
        context: dict | None = None,
        *,
        bug: bool = False,
        severity: str = "medium",
        target: str = "",
    ) -> dict:
        """Developer-side override of :meth:`BaseAgent.report_gap`.

        Fires the normal bus telemetry event (preserving Phase 1
        behavior for every in-tree caller that uses the 3-arg form),
        then optionally also files a BugStore entry when ``bug=True``.

        Signature is backward-compatible: every existing caller
        (``self.report_gap(op, reason)`` / ``self.report_gap(op, reason,
        {context})``) keeps working because the new keyword arguments
        default to ``bug=False``.

        Returns a dict with the acknowledgement payload. The base
        BaseAgent.report_gap returns None; returning a dict here is
        additive — existing callers that ``await`` without consuming
        the return value aren't affected. Callers that pass ``bug=True``
        get the ``bug_id`` in the payload.

        Best-effort bus send: if the bus isn't connected (tests / dry
        runs) we swallow the ``RuntimeError`` so the bug-file path still
        lands. Developer-triage loops need the bug tracker to stay
        authoritative regardless of bus reachability.
        """
        bus_ack = {"event": "gap.observed", "operation": operation, "reason": reason}
        try:
            await super().report_gap(operation, reason, context)
        except RuntimeError:
            # Not bus-connected — the telemetry half is a no-op but the
            # bug half still runs. Mark the ack so callers can tell.
            bus_ack["event"] = "gap.not_sent"

        if not bug:
            return bus_ack

        try:
            filed = self.pipeline.bugs.file_bug(
                target=target or self.agent_type,
                title=_gap_bug_title(operation, reason),
                description=_gap_bug_description(operation, reason, context),
                observed_entity=operation,
                severity=severity,
                reporter=self.agent_id or self.agent_type,
            )
        except asyncio.CancelledError:
            # Never swallow task cancellation — let it propagate so the
            # enclosing async task terminates cleanly. Same pattern as
            # PR #39 R4 for pr_watcher.
            raise
        except Exception as e:
            # Filing the bug is best-effort; surface the failure in the
            # ack but don't raise — report_gap callers use this from
            # inside except-branches and raising here would mask the
            # original problem.
            bus_ack["bug_error"] = str(e)
            return bus_ack

        bus_ack["bug_id"] = filed.id
        return bus_ack


# ---------------------------------------------------------------------------
# Phase 2A triage helpers
#
# Shared between ``triage_bug`` / ``triage_dogfood`` / ``report_gap``. Kept
# module-level (not class methods) because they're pure — they transform
# dogfood/bug fields into FR/bug seed fields without touching stores.
# ---------------------------------------------------------------------------


def _title_from_observation(observation: str, *, max_len: int = 72) -> str:
    """Shorten an observation to a single-line title for a downstream record.

    Keeps the first line; truncates at ``max_len`` with an ellipsis. The
    bug/FR stores both have their own length constraints and prefer
    one-line titles.
    """
    line = (observation or "").strip().splitlines()[0] if observation and observation.strip() else ""
    if len(line) <= max_len:
        return line or "(unnamed observation)"
    return line[: max_len - 1].rstrip() + "…"


def _compose_bug_description_from_dogfood(dog) -> str:
    """Seed a BugStore description from a dogfood observation.

    Preserves full observation text verbatim and records the source
    dog_id for provenance. The downstream bug's own ``description``
    field is what appears in the tracker; adding the dog_id into the
    body (not just metadata) means a human reading the bug in isolation
    still sees the chain-of-custody.
    """
    parts = [dog.observation.strip()]
    if dog.context:
        parts.append(f"Context: {dog.context.strip()}")
    parts.append(f"Promoted from dogfood observation {dog.id}.")
    return "\n\n".join(parts)


def _compose_fr_description_from_dogfood(dog) -> str:
    """Seed an FR description from a dogfood observation."""
    parts = [dog.observation.strip()]
    if dog.context:
        parts.append(f"Context: {dog.context.strip()}")
    parts.append(f"Promoted from dogfood observation {dog.id}.")
    return "\n\n".join(parts)


def _compose_fr_description_from_bug(bug) -> str:
    """Seed an FR description from a bug escalation.

    Per spec: "call the existing ``promote_fr`` skill with the bug's
    title/description/reproduction as seed; backing reference points at
    the bug id." The description in the FR body explicitly references
    the bug id so a reader of the FR alone sees the provenance.
    """
    parts = [bug.description.strip()]
    if bug.reproduction:
        parts.append(f"Reproduction: {bug.reproduction.strip()}")
    parts.append(f"Escalated from bug {bug.id} (severity={bug.severity}).")
    return "\n\n".join(parts)


def _severity_to_priority(severity: str) -> str:
    """Map a bug severity to an FR priority for escalation.

    ``blocker``/``high`` -> FR ``high``; ``medium`` -> ``medium``;
    ``low`` -> ``low``. A blocker bug converts to a high-priority FR
    because there's no ``blocker`` priority in FRStore.
    """
    mapping = {
        "blocker": "high",
        "high": "high",
        "medium": "medium",
        "low": "low",
    }
    return mapping.get(severity, "medium")


def _kind_to_severity(kind: str) -> str:
    """Map a dogfood kind to a bug severity for promote_to_bug.

    ``bug`` kinds land at medium by default (they were logged as
    friction-shaped bugs — real severity emerges in subsequent
    triage); ux/friction/docs also default to medium so the tracker
    doesn't silently downgrade unreviewed captures. Callers can pass a
    triage override on a later ``triage_bug`` call.
    """
    return "medium"


def _kind_to_priority(kind: str) -> str:
    """Map a dogfood kind to an FR priority for promote_to_fr."""
    return "medium"


def _gap_bug_title(operation: str, reason: str, *, max_len: int = 72) -> str:
    """Title for a gap-sourced bug.

    Front-loads the operation name so the tracker reads well when
    scanning: ``[gap] promote_fr: backing_papers parse failure``.
    """
    base = f"[gap] {operation}: {reason}"
    if len(base) <= max_len:
        return base
    return base[: max_len - 1].rstrip() + "…"


def _gap_bug_description(operation: str, reason: str, context: dict | None) -> str:
    """Description body for a gap-sourced bug."""
    parts = [f"Capability gap observed in {operation}: {reason}"]
    if context:
        try:
            parts.append("Context: " + json.dumps(context, default=str, sort_keys=True))
        except (TypeError, ValueError):
            parts.append(f"Context: {context!r}")
    return "\n\n".join(parts)


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

"""Configuration loader for the developer runtime.

Mirrors khonliang-researcher's config-loading pattern (load YAML, resolve
relative paths against the config-file directory, write resolved values
back) and adds typed dataclass wrappers for the structured blocks.

Validates ``models``, ``bus`` and ``researcher_mcp`` config blocks.
The bus URL is used by :class:`~developer.researcher_client.ResearcherClient`
for researcher evidence/context calls; FR ownership lives in developer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised when the developer config file is structurally invalid."""


REQUIRED_MODEL_KEYS = (
    "summarizer",
    "extractor",
    "assessor",
    "idea_parser",
    "embedder",
    "reviewer",
)


@dataclass
class ProjectConfig:
    """A project that the developer MCP server manages."""

    name: str
    repo: Path
    specs_dir: str = "specs"

    @property
    def specs_root(self) -> Path:
        return self.repo / self.specs_dir


@dataclass
class ModelsConfig:
    """Model assignments used by evaluation and review workflows."""

    summarizer: str = ""
    extractor: str = ""
    assessor: str = ""
    idea_parser: str = ""
    embedder: str = ""
    reviewer: str = ""


@dataclass
class BusConfig:
    """khonliang-bus connection settings."""

    url: str = "http://localhost:8787"
    enabled: bool = False


@dataclass
class ResearcherMCPConfig:
    """How to reach researcher for evidence/context calls."""

    transport: str = "stdio"  # stdio | http
    command: str = ""
    args: list[str] = field(default_factory=list)
    url: str = ""
    timeout: int = 30


@dataclass
class Config:
    """Top-level developer config.

    Construct via :meth:`Config.load`. All path fields are absolute after
    loading; relative paths in the YAML file are resolved against the
    config-file's directory.
    """

    config_path: Path
    db_path: Path
    workspace_root: Path
    prompts_dir: Path
    projects: dict[str, ProjectConfig]
    models: ModelsConfig
    bus: BusConfig
    researcher_mcp: ResearcherMCPConfig
    raw: dict[str, Any]  # original (resolved) dict — preserved for downstream consumers

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        """Load and validate a developer config file.

        Resolves relative paths against ``Path(path).resolve().parent`` and
        rewrites the in-memory dict so any downstream consumer that reads
        ``raw[...]`` sees absolute paths.
        """
        config_path = Path(path).resolve()
        if not config_path.exists():
            raise ConfigError(f"config file not found: {config_path}")

        with open(config_path) as f:
            data: Any = yaml.safe_load(f) or {}

        # YAML can legally hold a top-level list, scalar, or null. Reject
        # anything that is not a mapping with a clean ConfigError instead
        # of letting ``data.get(...)`` raise AttributeError further down.
        if not isinstance(data, dict):
            raise ConfigError(
                f"config file {config_path} must contain a YAML mapping at "
                f"the top level (got {type(data).__name__})"
            )

        config_dir = config_path.parent

        # --- path resolution -------------------------------------------------
        db_path = _resolve_path(data, "db_path", "data/developer.db", config_dir)
        workspace_root = _resolve_path(
            data, "workspace_root", str(config_dir.parent), config_dir
        )
        prompts_dir = _resolve_path(data, "prompts_dir", "prompts", config_dir)

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # --- projects --------------------------------------------------------
        projects_raw = data.get("projects") or {}
        if not isinstance(projects_raw, dict):
            raise ConfigError("'projects' must be a mapping of project_name -> { repo, specs_dir }")
        projects: dict[str, ProjectConfig] = {}
        for name, body in projects_raw.items():
            if not isinstance(body, dict) or "repo" not in body:
                raise ConfigError(f"projects[{name!r}] must have a 'repo' field")
            repo = Path(body["repo"])
            if not repo.is_absolute():
                repo = (config_dir / repo).resolve()
            if not repo.exists() or not repo.is_dir():
                # Warn-don't-error per milestone Task 2: missing repo doesn't
                # block server startup, only ``list_specs`` for that project.
                logger.warning(
                    "projects[%r].repo does not exist: %s — list_specs(%r) "
                    "will return an empty list until the path is created.",
                    name,
                    repo,
                    name,
                )
            projects[name] = ProjectConfig(
                name=name,
                repo=repo,
                specs_dir=body.get("specs_dir", "specs"),
            )

        # --- integration blocks --------------------------------------------
        models = _parse_models(data.get("models"))
        bus = _parse_bus(data.get("bus"))
        researcher_mcp = _parse_researcher_mcp(data.get("researcher_mcp"))

        return cls(
            config_path=config_path,
            db_path=Path(db_path),
            workspace_root=Path(workspace_root),
            prompts_dir=Path(prompts_dir),
            projects=projects,
            models=models,
            bus=bus,
            researcher_mcp=researcher_mcp,
            raw=data,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _resolve_path(
    data: dict[str, Any], key: str, default: str, config_dir: Path
) -> str:
    """Resolve ``data[key]`` against ``config_dir`` if relative; rewrite in-place."""
    raw = data.get(key, default)
    p = Path(raw)
    if not p.is_absolute():
        p = (config_dir / p).resolve()
    data[key] = str(p)
    return str(p)


def _parse_models(block: Any) -> ModelsConfig:
    if block is None:
        raise ConfigError(
            "missing 'models' block — developer requires the block to exist "
            f"with keys {REQUIRED_MODEL_KEYS} (values may be empty strings)"
        )
    if not isinstance(block, dict):
        raise ConfigError("'models' must be a mapping")
    missing = [k for k in REQUIRED_MODEL_KEYS if k not in block]
    if missing:
        raise ConfigError(
            f"'models' block missing required keys: {missing}. "
            f"All of {REQUIRED_MODEL_KEYS} must be present (values may be empty)."
        )
    return ModelsConfig(**{k: str(block.get(k, "")) for k in REQUIRED_MODEL_KEYS})


def _parse_bus(block: Any) -> BusConfig:
    if block is None:
        raise ConfigError(
            "missing 'bus' block — requires {url, enabled} to exist"
        )
    if not isinstance(block, dict):
        raise ConfigError("'bus' must be a mapping")
    if "url" not in block or "enabled" not in block:
        raise ConfigError("'bus' block requires both 'url' and 'enabled' keys")
    enabled = bool(block["enabled"])
    return BusConfig(url=str(block["url"]), enabled=enabled)


def _parse_researcher_mcp(block: Any) -> ResearcherMCPConfig:
    if block is None:
        raise ConfigError(
            "missing 'researcher_mcp' block — developer still needs "
            "researcher connection settings for evidence/context calls"
        )
    if not isinstance(block, dict):
        raise ConfigError("'researcher_mcp' must be a mapping")
    transport = str(block.get("transport", "stdio"))
    if transport not in ("stdio", "http"):
        raise ConfigError(
            f"'researcher_mcp.transport' must be 'stdio' or 'http', got {transport!r}"
        )
    if transport == "stdio":
        if not block.get("command"):
            raise ConfigError(
                "'researcher_mcp.command' is required when transport=stdio"
            )
    else:  # http
        if not block.get("url"):
            raise ConfigError(
                "'researcher_mcp.url' is required when transport=http"
            )
    return ResearcherMCPConfig(
        transport=transport,
        command=str(block.get("command", "")),
        args=list(block.get("args", []) or []),
        url=str(block.get("url", "")),
        timeout=int(block.get("timeout", 30)),
    )

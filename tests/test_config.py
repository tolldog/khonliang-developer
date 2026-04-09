"""Tests for developer.config — path resolution and forward-looking blocks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from developer.config import (
    Config,
    ConfigError,
    REQUIRED_MODEL_KEYS,
)


# ---------------------------------------------------------------------------
# Path resolution (acceptance #3)
# ---------------------------------------------------------------------------


def test_load_resolves_relative_paths_against_config_dir(tmp_path, monkeypatch):
    """Acceptance #3: Config.load resolves paths against the config-file dir.

    Verified by loading from a non-cwd location.
    """
    cfg_dir = tmp_path / "subdir"
    cfg_dir.mkdir()
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "db_path": "data/developer.db",
                "workspace_root": "..",
                "prompts_dir": "prompts",
                "projects": {
                    "developer": {"repo": str(tmp_path), "specs_dir": "specs"}
                },
                "models": {k: "" for k in REQUIRED_MODEL_KEYS},
                "bus": {"url": "x", "enabled": False},
                "researcher_mcp": {
                    "transport": "stdio",
                    "command": "python",
                    "args": [],
                },
            }
        )
    )

    # Change cwd somewhere unrelated to prove paths don't resolve against cwd.
    monkeypatch.chdir(tmp_path.parent)

    config = Config.load(cfg_path)

    assert config.db_path == cfg_dir / "data" / "developer.db"
    assert config.db_path.is_absolute()
    assert config.workspace_root == tmp_path  # ".." from cfg_dir
    assert config.prompts_dir == cfg_dir / "prompts"
    # Resolved values are written back into the raw dict.
    assert config.raw["db_path"] == str(cfg_dir / "data" / "developer.db")
    assert config.raw["prompts_dir"] == str(cfg_dir / "prompts")


def test_load_creates_db_parent_directory(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "db_path": "deep/nested/data/developer.db",
                "workspace_root": str(tmp_path),
                "prompts_dir": "prompts",
                "projects": {},
                "models": {k: "" for k in REQUIRED_MODEL_KEYS},
                "bus": {"url": "x", "enabled": False},
                "researcher_mcp": {
                    "transport": "stdio",
                    "command": "python",
                    "args": [],
                },
            }
        )
    )
    Config.load(cfg_path)
    assert (tmp_path / "deep" / "nested" / "data").is_dir()


# ---------------------------------------------------------------------------
# Forward-looking block validation
# ---------------------------------------------------------------------------


def test_load_requires_models_block(temp_config_file):
    cfg = temp_config_file({"models": None})
    with pytest.raises(ConfigError, match="models"):
        Config.load(cfg)


def test_load_requires_all_model_keys(tmp_path):
    """Models block must have ALL required keys, not just some."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "db_path": "data/developer.db",
                "workspace_root": str(tmp_path),
                "prompts_dir": "prompts",
                "projects": {},
                "models": {"summarizer": ""},  # missing the other 5 keys
                "bus": {"url": "x", "enabled": False},
                "researcher_mcp": {
                    "transport": "stdio",
                    "command": "python",
                    "args": [],
                },
            }
        )
    )
    with pytest.raises(ConfigError, match="missing required keys"):
        Config.load(cfg)


def test_load_accepts_empty_model_values(temp_config_file):
    """Empty strings are valid — MS-01 doesn't construct a ModelPool."""
    cfg = temp_config_file({"models": {k: "" for k in REQUIRED_MODEL_KEYS}})
    config = Config.load(cfg)
    for k in REQUIRED_MODEL_KEYS:
        assert getattr(config.models, k) == ""


def test_load_requires_bus_block(temp_config_file):
    cfg = temp_config_file({"bus": None})
    with pytest.raises(ConfigError, match="bus"):
        Config.load(cfg)


def test_load_refuses_bus_enabled_true(temp_config_file):
    """MS-01 must refuse enabled=true so no client is constructed."""
    cfg = temp_config_file({"bus": {"url": "http://x", "enabled": True}})
    with pytest.raises(ConfigError, match="enabled.*MS-06"):
        Config.load(cfg)


def test_load_requires_researcher_mcp_block(temp_config_file):
    cfg = temp_config_file({"researcher_mcp": None})
    with pytest.raises(ConfigError, match="researcher_mcp"):
        Config.load(cfg)


def test_load_requires_command_for_stdio_transport(temp_config_file):
    cfg = temp_config_file(
        {"researcher_mcp": {"transport": "stdio", "command": "", "args": []}}
    )
    with pytest.raises(ConfigError, match="command.*stdio"):
        Config.load(cfg)


def test_load_requires_url_for_http_transport(temp_config_file):
    cfg = temp_config_file(
        {"researcher_mcp": {"transport": "http", "url": "", "timeout": 30}}
    )
    with pytest.raises(ConfigError, match="url.*http"):
        Config.load(cfg)


def test_load_rejects_unknown_transport(temp_config_file):
    cfg = temp_config_file(
        {"researcher_mcp": {"transport": "carrier_pigeon", "command": "x", "args": []}}
    )
    with pytest.raises(ConfigError, match="transport"):
        Config.load(cfg)


def test_load_real_config_file():
    """The repo's actual config.yaml must always parse cleanly."""
    real = Path(__file__).resolve().parent.parent / "config.yaml"
    config = Config.load(real)
    assert config.bus.enabled is False
    assert config.researcher_mcp.transport == "stdio"
    assert "developer" in config.projects

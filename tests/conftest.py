"""Shared pytest fixtures for the developer test suite."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from developer.config import Config
from developer.pipeline import Pipeline

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "specs" / "MS-01" / "spec.md"
MILESTONE_PATH = REPO_ROOT / "milestones" / "MS-01" / "milestone.md"


def _make_config_dict(db_relative: str = "data/developer.db") -> dict:
    """Build a valid config dict for tests, with all blocks present."""
    return {
        "db_path": db_relative,
        "workspace_root": str(REPO_ROOT.parent),
        "prompts_dir": str(REPO_ROOT / "prompts"),
        "projects": {
            "developer": {
                "repo": str(REPO_ROOT),
                "specs_dir": "specs",
            },
        },
        "models": {
            "summarizer": "",
            "extractor": "",
            "assessor": "",
            "idea_parser": "",
            "embedder": "",
            "reviewer": "",
        },
        "bus": {"url": "http://localhost:8787", "enabled": False},
        "researcher_mcp": {
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "researcher.server"],
            "url": "",
            "timeout": 30,
        },
    }


@pytest.fixture
def temp_config_file(tmp_path):
    """Write a valid config.yaml to a temp dir and return its path."""

    def _make(overrides: dict | None = None) -> Path:
        data = _make_config_dict()
        if overrides:
            for k, v in overrides.items():
                if isinstance(v, dict) and isinstance(data.get(k), dict):
                    data[k].update(v)
                else:
                    data[k] = v
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(data))
        return cfg_path

    return _make


@pytest.fixture
def loaded_config(temp_config_file):
    """A loaded Config pointing at a temp DB but the real spec workspace."""
    return Config.load(temp_config_file())


@pytest.fixture
def pipeline(loaded_config):
    """A fully wired Pipeline using a temp DB."""
    return Pipeline.from_config(loaded_config)
